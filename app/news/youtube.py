from __future__ import annotations

import asyncio
import html
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.domain.exceptions import InvalidUrlError
from app.news.classifier import normalize_youtube_url, youtube_video_id
from app.news.errors import (
    EmptyNewsContentError,
    MissingNewsDependencyError,
    NewsSourceAccessError,
    NewsSourceNotFoundError,
    NewsSourceRateLimitedError,
    NewsSourceUnavailableError,
    NewsTranscriptUnavailableError,
)
from app.news.models import (
    ExtractedNewsSource,
    ExtractionProgress,
    ExtractionStage,
    NewsMedia,
    NewsMediaKind,
    NewsSourceKind,
    ProgressCallback,
)
from app.utils.urls import validate_public_url


@dataclass(slots=True, frozen=True)
class Transcript:
    text: str
    language: str | None = None
    is_generated: bool | None = None
    segments: tuple[TranscriptSegment, ...] = ()


@dataclass(slots=True, frozen=True)
class TranscriptSegment:
    start: float
    duration: float
    text: str


class YouTubeMetadataAdapter(Protocol):
    async def fetch_metadata(self, url: str) -> Mapping[str, Any]: ...


class YouTubeTranscriptAdapter(Protocol):
    async def fetch_transcript(
        self,
        video_id: str,
        languages: Sequence[str],
    ) -> Transcript: ...


