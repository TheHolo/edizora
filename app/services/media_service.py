import asyncio
import math
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.domain.exceptions import MediaTooLargeError, MediaValidationError
from app.domain.models import DownloadedMedia, PreparedMedia

PHOTO_MAX_BYTES = 10_000_000
PHOTO_TARGET_BYTES = 9_500_000
PHOTO_MAX_DIMENSION_SUM = 10_000
PHOTO_TARGET_DIMENSION_SUM = 9_800
PHOTO_MAX_ASPECT_RATIO = 20
PHOTO_FORMATS = {"JPEG", "PNG", "WEBP"}
SUPPORTED_IMAGE_FORMATS = PHOTO_FORMATS | {"GIF"}
MAX_IMAGE_PIXELS = 80_000_000
DOCUMENT_MAX_BYTES = 49_000_000


class MediaService:
    async def prepare(self, media: DownloadedMedia, mode: str) -> PreparedMedia:
        try:
            width, height, fmt = await asyncio.to_thread(self._verify, media.path)
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise MediaValidationError("Изображение повреждено или имеет неизвестный формат") from error
        media.width, media.height = width, height
        if mode == "document":
            self._validate_document_size(media.size)
            return PreparedMedia(
                media.path, as_document=True, order=media.source.order,
                media_type=media.source.media_type,
            )

        aspect_ratio = max(width / height, height / width)
        photo_compatible = fmt in PHOTO_FORMATS and aspect_ratio <= PHOTO_MAX_ASPECT_RATIO
        within_limits = media.size <= PHOTO_MAX_BYTES and width + height <= PHOTO_MAX_DIMENSION_SUM
        if photo_compatible and within_limits:
            return PreparedMedia(
                media.path, as_document=False, order=media.source.order,
                media_type=media.source.media_type,
            )

        if photo_compatible:
            prepared_path = await asyncio.to_thread(self._fit_for_telegram_photo, media.path)
            if prepared_path is not None:
                return PreparedMedia(
                    prepared_path, as_document=False, order=media.source.order,
                    media_type=media.source.media_type,
                )

        self._validate_document_size(media.size)
        return PreparedMedia(
            media.path, as_document=True, order=media.source.order,
            media_type=media.source.media_type,
        )

    @staticmethod
    def _validate_document_size(size: int) -> None:
        if size > DOCUMENT_MAX_BYTES:
            raise MediaTooLargeError("Файл превышает лимит Telegram для документов")

    @staticmethod
    def _verify(path: Path) -> tuple[int, int, str | None]:
        with Image.open(path, formats=sorted(SUPPORTED_IMAGE_FORMATS)) as image:
            if image.format not in SUPPORTED_IMAGE_FORMATS:
                raise MediaValidationError(f"Формат изображения {image.format or 'неизвестен'} не поддерживается")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise MediaValidationError("Изображение превышает допустимое количество пикселей")
            image.verify()
        with Image.open(path, formats=sorted(SUPPORTED_IMAGE_FORMATS)) as image:
            return image.width, image.height, image.format

    @staticmethod
    def _fit_for_telegram_photo(path: Path) -> Path | None:
        target = path.with_name(f"{path.stem}_telegram.jpg")
        try:
            with Image.open(path, formats=sorted(SUPPORTED_IMAGE_FORMATS)) as source:
                image = ImageOps.exif_transpose(source)
                if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")

                if sum(image.size) > PHOTO_TARGET_DIMENSION_SUM:
                    scale = PHOTO_TARGET_DIMENSION_SUM / sum(image.size)
                    image = image.resize(
                        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                        Image.Resampling.LANCZOS,
                    )

                for attempt in range(6):
                    quality = max(67, 92 - attempt * 5)
                    image.save(target, "JPEG", quality=quality, optimize=True, progressive=True)
                    size = target.stat().st_size
                    if size <= PHOTO_TARGET_BYTES:
                        return target
                    scale = min(0.9, math.sqrt(PHOTO_TARGET_BYTES / size) * 0.95)
                    image = image.resize(
                        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
        except (OSError, UnidentifiedImageError):
            target.unlink(missing_ok=True)
            return None
        target.unlink(missing_ok=True)
        return None
