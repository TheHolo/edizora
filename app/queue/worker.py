import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Channel, Job, MediaRecord, Publication, JobAsset, MediaAsset
from app.domain.enums import ContentKind, JobStatus, MediaType
from app.domain.exceptions import ApplicationError, DuplicatePublicationError, UncertainPublishError
from app.domain.models import PreparedMedia
from app.services.caption_service import CaptionService
from app.services.download_service import DownloadService
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.news_render_service import NewsRenderService
from app.services.preview_service import deserialize_post
from app.services.publisher_service import TelegramPublisher
from app.services.translation_service import TranslationService
from app.utils.tags import publication_tags

logger = logging.getLogger(__name__)


def update_channel_schedule_after_publication(
    channel: Channel, job: Job, published_at: datetime,
) -> None:
    """Advance the regular interval only for publications from the regular queue."""
    if job.scheduled_at is not None:
        return
    channel.next_publish_at = (
        published_at + timedelta(seconds=channel.publish_interval_seconds)
        if channel.publish_interval_seconds else None
    )


@dataclass(frozen=True)
class WorkerSnapshot:
    index: int
    alive: bool
    job_id: int | None
    channel_alias: str | None
    stage: str | None
    busy_seconds: float | None
    heartbeat_age_seconds: float | None


@dataclass
class _WorkerRuntime:
    index: int
    last_heartbeat: float | None = None
    job_id: int | None = None
    channel_alias: str | None = None
    stage: str | None = None
    busy_since: float | None = None


