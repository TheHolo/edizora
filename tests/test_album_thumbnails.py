from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from app.domain.album import initialize_album, read_album
from app.domain.exceptions import DownloadError
from app.services.album_thumbnails import AlbumThumbnails, TILE, make_thumbnail
from app.services.job_service import serialize_post
from test_ingest_service import _post


async def test_thumbnails_use_preview_urls_cache_successes_and_preserve_numbers(tmp_path):
    original = tmp_path / "source.png"
    Image.new("RGB", (100, 220), "red").save(original)
    post = _post("pixiv", 12)
    for i, item in enumerate(post.media_items, 1):
        item.preview_url = f"https://cdn.test/preview-{i}.jpg"
    initialize_album(post, [12, 1, 7])
    album = read_album(serialize_post(post))
    downloader = SimpleNamespace(storage=tmp_path, download=AsyncMock(return_value=SimpleNamespace(path=original)))
    service = AlbumThumbnails(downloader)
    first = await service.page(1, album, 0)
    assert first.numbers == list(range(1, 10))
    assert downloader.download.await_args_list[0].args[1].url == "https://cdn.test/preview-1.jpg"
    await service.page(1, album, 0)
    assert downloader.download.await_count == 9
    second = await service.page(1, album, 99)
    assert second.page == 1 and second.numbers == [10, 11, 12]
    with Image.open(BytesIO(first.image)) as sheet:
        assert sheet.size == (TILE * 3 + 32, TILE * 3 + 32)
    with Image.open(BytesIO(second.image)) as sheet:
        assert sheet.size == (TILE * 3 + 32, TILE + 32)


async def test_unavailable_previews_produce_placeholders_without_losing_selection(tmp_path):
    post = _post("pixiv", 3)
    initialize_album(post, [3, 1])
    album = read_album(serialize_post(post))
    downloader = SimpleNamespace(storage=tmp_path, download=AsyncMock(side_effect=DownloadError("offline")))
    page = await AlbumThumbnails(downloader).page(1, album)
    assert page.unavailable == 3 and page.numbers == [1, 2, 3]
    assert album.selection == [3, 1]
    assert page.image.startswith(b"\xff\xd8")


def test_thumbnail_keeps_entire_portrait_and_original_file(tmp_path):
    original = tmp_path / "portrait.png"
    image = Image.new("RGBA", (100, 400), (255, 0, 0, 255))
    image.save(original)
    before = original.read_bytes()
    thumbnail = make_thumbnail(original)
    with Image.open(BytesIO(thumbnail)) as reduced:
        assert reduced.height == TILE - 68
        assert reduced.width * 4 == reduced.height
    assert original.read_bytes() == before