class YtDlpMetadataAdapter:
    """Read public video metadata without downloading media."""

    def __init__(self, *, socket_timeout: float = 20) -> None:
        self._socket_timeout = socket_timeout

    async def fetch_metadata(self, url: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> Mapping[str, Any]:
        try:
            import yt_dlp
        except ImportError as exc:
            raise MissingNewsDependencyError(
                "Для YouTube установите зависимость yt-dlp"
            ) from exc
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": self._socket_timeout,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                result = downloader.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            message = str(exc).lower()
            if (
                "private" in message
                or "sign in" in message
                or "members-only" in message
            ):
                raise NewsSourceAccessError(
                    "Видео YouTube недоступно без авторизации"
                ) from exc
            if "not available" in message or "removed" in message:
                raise NewsSourceNotFoundError(
                    "Видео YouTube удалено или недоступно"
                ) from exc
            if "429" in message or "too many requests" in message:
                raise NewsSourceRateLimitedError(
                    "YouTube временно ограничил запросы"
                ) from exc
            raise NewsSourceUnavailableError(
                "Не удалось получить данные YouTube"
            ) from exc
        if not isinstance(result, dict):
            raise NewsSourceUnavailableError("YouTube вернул некорректные метаданные")
        return result


class YoutubeTranscriptApiAdapter:
    def __init__(self, *, preserve_formatting: bool = False) -> None:
        self._preserve_formatting = preserve_formatting

    async def fetch_transcript(
        self,
        video_id: str,
        languages: Sequence[str],
    ) -> Transcript:
        return await asyncio.to_thread(self._fetch_sync, video_id, tuple(languages))

    def _fetch_sync(self, video_id: str, languages: Sequence[str]) -> Transcript:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError as exc:
            raise MissingNewsDependencyError(
                "Для субтитров установите зависимость youtube-transcript-api"
            ) from exc
        try:
            api = YouTubeTranscriptApi()
            try:
                fetched, language, generated = _fetch_preferred_transcript(
                    api,
                    YouTubeTranscriptApi,
                    video_id,
                    languages,
                    self._preserve_formatting,
                )
            except Exception as preferred_error:
                if type(preferred_error).__name__ != "NoTranscriptFound":
                    raise
                fetched, language, generated = _fetch_any_transcript(
                    api,
                    YouTubeTranscriptApi,
                    video_id,
                    self._preserve_formatting,
                )
            values = _transcript_values(fetched)
            language = getattr(fetched, "language_code", language)
            generated = getattr(fetched, "is_generated", generated)
        except Exception as exc:
            name = type(exc).__name__
            if name in {
                "NoTranscriptFound",
                "TranscriptsDisabled",
                "CouldNotRetrieveTranscript",
            }:
                raise NewsTranscriptUnavailableError(
                    "У видео нет доступных субтитров; потребуется локальное распознавание аудио"
                ) from exc
            if name in {"VideoUnavailable", "InvalidVideoId"}:
                raise NewsSourceNotFoundError("Видео YouTube недоступно") from exc
            if name in {"RequestBlocked", "IpBlocked", "TooManyRequests"}:
                raise NewsSourceRateLimitedError(
                    "YouTube заблокировал получение субтитров"
                ) from exc
            if isinstance(
                exc, (NewsTranscriptUnavailableError, NewsSourceNotFoundError)
            ):
                raise
            raise NewsSourceUnavailableError(
                "Не удалось получить субтитры YouTube"
            ) from exc
        text = _clean_transcript(values)
        if not text:
            raise NewsTranscriptUnavailableError("YouTube вернул пустые субтитры")
        return Transcript(
            text=text, language=language, is_generated=generated,
            segments=_transcript_segments(fetched),
        )


def _fetch_preferred_transcript(
    api: Any,
    api_type: type,
    video_id: str,
    languages: Sequence[str],
    preserve_formatting: bool,
) -> tuple[Any, str | None, bool | None]:
    if hasattr(api, "fetch"):
        return (
            api.fetch(
                video_id,
                languages=list(languages),
                preserve_formatting=preserve_formatting,
            ),
            None,
            None,
        )
    return (
        api_type.get_transcript(
            video_id,
            languages=list(languages),
            preserve_formatting=preserve_formatting,
        ),
        None,
        None,
    )


def _fetch_any_transcript(
    api: Any,
    api_type: type,
    video_id: str,
    preserve_formatting: bool,
) -> tuple[Any, str | None, bool | None]:
    if hasattr(api, "list"):
        transcript_list = api.list(video_id)
    elif hasattr(api_type, "list_transcripts"):
        transcript_list = api_type.list_transcripts(video_id)
    else:
        raise NewsTranscriptUnavailableError(
            "Установленная версия youtube-transcript-api не поддерживает выбор субтитров"
        )
    candidates = list(transcript_list)
    if not candidates:
        raise NewsTranscriptUnavailableError("У видео нет доступных субтитров")
    transcript = next(
        (
            candidate
            for candidate in candidates
            if not getattr(candidate, "is_generated", False)
        ),
        candidates[0],
    )
    fetched = transcript.fetch(preserve_formatting=preserve_formatting)
    return (
        fetched,
        getattr(transcript, "language_code", None),
        getattr(transcript, "is_generated", None),
    )


def _transcript_values(fetched: Any) -> list[str]:
    snippets = getattr(fetched, "snippets", fetched)
    return [
        str(
            getattr(
                item,
                "text",
                item.get("text", "") if isinstance(item, dict) else "",
            )
        )
        for item in snippets
    ]


def _transcript_segments(fetched: Any) -> tuple[TranscriptSegment, ...]:
    result: list[TranscriptSegment] = []
    for item in getattr(fetched, "snippets", fetched):
        def field(name: str, default: Any = None) -> Any:
            return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)

        try:
            start = float(field("start"))
            duration = float(field("duration", 0))
        except (TypeError, ValueError):
            continue
        text = _clean_inline(field("text"))
        if text and math.isfinite(start) and math.isfinite(duration) and start >= 0 and duration >= 0:
            result.append(TranscriptSegment(start=start, duration=duration, text=text))
    return tuple(result)


