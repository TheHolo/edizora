from unittest.mock import AsyncMock

import pytest

from app.domain.exceptions import InvalidMediaSelectionError, TooManyMediaError
from app.domain.models import MediaItem, SourcePost
from app.providers.registry import ProviderRegistry
from app.services.ingest_service import IngestService


class AlbumProvider:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self.fetch_post = AsyncMock(return_value=_post(name, count))

    def can_handle(self, url: str) -> bool:
        return url.startswith(f"https://{self.name}.test/")

    def normalize_url(self, url: str) -> str:
        return url


def _post(provider: str, count: int) -> SourcePost:
    return SourcePost(
        provider=provider,
        source_id="album",
        source_url=f"https://{provider}.test/album",
        normalized_url=f"https://{provider}.test/album",
        title="Album",
        author_name="Artist",
        author_url=f"https://{provider}.test/artist",
        media_items=[
            MediaItem(url=f"https://cdn.test/{number}.jpg", filename=f"{number}.jpg", order=number - 1)
            for number in range(1, count + 1)
        ],
        metadata={"page_count": count},
    )


def _service(provider: AlbumProvider, limit: int = 10) -> IngestService:
    return IngestService(
        ProviderRegistry([provider]), max_tags=20, max_tag_length=64,
        media_limit_enabled=True, max_images=limit,
    )


@pytest.mark.parametrize("provider_name", ["pixiv", "deviantart"])
async def test_selection_is_applied_before_album_limit(provider_name):
    provider = AlbumProvider(provider_name, 19)

    post = await _service(provider).fetch(
        f"https://{provider_name}.test/album [1,3,5,6,7,10]"
    )

    assert [item.filename for item in post.media_items] == [
        "1.jpg", "3.jpg", "5.jpg", "6.jpg", "7.jpg", "10.jpg",
    ]
    assert [item.order for item in post.media_items] == list(range(6))
    assert post.metadata["original_media_count"] == 19
    assert post.metadata["selected_media_numbers"] == [1, 3, 5, 6, 7, 10]


async def test_selector_over_limit_is_rejected_before_provider_request():
    provider = AlbumProvider("pixiv", 19)

    with pytest.raises(TooManyMediaError, match="Выбрано 11 изображений.*10"):
        await _service(provider).fetch("https://pixiv.test/album [1,2,3,4,5,6,7,8,9,10,11]")

    provider.fetch_post.assert_not_awaited()


async def test_missing_album_numbers_are_reported_after_fetch():
    provider = AlbumProvider("pixiv", 19)
    service = _service(provider)
    sources, _, _ = service.parse("https://pixiv.test/album [15-21]")

    with pytest.raises(InvalidMediaSelectionError, match=r"всего 19.*20, 21 отсутствуют"):
        await service.fetch(sources[0])


async def test_large_album_keeps_all_images_for_editor_and_defaults_to_first_ten():
    provider = AlbumProvider("pixiv", 19)

    post = await _service(provider).fetch("https://pixiv.test/album")
    assert len(post.media_items) == 10
    assert len(post.metadata["artwork_album"]["catalog"]) == 19
    assert post.metadata["artwork_album"]["automatic_selection"] is True
    assert post.metadata["selected_media_numbers"] == list(range(1, 11))


async def test_album_limit_can_still_be_disabled():
    provider = AlbumProvider("pixiv", 19)
    service = IngestService(
        ProviderRegistry([provider]), max_tags=20, max_tag_length=64,
        media_limit_enabled=False, max_images=10,
    )

    post = await service.fetch("https://pixiv.test/album")

    assert len(post.media_items) == 19


async def test_selection_is_rejected_for_non_album_provider():
    provider = AlbumProvider("direct", 1)

    with pytest.raises(InvalidMediaSelectionError, match="только для Pixiv и DeviantArt"):
        await _service(provider).fetch("https://direct.test/image [1]")

    provider.fetch_post.assert_not_awaited()