class WorkerPool:
    def __init__(
        self, *, bot: Bot, sessions: async_sessionmaker[AsyncSession], jobs: JobService,
        downloader: DownloadService, media: MediaService, captions: CaptionService,
        publisher: TelegramPublisher, count: int, wakeup: asyncio.Event,
        delete_after_publish: bool, storage: Path, auto_add_source_tags: bool,
        max_tags: int, max_tag_length: int, translator: TranslationService,
        news_renderer: NewsRenderService | None = None,
    ) -> None:
        self.bot, self.sessions, self.jobs = bot, sessions, jobs
        self.downloader, self.media, self.captions, self.publisher = downloader, media, captions, publisher
        self.translator = translator
        self.news_renderer = news_renderer or NewsRenderService()
        self.count, self.wakeup = count, wakeup
        self.delete_after_publish, self.storage = delete_after_publish, storage
        self.auto_add_source_tags = auto_add_source_tags
        self.max_tags, self.max_tag_length = max_tags, max_tag_length
        self.tasks: list[asyncio.Task] = []
        self._runtime = [_WorkerRuntime(index=index) for index in range(count)]
        self.stopping = False

    async def start(self) -> None:
        await self.jobs.recover()
        self.stopping = False
        now = time.monotonic()
        for state in self._runtime:
            state.last_heartbeat = now
        self.tasks = [asyncio.create_task(self._run(index), name=f"publisher-worker-{index}") for index in range(self.count)]
        self.wakeup.set()

    async def stop(self) -> None:
        self.stopping = True
        self.wakeup.set()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run(self, index: int) -> None:
        while not self.stopping:
            self._heartbeat(index)
            try:
                job = await self.jobs.claim_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker_claim_failed worker=%s", index)
                await asyncio.sleep(2)
                continue
            if not job:
                self._set_idle(index)
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=10)
                except TimeoutError:
                    pass
                continue
            channel_alias = getattr(getattr(job, "channel", None), "alias", "unknown")
            self._set_busy(index, job.id, channel_alias, JobStatus.DOWNLOADING)
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if getattr(error,"code",None)=="publication_blocked":
                    await self.jobs.defer(job.id)
                    continue
                retryable = isinstance(error, ApplicationError) and error.retryable
                await self.jobs.fail(job.id, error, retryable)
                logger.exception("job_failed job_id=%s provider=%s attempt=%s", job.id, job.provider, job.attempts)
                await self._notify(job, f"Задание #{job.id}: ошибка — {error}")
                if retryable and job.attempts < job.max_attempts:
                    self.wakeup.set()
            finally:
                self._set_idle(index)

    async def _process(self, job: Job) -> None:
        if getattr(self.jobs, "gate", None):
            return await self._process_managed(job)
        self._set_job_stage(job.id, JobStatus.RESOLVING)
        post = deserialize_post(job.post_data)
        if post.content_kind == ContentKind.ARTWORK:
            await self.translator.enrich_title(post)
        duplicate = await self.jobs.duplicate(job)
        if duplicate and not job.allow_duplicate:
            raise DuplicatePublicationError(f"Эта публикация уже отправлена в канал {job.channel.alias}")
        downloaded_by_order = {}
        prepared: list[PreparedMedia] = []
        total_media = len(post.media_items)
        for position, item in enumerate(post.media_items, start=1):
            self._set_job_stage(job.id, f"{JobStatus.DOWNLOADING} {position}/{total_media}")
            if await self.jobs.is_cancelled(job.id):
                await self.jobs.transition(job.id, JobStatus.CANCELLED)
                return
            if item.telegram_file_id:
                prepared.append(PreparedMedia(
                    path=None,
                    as_document=item.media_type == MediaType.DOCUMENT,
                    order=item.order,
                    media_type=item.media_type,
                    telegram_file_id=item.telegram_file_id,
                ))
            else:
                downloaded_by_order[item.order] = await self.downloader.download(job.id, item)
        await self.jobs.transition(job.id, JobStatus.PROCESSING)
        for position, downloaded_item in enumerate(downloaded_by_order.values(), start=1):
            self._set_job_stage(job.id, f"{JobStatus.PROCESSING} {position}/{total_media}")
            prepared.append(await self.media.prepare(downloaded_item, job.channel.publish_mode))
        prepared.sort(key=lambda item: item.order)
        if await self.jobs.is_cancelled(job.id):
            await self.jobs.transition(job.id, JobStatus.CANCELLED)
            return
        caption_override = getattr(job, "caption_override", None)
        caption_tags = publication_tags(
            job.user_tags, job.source_tags, getattr(job.channel, "default_tags", None),
            include_source=self.auto_add_source_tags, max_tags=self.max_tags, max_length=self.max_tag_length,
        )
        if caption_override is not None:
            caption = (
                self.news_renderer.build_custom(caption_override, post, caption_tags)
                if post.content_kind == ContentKind.NEWS
                else self.captions.build_custom(caption_override)
            )
        else:
            caption = (
                self.news_renderer.build(post, caption_tags)
                if post.content_kind == ContentKind.NEWS
                else self.captions.build(
                    post, caption_tags, job.channel.caption_template or self.captions_template,
                )
            )
        await self.jobs.transition(job.id, JobStatus.PUBLISHING)
        self._set_job_stage(job.id, JobStatus.PUBLISHING)
        result = await self.publisher.publish(job, post, prepared, job.channel, caption)
        try:
            async with self.sessions() as session, session.begin():
                session.add(Publication(
                    job_id=job.id, channel_id=job.target_channel_id, telegram_chat_id=result.chat_id,
                    telegram_message_ids=result.message_ids, published_at=result.published_at, caption=caption,
                ))
                channel = await session.get(Channel, job.target_channel_id)
                if channel:
                    update_channel_schedule_after_publication(channel, job, result.published_at)
                    from app.application.scheduling import record_catchup_success
                    await record_catchup_success(session,job,result.published_at)
                for source, prepared_item, message_id in zip(
                    post.media_items, prepared, result.message_ids, strict=False
                ):
                    downloaded_item = downloaded_by_order.get(source.order)
                    session.add(MediaRecord(
                        job_id=job.id, source_url=source.url,
                        local_path=str(downloaded_item.path) if downloaded_item else None,
                        prepared_path=str(prepared_item.path) if prepared_item.path else None,
                        filename=source.filename,
                        mime_type=downloaded_item.mime_type if downloaded_item else source.mime_type,
                        media_type=source.media_type,
                        size=downloaded_item.size if downloaded_item else source.size,
                        width=downloaded_item.width if downloaded_item else source.width,
                        height=downloaded_item.height if downloaded_item else source.height,
                        sort_order=source.order,
                        download_status="completed" if downloaded_item else "telegram",
                        publish_status="completed", telegram_message_id=message_id,
                    ))
            await self.jobs.transition(job.id, JobStatus.COMPLETED)
        except Exception as error:
            raise UncertainPublishError("Telegram подтвердил отправку, но запись истории не завершена; проверьте канал") from error
        logger.info("job_published job_id=%s provider=%s channel=%s messages=%s", job.id, job.provider, job.channel.alias, len(result.message_ids))
        await self._notify(job, f"Задание #{job.id} успешно опубликовано в {job.channel.alias}.")
        if self.delete_after_publish:
            shutil.rmtree(self.storage / "jobs" / str(job.id), ignore_errors=True)

    async def _process_managed(self, job: Job) -> None:
        """Prepared jobs remain publishable without the home AI or source website."""
        from app.storage.assets import safe_path
        async with self.sessions() as session:
            current = await self.jobs.get(job.id)
            plan = current.render_plan
            if not plan:
                raise ApplicationError("Сначала подготовьте публикацию в редакторе")
            links = list((await session.execute(select(JobAsset, MediaAsset).join(MediaAsset, JobAsset.asset_id == MediaAsset.id).where(JobAsset.job_id == job.id,JobAsset.selected.is_(True)).order_by(JobAsset.sort_order))).all())
        post = deserialize_post(current.post_data)
        if current.content_kind == ContentKind.ARTWORK and not post.media_items:
            raise ApplicationError("Выберите хотя бы одно изображение")
        from app.application.prepared_media import load_prepared_media
        try:prepared=load_prepared_media(plan,links,post,self.storage,job.channel.publish_mode)
        except ValueError as error:raise ApplicationError("Подготовленный каталог медиа недоступен") from error
        duplicate=await self.jobs.duplicate(current)
        if duplicate and not current.allow_duplicate:
            raise DuplicatePublicationError("Этот источник уже опубликован в канале")
        if await self.jobs.is_cancelled(job.id):
            await self.jobs.transition(job.id,JobStatus.CANCELLED);return
        await self.jobs.transition(job.id,JobStatus.PUBLISHING)
        current=await self.jobs.get(job.id)
        result=await self.publisher.publish(current,post,prepared,current.channel,plan["html"])
        try:
            async with self.sessions() as session,session.begin():
                session.add(Publication(job_id=job.id,channel_id=job.target_channel_id,telegram_chat_id=result.chat_id,telegram_message_ids=result.message_ids,published_at=result.published_at,caption=plan["html"]))
                channel=await session.get(Channel,job.target_channel_id)
                if channel:update_channel_schedule_after_publication(channel,job,result.published_at)
                from app.application.scheduling import record_catchup_success
                await record_catchup_success(session,current,result.published_at)
            await self.jobs.transition(job.id,JobStatus.COMPLETED)
        except Exception as error:
            raise UncertainPublishError("Telegram подтвердил отправку, но запись истории не завершена; проверьте канал") from error
        await self._notify(job,f"Задание #{job.id} успешно опубликовано в {job.channel.alias}.")

    async def close_and_drain(self, migration_id: str) -> None:
        # A migration closes permits before draining already accepted work.
        if getattr(self.jobs, "gate", None):
            self.jobs.gate.block("migration",migration_id)
        await self.stop()

    async def assert_quiesced(self) -> None:
        if any(not task.done() for task in self.tasks):
            raise RuntimeError("Edizora workers are still running")

    def snapshot(self) -> list[WorkerSnapshot]:
        now = time.monotonic()
        snapshots = []
        for state in self._runtime:
            task = self.tasks[state.index] if state.index < len(self.tasks) else None
            snapshots.append(WorkerSnapshot(
                index=state.index,
                alive=task is not None and not task.done(),
                job_id=state.job_id,
                channel_alias=state.channel_alias,
                stage=state.stage,
                busy_seconds=now - state.busy_since if state.busy_since is not None else None,
                heartbeat_age_seconds=(
                    now - state.last_heartbeat if state.last_heartbeat is not None else None
                ),
            ))
        return snapshots

    def _heartbeat(self, index: int) -> None:
        self._runtime[index].last_heartbeat = time.monotonic()

    def _set_busy(self, index: int, job_id: int, channel_alias: str, stage: str) -> None:
        state = self._runtime[index]
        now = time.monotonic()
        state.last_heartbeat = now
        state.job_id = job_id
        state.channel_alias = channel_alias
        state.stage = str(stage)
        state.busy_since = now

    def _set_idle(self, index: int) -> None:
        state = self._runtime[index]
        state.last_heartbeat = time.monotonic()
        state.job_id = None
        state.channel_alias = None
        state.stage = None
        state.busy_since = None

    def _set_job_stage(self, job_id: int, stage: str) -> None:
        for state in self._runtime:
            if state.job_id == job_id:
                state.stage = str(stage)
                state.last_heartbeat = time.monotonic()
                return

    @property
    def captions_template(self) -> str:
        from app.services.caption_service import DEFAULT_TEMPLATE
        return DEFAULT_TEMPLATE

    async def _notify(self, job: Job, text: str) -> None:
        try:
            async with self.sessions() as session:
                from app.db.models import User
                user = await session.get(User, job.created_by_user_id)
                if user:
                    await self.bot.send_message(user.telegram_user_id, text)
        except Exception:
            logger.warning("notification_failed job_id=%s", job.id, exc_info=True)
