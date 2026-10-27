import pytest
from PIL import Image

from app.domain.exceptions import MediaTooLargeError, MediaValidationError
from app.domain.models import DownloadedMedia, MediaItem
from app.services import media_service
from app.services.media_service import MediaService


async def test_oversized_supported_image_is_converted_to_telegram_photo(tmp_path, monkeypatch):
    source_path = tmp_path / "large.png"
    Image.effect_noise((300, 300), 100).convert("RGB").save(source_path, "PNG")
    source = MediaItem(url="https://example.com/large.png", filename="large.png", order=0)
    downloaded = DownloadedMedia(source, source_path, "image/png", source_path.stat().st_size)
    monkeypatch.setattr(media_service, "PHOTO_MAX_BYTES", 1_000)
    monkeypatch.setattr(media_service, "PHOTO_TARGET_BYTES", 8_000)

    prepared = await MediaService().prepare(downloaded, "auto")

    assert prepared.as_document is False
    assert prepared.path != source_path
    assert prepared.path.suffix == ".jpg"
    assert prepared.path.stat().st_size <= 8_000


async def test_document_mode_keeps_original_file(tmp_path):
    source_path = tmp_path / "original.png"
    Image.new("RGB", (100, 100), "red").save(source_path, "PNG")
    source = MediaItem(url="https://example.com/original.png", filename="original.png", order=0)
    downloaded = DownloadedMedia(source, source_path, "image/png", source_path.stat().st_size)

    prepared = await MediaService().prepare(downloaded, "document")

    assert prepared.as_document is True
    assert prepared.path == source_path


async def test_unsupported_decoded_format_is_rejected(tmp_path):
    source_path = tmp_path / "disguised.jpg"
    Image.new("RGB", (10, 10), "blue").save(source_path, "BMP")
    source = MediaItem(url="https://example.com/disguised.jpg", filename="disguised.jpg", order=0)
    downloaded = DownloadedMedia(source, source_path, "image/jpeg", source_path.stat().st_size)

    # Unsupported decoders must not run merely to determine their format name.
    with pytest.raises(MediaValidationError, match="формат"):
        await MediaService().prepare(downloaded, "auto")


async def test_excessive_pixel_count_is_rejected(tmp_path, monkeypatch):
    source_path = tmp_path / "large-dimensions.png"
    Image.new("RGB", (100, 100), "blue").save(source_path, "PNG")
    source = MediaItem(url="https://example.com/large.png", filename="large.png", order=0)
    downloaded = DownloadedMedia(source, source_path, "image/png", source_path.stat().st_size)
    monkeypatch.setattr(media_service, "MAX_IMAGE_PIXELS", 9_999)

    with pytest.raises(MediaValidationError, match="количество пикселей"):
        await MediaService().prepare(downloaded, "auto")


async def test_oversized_document_is_rejected_before_publish(tmp_path, monkeypatch):
    source_path = tmp_path / "large-document.png"
    Image.new("RGB", (10, 10), "blue").save(source_path, "PNG")
    source = MediaItem(url="https://example.com/large.png", filename="large.png", order=0)
    downloaded = DownloadedMedia(source, source_path, "image/png", 101)
    monkeypatch.setattr(media_service, "DOCUMENT_MAX_BYTES", 100)

    with pytest.raises(MediaTooLargeError, match="лимит Telegram"):
        await MediaService().prepare(downloaded, "document")