class YouTubeExtractor:
    def __init__(
        self,
        metadata_adapter: YouTubeMetadataAdapter,
        transcript_adapter: YouTubeTranscriptAdapter,
        *,
        languages: Sequence[str] = ("ru", "en"),
        video_analyzer: Any | None = None,
    ) -> None:
        self._metadata_adapter = metadata_adapter
        self._transcript_adapter = transcript_adapter
        self._languages = tuple(languages)
        self._video_analyzer = video_analyzer

    async def extract(
        self,
        url: str,
        *,
        progress: ProgressCallback | None = None,
        vision_client: Any | None = None,
    ) -> ExtractedNewsSource:
        normalized_url = normalize_youtube_url(url)
        source_id = youtube_video_id(normalized_url)
        await _emit(
            progress,
            ExtractionStage.EXTRACTING_METADATA,
            "Получаем метаданные YouTube",
        )
        metadata = dict(await self._metadata_adapter.fetch_metadata(normalized_url))
        await _emit(
            progress,
            ExtractionStage.EXTRACTING_SUBTITLES,
            "Извлекаем субтитры YouTube",
        )
        transcript: Transcript | None
        try:
            transcript = await self._transcript_adapter.fetch_transcript(source_id, self._languages)
        except (NewsTranscriptUnavailableError, NewsSourceRateLimitedError):
            if self._video_analyzer is None:
                raise
            transcript = None

        video_result = None
        if self._video_analyzer is not None:
            video_result = await self._video_analyzer.analyze(
                normalized_url, metadata=metadata, need_audio=transcript is None,
                vision_client=vision_client, progress=progress,
            )
            if transcript is None:
                transcript = video_result.transcript

        title = _clean_inline(metadata.get("title")) or "Видео YouTube"
        description = _clean_block(metadata.get("description"))
        raw_parts = [f"Название: {title}"]
        if description:
            raw_parts.append(f"Описание:\n{description}")
        timed: list[tuple[float, str]] = []
        if transcript is not None:
            if transcript.segments:
                label = "Речь (Whisper)" if video_result and video_result.transcript else "Субтитры"
                timed.extend((segment.start, f"{label}: {segment.text}") for segment in transcript.segments)
            else:
                raw_parts.append(f"Субтитры:\n{transcript.text}")
        if video_result is not None:
            timed.extend((item.start, f"{item.kind}: {item.text}") for item in video_result.timeline)
            if video_result.warnings:
                raw_parts.append("Ограничения анализа:\n" + "\n".join(video_result.warnings))
        if timed:
            timed.sort(key=lambda item: item[0])
            raw_parts.append("Сведения из видео с исходными таймкодами:\n" + "\n".join(
                f"[{_timestamp(at)}] {text}" for at, text in timed
            ))
        raw_text = "\n\n".join(raw_parts)
        if not raw_text.strip():
            raise EmptyNewsContentError("YouTube не вернул текст для обработки")

        thumbnail = _safe_url(metadata.get("thumbnail"))
        if not thumbnail:
            thumbnail = _best_thumbnail(metadata.get("thumbnails"))
        media = (
            [NewsMedia(kind=NewsMediaKind.IMAGE, url=thumbnail)] if thumbnail else []
        )
        author_url = _safe_url(
            metadata.get("channel_url") or metadata.get("uploader_url")
        )
        return ExtractedNewsSource(
            kind=NewsSourceKind.YOUTUBE,
            source_id=source_id,
            source_url=url,
            normalized_url=normalized_url,
            title=title[:500],
            raw_text=raw_text,
            author_name=_clean_inline(
                metadata.get("channel") or metadata.get("uploader")
            )
            or None,
            author_url=author_url,
            published_at=_published_at(metadata),
            media=media,
            metadata={
                "extractor": "yt-dlp+youtube-transcript-api",
                "duration": _positive_int(metadata.get("duration")),
                "view_count": _positive_int(metadata.get("view_count")),
                "channel_id": _clean_inline(metadata.get("channel_id")) or None,
                "transcript_language": transcript.language if transcript else None,
                "transcript_generated": transcript.is_generated if transcript else None,
                **({"video_analysis": video_result.metadata, "analysis_warnings": video_result.warnings} if video_result else {}),
            },
        )


async def _emit(
    progress: ProgressCallback | None,
    stage: ExtractionStage,
    message: str,
) -> None:
    if progress is not None:
        await progress(ExtractionProgress(stage=stage, message=message))


def _timestamp(seconds: float) -> str:
    total_ms = round(max(0.0, seconds) * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _clean_transcript(values: Sequence[str]) -> str:
    result: list[str] = []
    previous = ""
    for value in values:
        cleaned = " ".join(html.unescape(value).split())
        if cleaned and cleaned != previous:
            result.append(cleaned)
            previous = cleaned
    return "\n".join(result)


def _clean_inline(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def _clean_block(value: Any) -> str:
    lines = [_clean_inline(line) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _safe_url(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        return validate_public_url(candidate)
    except InvalidUrlError:
        return None


def _best_thumbnail(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    ranked = sorted(
        (item for item in value if isinstance(item, dict)),
        key=lambda item: (
            (_positive_int(item.get("width")) or 0)
            * (_positive_int(item.get("height")) or 0)
        ),
        reverse=True,
    )
    return next((url for item in ranked if (url := _safe_url(item.get("url")))), None)


def _published_at(metadata: Mapping[str, Any]) -> datetime | None:
    timestamp = metadata.get("timestamp") or metadata.get("release_timestamp")
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    value = str(metadata.get("upload_date") or metadata.get("release_date") or "")
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
