from unittest.mock import AsyncMock

import aiohttp

from app.domain.models import SourcePost
from app.services.translation_service import TranslationService


class FakeResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, *, content_type=None):
        return self.payload


class FakeSession:
    def __init__(self, response=None, *, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def make_post(title: str) -> SourcePost:
    return SourcePost(
        provider="pixiv", source_id="1", source_url="https://x", normalized_url="https://x",
        title=title, author_name="Artist", author_url="https://x/artist", media_items=[],
    )


async def test_non_ascii_title_is_enriched_with_translation():
    service = TranslationService(AsyncMock())
    service.translate_to_english = AsyncMock(return_value=("What do you think?", "ja"))
    post = make_post("どうじゃ？")

    await service.enrich_title(post)

    assert post.metadata["title_translation"] == "What do you think?"
    assert post.metadata["title_language"] == "ja"


async def test_ascii_foreign_title_is_also_translated():
    service = TranslationService(AsyncMock())
    service.translate_to_english = AsyncMock(return_value=("The night", "fr"))
    post = make_post("La nuit")

    await service.enrich_title(post)

    service.translate_to_english.assert_awaited_once_with("La nuit")
    assert post.metadata["title_translation"] == "The night"


async def test_translation_response_is_decoded_and_cached():
    session = FakeSession(FakeResponse(200, {
        "responseData": {
            "translatedText": "What &amp; why?",
            "detectedLanguage": "ja",
        },
    }))
    service = TranslationService(session)

    first = await service.translate_to_english("何で？")
    second = await service.translate_to_english("何で？")

    assert first == second == ("What & why?", "ja")
    assert session.calls == 1


async def test_translation_http_and_network_failures_are_cached_as_no_result():
    http_session = FakeSession(FakeResponse(503, {}))
    network_session = FakeSession(error=aiohttp.ClientConnectionError("offline"))

    assert await TranslationService(http_session).translate_to_english("テスト") is None
    network_service = TranslationService(network_session)
    assert await network_service.translate_to_english("テスト") is None
    assert await network_service.translate_to_english("テスト") is None
    assert network_session.calls == 1
