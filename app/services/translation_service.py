import asyncio
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from html import unescape
from time import monotonic

import aiohttp

from app.domain.models import SourcePost
from app.news.worker_errors import LlmError
from app.services.title_client import LMStudioTitleClient, SelectedTitleClient

logger = logging.getLogger(__name__)
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
TRANSLATION_FIELDS = (
    "title_translation", "title_language", "title_translation_provider",
    "title_translation_target", "title_translation_source",
)


@dataclass(frozen=True)
class CachedTranslation:
    result: tuple[str, str] | None
    provider: str
    expires_at: float


class TranslationService:
    def __init__(
        self, session: aiohttp.ClientSession, enabled: bool = True, timeout: int = 5,
        *, local_client: LMStudioTitleClient | SelectedTitleClient | None = None, fallback_mymemory: bool = True,
        local_timeout: float = 60, cache_size: int = 512, cache_ttl: float = 86400,
        failure_ttl: float = 30,
    ) -> None:
        self.session = session
        self.enabled = enabled
        self.timeout = timeout
        self.local_client = local_client
        self.fallback_mymemory = fallback_mymemory
        self.local_timeout = local_timeout
        self.cache_size = cache_size
        self.cache_ttl = cache_ttl
        self.failure_ttl = failure_ttl
        self.cache: OrderedDict[str, CachedTranslation] = OrderedDict()
        # Serialize short title requests so previews cannot overload a local model.
        self._lock = asyncio.Lock()
        self._local_unavailable_until = 0.0
        self._local_model = getattr(local_client, "model", None)

    async def enrich_title(self, post: SourcePost) -> None:
        if not self.enabled or not post.title or not any(c.isalpha() for c in post.title):
            return
        metadata = post.metadata
        if (
            metadata.get("title_translation")
            and metadata.get("title_translation_target", "en") == "en"
            and metadata.get("title_translation_source", post.title) == post.title
        ):
            return
        result = await self.translate_to_english(post.title)
        for field in TRANSLATION_FIELDS:
            metadata.pop(field, None)
        if not result:
            return
        translated, detected_language = result
        cached = self.cache.get(post.title)
        metadata.update(
            title_translation=translated,
            title_language=detected_language,
            title_translation_provider=cached.provider if cached else "unknown",
            title_translation_target="en",
            title_translation_source=post.title,
        )
        # Persist unchanged English titles too; CaptionService suppresses duplicate text.

    async def translate_to_english(self, text: str) -> tuple[str, str] | None:
        if not self.enabled or not text.strip() or len(text) > 2000:
            return None
        async with self._lock:
            model = getattr(self.local_client, "model", None)
            if model != self._local_model:
                self.cache.clear()
                self._local_unavailable_until = 0.0
                self._local_model = model
            cached = self.cache.get(text)
            if cached and cached.expires_at > monotonic():
                self.cache.move_to_end(text)
                return cached.result
            result = None
            provider = ""
            if self.local_client is not None and monotonic() >= self._local_unavailable_until:
                try:
                    async with asyncio.timeout(self.local_timeout):
                        response = await self.local_client.translate(text)
                    result = (response.translated_text, response.source_language)
                    provider = "lmstudio"
                    self._local_unavailable_until = 0
                except (LlmError, TimeoutError, aiohttp.ClientError, ValueError, TypeError):
                    self._local_unavailable_until = monotonic() + self.failure_ttl
                    logger.warning("title_translation_lmstudio_unavailable; fallback=%s", self.fallback_mymemory)
            if result is None and self.fallback_mymemory:
                result = await self._mymemory(text)
                if result:
                    provider = "mymemory"
            self.cache[text] = CachedTranslation(
                result, provider, monotonic() + (self.cache_ttl if result else self.failure_ttl),
            )
            self.cache.move_to_end(text)
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
            return result

    async def _mymemory(self, text: str) -> tuple[str, str] | None:
        # MyMemory limits q to 500 UTF-8 bytes; never translate a truncated title.
        if len(text.encode("utf-8")) > 500:
            return None
        # MyMemory's autodetect misidentifies very short kana titles. These scripts
        # provide an unambiguous hint; do not guess a language from Han or Latin alone.
        source_language = (
            "ja" if re.search(r"[\u3040-\u30ff\uff66-\uff9f]", text)
            else "ko" if re.search(r"[\uac00-\ud7af\u1100-\u11ff]", text)
            else "autodetect"
        )
        try:
            async with asyncio.timeout(self.timeout):
                async with self.session.get(
                    MYMEMORY_URL, params={"q": text, "langpair": f"{source_language}|en"},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        logger.warning("title_translation_http_error status=%s", response.status)
                        return None
                    data = await response.json(content_type=None)
            if not isinstance(data, dict) or str(data.get("responseStatus", 200)) != "200":
                return None
            response_data = data.get("responseData")
            if not isinstance(response_data, dict):
                return None
            translated = response_data.get("translatedText")
            detected = source_language if source_language != "autodetect" else response_data.get("detectedLanguage")
            if not isinstance(translated, str) or not isinstance(detected, str):
                return None
            translated = unescape(translated).strip()
            return (translated, detected.strip()) if translated and detected.strip() else None
        except (TimeoutError, aiohttp.ClientError, ValueError, TypeError):
            logger.warning("title_translation_mymemory_unavailable")
            return None
