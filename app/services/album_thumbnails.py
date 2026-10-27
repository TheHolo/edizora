import asyncio
import hashlib
import logging
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from app.domain.album import AlbumState
from app.domain.enums import MediaType
from app.domain.exceptions import ApplicationError
from app.domain.models import MediaItem
from app.services.download_service import DownloadService

logger = logging.getLogger(__name__)
PAGE_SIZE = 9
TILE = 280


@dataclass(frozen=True)
class AlbumPage:
    image: bytes
    page: int
    pages: int
    numbers: list[int]
    unavailable: int


class AlbumThumbnails:
    def __init__(self, downloader: DownloadService, *, timeout: float = 12, cache_size: int = 256) -> None:
        self.downloader = downloader
        self.timeout = timeout
        self.cache_size = cache_size
        self.cache: OrderedDict[str, bytes] = OrderedDict()
        self._parallel = asyncio.Semaphore(3)

    async def page(self, job_id: int, album: AlbumState, page: int = 0) -> AlbumPage:
        numbers = sorted(album.catalog)
        pages = max(1, (len(numbers) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, page), pages - 1)
        numbers = numbers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        thumbnails = await asyncio.gather(*(self._thumbnail(job_id, album.catalog[n]) for n in numbers))
        content = await asyncio.to_thread(render_sheet, numbers, thumbnails, album.selection)
        return AlbumPage(content, page, pages, numbers, sum(t is None for t in thumbnails))

    async def _thumbnail(self, job_id: int, item: dict) -> bytes | None:
        url = item.get("preview_url") or item["url"]
        key = hashlib.sha256((url + repr(sorted(item.get("headers", {}).items()))).encode()).hexdigest()
        async with self._parallel:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            temporary_id = f"preview-album-{int(job_id)}-{uuid4().hex}"
            try:
                media = MediaItem(**{
                    **item, "url": url, "media_type": MediaType(item.get("media_type", "image")),
                })
                async with asyncio.timeout(self.timeout):
                    downloaded = await self.downloader.download(temporary_id, media)
                content = await asyncio.to_thread(make_thumbnail, downloaded.path)
                self.cache[key] = content
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
                return content
            except (ApplicationError, TimeoutError, OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
                logger.info("album_thumbnail_unavailable job_id=%s", job_id)
                return None
            finally:
                shutil.rmtree(self.downloader.storage / "jobs" / temporary_id, ignore_errors=True)


def make_thumbnail(path) -> bytes:
    with Image.open(path, formats=["JPEG", "PNG", "WEBP", "GIF"]) as original:
        if original.width * original.height > 24_000_000:
            raise ValueError("Thumbnail source has too many pixels")
        original.seek(0)
        oriented = ImageOps.exif_transpose(original)
        oriented.thumbnail((TILE - 24, TILE - 68))
        canvas = Image.new("RGB", oriented.size, "#ffffff")
        rgba = oriented.convert("RGBA")
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        stream = BytesIO()
        canvas.save(stream, "JPEG", quality=82)
        return stream.getvalue()


def render_sheet(numbers: list[int], thumbnails: list[bytes | None], selection: list[int]) -> bytes:
    rows = max(1, (len(numbers) + 2) // 3)
    sheet = Image.new("RGB", (3 * TILE + 32, rows * TILE + 32), "#101722")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    small = ImageFont.load_default(size=16)
    positions = {n: i + 1 for i, n in enumerate(selection)}
    for i, (number, thumbnail) in enumerate(zip(numbers, thumbnails, strict=True)):
        x, y = 16 + (i % 3) * TILE, 16 + (i // 3) * TILE
        selected = number in positions
        color = "#5ee5af" if selected else "#43516a"
        draw.rounded_rectangle((x + 3, y + 3, x + TILE - 7, y + TILE - 7), radius=12, fill="#1d293a", outline=color, width=3)
        if thumbnail:
            with Image.open(BytesIO(thumbnail), formats=["JPEG"]) as image:
                sheet.paste(image, (x + (TILE - image.width) // 2 - 2, y + 12 + (TILE - 68 - image.height) // 2))
        else:
            draw.text((x + 35, y + 104), "Preview unavailable", font=small, fill="#a6b2c5")
        draw.text((x + 16, y + TILE - 49), f"#{number}", font=font, fill="#ffffff")
        label = "FIRST / 1" if positions.get(number) == 1 else f"ORDER {positions[number]}" if selected else "EXCLUDED"
        draw.text((x + 110, y + TILE - 45), label, font=small, fill=color if selected else "#a6b2c5")
    stream = BytesIO()
    sheet.save(stream, "JPEG", quality=85)
    return stream.getvalue()
