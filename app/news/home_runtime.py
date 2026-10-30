from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from app.news.errors import NewsSourceAccessError
from app.news.models import (
    ExtractedNewsSource,
    ExtractionProgress,
    NewsMedia,
    NewsMediaKind,
    NewsSourceKind,
    NewsSourceRequest,
    ProgressCallback,
)
from app.news.structured_client import StructuredNewsClient
from app.news.vps_client import VpsNewsApiClient
from app.news.worker_errors import LeaseLostError, VpsApiError, VpsAuthenticationError
from app.news.worker_models import NewsDraft, NewsTask, ProgressStage, WorkerResult

logger = logging.getLogger(__name__)


class NewsExtractor(Protocol):
    async def extract(
        self,
        request: NewsSourceRequest | str,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractedNewsSource: ...


class HomeNewsWorker:
    def __init__(
        self,
        *,
        api: VpsNewsApiClient,
        extractor: NewsExtractor,
        ollama: StructuredNewsClient | None,
        poll_interval_seconds: float = 5,
        ready: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.api = api
        self.extractor = extractor
        self.ollama = ollama
        self.poll_interval_seconds = poll_interval_seconds
        self.ready = ready

    async def run_once(self) -> bool:
        if self.ready is not None and not await self.ready():
            return False
        if self.ollama is None:
            return False
        task = await self.api.lease()
        if task is None:
            return False
        await self._process(task)
        return True

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                processed = await self.run_once()
            except VpsAuthenticationError:
                logger.error("home_worker_authentication_failed")
                raise
            except VpsApiError:
                logger.warning("home_worker_vps_unavailable", exc_info=True)
                processed = False
            if not processed:
                await _wait_or_stop(stop, self.poll_interval_seconds)

    async def _process(self, task: NewsTask) -> None:
        progress = _TaskProgress(self.api, task)
        work = asyncio.create_task(
            self._execute(task, progress), name=f"news-task-work-{task.id}",
        )
        heartbeat = asyncio.create_task(
            progress.keepalive(), name=f"news-task-heartbeat-{task.id}",
        )
        try:
            done, _ = await asyncio.wait(
                {work, heartbeat}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                exception = heartbeat.exception()
                if exception is not None:
                    if not work.done():
                        work.cancel()
                        await asyncio.gather(work, return_exceptions=True)
                    raise exception
            await work
        except VpsAuthenticationError:
            raise
        except LeaseLostError:
            logger.info("home_worker_lease_lost task_id=%s", task.id)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - task failures must be reported to the VPS
            retryable = bool(getattr(error, "retryable", False))
            logger.warning(
                "home_worker_task_failed task_id=%s retryable=%s type=%s",
                task.id,
                retryable,
                type(error).__name__,
            )
            try:
                await self.api.fail(task, error, retryable=retryable)
            except VpsAuthenticationError:
                raise
            except LeaseLostError:
                logger.info("home_worker_failure_lease_lost task_id=%s", task.id)
            except VpsApiError:
                logger.warning("home_worker_failure_report_failed task_id=%s", task.id)
        finally:
            work.cancel()
            heartbeat.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def _execute(self, task: NewsTask, progress: _TaskProgress) -> None:
        llm = self.ollama
        if llm is None:
            raise RuntimeError("Локальная модель не выбрана")
        first_stage = (
            ProgressStage.EXTRACTING_CONTENT
            if task.input_payload.kind is NewsSourceKind.MANUAL
            else ProgressStage.EXTRACTING_METADATA
        )
        await progress.report(first_stage, "Подготавливаем источник")
        source = await self._extract_source(task, progress)
        _validate_source(source)

        await progress.report(
            ProgressStage.LLM_PROCESSING,
            f"Локальная модель {llm.model} создаёт черновик",
        )
        original_chunks = getattr(llm, "max_source_chunks", None)
        original_reasoning = getattr(llm, "_disable_reasoning", None)
        video_analyzed = source.kind is NewsSourceKind.YOUTUBE and "video_analysis" in source.metadata
        if video_analyzed and original_chunks is not None:
            from app.news.worker_errors import SourceTooLargeError
            if len(source.raw_text) > 2_000_000:
                raise SourceTooLargeError("Результат анализа видео превышает лимит текста")
            llm.max_source_chunks = getattr(llm, "youtube_max_source_chunks", original_chunks)
        if video_analyzed and original_reasoning is not None:
            llm._disable_reasoning = True
        try:
            draft = await llm.rewrite(source)
        finally:
            if original_chunks is not None:
                llm.max_source_chunks = original_chunks
            if original_reasoning is not None:
                llm._disable_reasoning = original_reasoning
        source_warnings = source.metadata.get("analysis_warnings") or []
        if source_warnings:
            draft = draft.model_copy(update={"warnings": list(dict.fromkeys(
                [*source_warnings, *draft.warnings]
            ))[:20]})

        await progress.report(
            ProgressStage.VALIDATING,
            "Проверяем структуру черновика",
        )
        # Revalidate a detached representation before it crosses the trust boundary.
        validated_draft = NewsDraft.model_validate(draft.model_dump(mode="python"))
        await self.api.complete(
            task,
            WorkerResult(
                source=_completion_source(source),
                draft=validated_draft,
            ),
        )
        logger.info("home_worker_task_completed task_id=%s", task.id)

    async def _extract_source(
        self,
        task: NewsTask,
        progress: _TaskProgress,
    ) -> ExtractedNewsSource:
        payload = task.input_payload
        if (
            payload.kind is NewsSourceKind.TELEGRAM
            and (payload.telegram is not None or payload.source_text is not None)
        ):
            await progress.report(
                ProgressStage.EXTRACTING_CONTENT,
                "Подготавливаем пересланный Telegram-пост",
            )
            return _forwarded_telegram_source(task)
        request = NewsSourceRequest(kind=payload.kind, value=payload.extraction_value())
        if payload.kind is NewsSourceKind.YOUTUBE:
            from app.news.facade import NewsSourceFacade
            if isinstance(self.extractor, NewsSourceFacade):
                return await self.extractor.extract_youtube(
                    request, vision_client=self.ollama, progress=progress.from_extractor,
                )
        return await self.extractor.extract(request, progress=progress.from_extractor)


class _TaskProgress:
    def __init__(self, api: VpsNewsApiClient, task: NewsTask) -> None:
        self.api = api
        self.task = task
        self.last_stage: str | None = None
        self.last_message: str | None = None
        self._lock = asyncio.Lock()

    async def report(self, stage: ProgressStage | str, message: str) -> None:
        value = stage.value if isinstance(stage, ProgressStage) else stage
        async with self._lock:
            if value == self.last_stage and message == self.last_message:
                return
            self.last_stage = value
            self.last_message = message
            try:
                await self.api.progress(self.task, value, message)
            except VpsAuthenticationError:
                raise
            except LeaseLostError:
                raise
            except VpsApiError:
                # Progress is advisory. A temporary failure must not waste completed extraction.
                logger.warning(
                    "home_worker_progress_failed task_id=%s stage=%s",
                    self.task.id,
                    value,
                )

    async def from_extractor(self, update: ExtractionProgress) -> None:
        await self.report(update.stage.value, update.message)

    async def keepalive(self) -> None:
        lease_seconds = float(getattr(self.api, "lease_seconds", 1800))
        # A short silent heartbeat also makes user cancellation stop local GPU
        # work promptly without producing extra Telegram status updates.
        interval = max(15.0, min(lease_seconds / 3, 60.0))
        while True:
            await asyncio.sleep(interval)
            async with self._lock:
                if self.last_stage is None or self.last_message is None:
                    continue
                try:
                    await self.api.progress(
                        self.task, self.last_stage, self.last_message,
                    )
                except (VpsAuthenticationError, LeaseLostError):
                    raise
                except VpsApiError:
                    logger.warning(
                        "home_worker_heartbeat_failed task_id=%s stage=%s",
                        self.task.id,
                        self.last_stage,
                    )


def _validate_source(source: ExtractedNewsSource) -> None:
    if not source.raw_text.strip():
        raise ValueError("extractor returned empty source text")
    if not source.source_id.strip():
        raise ValueError("extractor returned empty source id")


def _completion_source(source: ExtractedNewsSource) -> ExtractedNewsSource:
    """Return metadata and proof of input without copying a long article/transcript to VPS."""
    digest = hashlib.sha256(source.raw_text.encode("utf-8")).hexdigest()
    return replace(
        source,
        raw_text=f"processed-locally sha256={digest} characters={len(source.raw_text)}",
    )


async def _wait_or_stop(stop: asyncio.Event, timeout_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_seconds)
    except TimeoutError:
        pass


def _forwarded_telegram_source(task: NewsTask) -> ExtractedNewsSource:
    payload = task.input_payload
    telegram = dict(payload.telegram or {})
    if telegram.get("has_protected_content") is True:
        raise NewsSourceAccessError("Автор запретил пересылку содержимого этого Telegram-поста")
    raw_text = (
        payload.source_text
        or _optional_text(telegram.get("text"))
        or _optional_text(telegram.get("caption"))
        or ""
    ).strip()
    if not raw_text:
        raise ValueError("forwarded Telegram post does not contain text")

    source_url = payload.source_url or _optional_text(
        telegram.get("source_url") or telegram.get("url")
    )
    message_id = telegram.get("message_id")
    chat_id = telegram.get("chat_id")
    source_id = _optional_text(telegram.get("source_id"))
    if not source_id and message_id is not None:
        source_id = f"{chat_id or 'telegram'}:{message_id}"
    if not source_id:
        fingerprint = f"{source_url or ''}\n{raw_text}".encode()
        source_id = hashlib.sha256(fingerprint).hexdigest()[:24]

    media_payload = telegram.get("media") or telegram.get("media_items") or []
    media = [_telegram_media(item) for item in media_payload if isinstance(item, dict)]
    metadata = dict(payload.metadata)
    metadata.update(dict(telegram.get("metadata") or {}))
    for key in ("chat_id", "message_id", "chat_title", "has_protected_content"):
        if key in telegram:
            metadata[key] = telegram[key]

    title = _optional_text(telegram.get("title") or telegram.get("chat_title"))
    return ExtractedNewsSource(
        kind=NewsSourceKind.TELEGRAM,
        source_id=source_id,
        source_url=source_url,
        normalized_url=source_url,
        title=title or raw_text.splitlines()[0][:180],
        raw_text=raw_text,
        author_name=_optional_text(
            telegram.get("author_name") or telegram.get("channel_title")
        ),
        author_url=_optional_text(telegram.get("author_url")),
        published_at=_optional_datetime(telegram.get("published_at") or telegram.get("date")),
        media=media,
        metadata=metadata,
    )


def _telegram_media(payload: dict) -> NewsMedia:
    normalized = dict(payload)
    raw_kind = str(normalized.get("kind") or normalized.get("media_type") or "image")
    normalized["kind"] = {
        "photo": NewsMediaKind.IMAGE.value,
        "thumbnail": NewsMediaKind.IMAGE.value,
    }.get(raw_kind, raw_kind)
    if "telegram_file_id" not in normalized and normalized.get("file_id"):
        normalized["telegram_file_id"] = normalized["file_id"]
    return NewsMedia.from_dict(normalized)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
