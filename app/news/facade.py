from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

import aiohttp

from app.news.classifier import classify_news_input
from app.news.errors import EmptyNewsContentError, UnsupportedNewsSourceError
from app.news.http import SafeHttpFetcher
from app.news.models import (
    ExtractedNewsSource,
    ExtractionProgress,
    ExtractionStage,
    NewsSourceKind,
    NewsSourceRequest,
    ProgressCallback,
)
from app.news.telegram import ForwardedTelegramPost, TelegramPostExtractor
from app.news.website import WebsiteExtractor
from app.news.youtube import (
    YouTubeExtractor,
    YoutubeTranscriptApiAdapter,
    YtDlpMetadataAdapter,
)


class _Extractor(Protocol):
    async def extract(
        self,
        value: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractedNewsSource: ...


class NewsSourceFacade:
    def __init__(
        self,
        *,
        website: _Extractor | None = None,
        youtube: _Extractor | None = None,
        telegram: _Extractor | None = None,
        max_manual_characters: int = 1_000_000,
    ) -> None:
        self._extractors: dict[NewsSourceKind, _Extractor] = {}
        self._telegram = telegram
        if website is not None:
            self._extractors[NewsSourceKind.WEBSITE] = website
        if youtube is not None:
            self._extractors[NewsSourceKind.YOUTUBE] = youtube
        if telegram is not None:
            self._extractors[NewsSourceKind.TELEGRAM] = telegram
        self._max_manual_characters = max_manual_characters

    @classmethod
    def from_session(
        cls,
        session: aiohttp.ClientSession,
        *,
        max_source_bytes: int = 4 * 1024 * 1024,
        youtube_languages: tuple[str, ...] = ("ru", "en"),
        worker_settings: object | None = None,
    ) -> NewsSourceFacade:
        fetcher = SafeHttpFetcher(session, max_bytes=max_source_bytes)
        from app.news.video import VideoAnalyzer, VideoOptions

        settings = worker_settings
        analyzer = None
        if settings is None or getattr(settings, "youtube_video_analysis", True):
            analyzer = VideoAnalyzer(VideoOptions(
                sample_fps=getattr(settings, "youtube_sample_fps", 2.0),
                max_height=getattr(settings, "youtube_max_height", 720),
                max_seconds=getattr(settings, "youtube_max_seconds", 3600),
                max_bytes=getattr(settings, "youtube_max_bytes", 512 * 1024 * 1024),
                ocr_confidence=getattr(settings, "youtube_ocr_confidence", 0.8),
                vision_interval_seconds=getattr(settings, "youtube_vision_interval_seconds", 30.0),
                vision_parallelism=getattr(settings, "youtube_vision_parallelism", 2),
                whisper_model=getattr(settings, "youtube_whisper_model", "small"),
                cache_dir=Path(getattr(settings, "youtube_cache_dir", ".cache/edizora-video")),
            ))
        return cls(
            website=WebsiteExtractor(fetcher),
            youtube=YouTubeExtractor(
                YtDlpMetadataAdapter(),
                YoutubeTranscriptApiAdapter(),
                languages=youtube_languages,
                video_analyzer=analyzer,
            ),
            telegram=TelegramPostExtractor(fetcher),
        )

    async def extract(
        self,
        request: NewsSourceRequest | str,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractedNewsSource:
        resolved = classify_news_input(request) if isinstance(request, str) else request
        if resolved.kind is NewsSourceKind.MANUAL:
            return await self._extract_manual(resolved.value, progress=progress)
        extractor = self._extractors.get(resolved.kind)
        if extractor is None:
            raise UnsupportedNewsSourceError(
                f"Обработчик источника {resolved.kind.value} не настроен"
            )
        return await extractor.extract(resolved.value, progress=progress)

    async def extract_youtube(
        self, request: NewsSourceRequest, *, vision_client: object,
        progress: ProgressCallback | None = None,
    ) -> ExtractedNewsSource:
        extractor = self._extractors.get(NewsSourceKind.YOUTUBE)
        if not isinstance(extractor, YouTubeExtractor):
            raise UnsupportedNewsSourceError("Обработчик YouTube не настроен")
        if extractor._video_analyzer is not None:
            from app.news.video import _loopback
            if not _loopback(getattr(vision_client, "base_url", "")):
                raise UnsupportedNewsSourceError("Для анализа видео выберите локальную модель LM Studio или Ollama")
        return await extractor.extract(request.value, progress=progress, vision_client=vision_client)

    async def extract_forwarded(
        self,
        post: ForwardedTelegramPost,
        *,
        progress: ProgressCallback | None = None,
    ) -> ExtractedNewsSource:
        extractor = self._telegram
        if extractor is None or not hasattr(extractor, "extract_forwarded"):
            raise UnsupportedNewsSourceError(
                "Обработчик пересланных Telegram-постов не настроен"
            )
        return await extractor.extract_forwarded(post, progress=progress)

    async def _extract_manual(
        self,
        value: str,
        *,
        progress: ProgressCallback | None,
    ) -> ExtractedNewsSource:
        text = _clean_manual_text(value)
        if not text:
            raise EmptyNewsContentError("Текст новости пуст")
        if len(text) > self._max_manual_characters:
            raise EmptyNewsContentError("Ручной текст слишком большой")
        if progress is not None:
            await progress(
                ExtractionProgress(
                    ExtractionStage.EXTRACTING_CONTENT,
                    "Подготавливаем введённый текст",
                )
            )
        first_line = text.splitlines()[0]
        title = first_line[:180] if first_line else "Новая публикация"
        return ExtractedNewsSource(
            kind=NewsSourceKind.MANUAL,
            source_id=hashlib.sha256(text.encode("utf-8")).hexdigest()[:24],
            source_url=None,
            normalized_url=None,
            title=title,
            raw_text=text,
            metadata={"extractor": "manual-input"},
        )


def _clean_manual_text(value: str) -> str:
    lines = [
        line.rstrip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()
