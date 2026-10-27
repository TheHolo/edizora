"""Security regressions: all probes must pass after remediation.

Run explicitly: python -m pytest audit/test_security_regressions.py -rx
No Telegram calls, external requests, large allocations, or slow payloads.
"""

import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, UnidentifiedImageError

from app.domain.exceptions import MediaValidationError
from app.domain.models import DownloadedMedia, MediaItem
from app.news.api import NewsApiServer
from app.logging_config import SecretFilter
from app.services.album_thumbnails import make_thumbnail
from app.services.media_service import MediaService
from app.services.news_task_service import NewsTaskLeaseError, NewsTaskService


class AllocationAttempt(RuntimeError):
    pass


class ForbiddenPluginAttempt(RuntimeError):
    pass


def apng(disposal):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 22000, 22000, 8, 6, 0, 0, 0))
        + chunk(b'acTL', struct.pack('>II', 2, 0))
        + chunk(b'fcTL', struct.pack('>IIIIIHHBB', 0, 1, 1, 0, 0, 1, 10, disposal, 0))
        + chunk(b'IDAT', zlib.compress(b'\x00' * 5))
        + chunk(b'IEND', b'')
    )


def block_allocation(monkeypatch):
    def blocked(mode, size, *args, **kwargs):
        raise AllocationAttempt(f'Blocked native allocation: {mode}, {size}')
    monkeypatch.setattr(Image.core, 'fill', blocked)


async def test_oversized_apng_control_is_rejected_without_allocation(tmp_path, monkeypatch):
    block_allocation(monkeypatch)
    path = tmp_path / 'control.png'
    path.write_bytes(apng(0))
    media = DownloadedMedia(MediaItem('https://example.com/control.png', 'control.png', 0), path, 'image/png', path.stat().st_size)
    with pytest.raises(MediaValidationError):
        await MediaService().prepare(media, 'auto')


async def test_apng_disposal_is_rejected_before_large_allocation(tmp_path, monkeypatch):
    block_allocation(monkeypatch)
    path = tmp_path / 'disposal.png'
    path.write_bytes(apng(1))
    media = DownloadedMedia(MediaItem('https://example.com/disposal.png', 'disposal.png', 0), path, 'image/png', path.stat().st_size)
    with pytest.raises(MediaValidationError):
        await MediaService().prepare(media, 'auto')


def test_apng_thumbnail_rejects_before_large_allocation(tmp_path, monkeypatch):
    block_allocation(monkeypatch)
    path = tmp_path / 'disposal.png'
    path.write_bytes(apng(1))
    with pytest.raises((Image.DecompressionBombError, ValueError)):
        make_thumbnail(path)


@pytest.mark.parametrize('consumer', [MediaService._verify, make_thumbnail])
def test_unneeded_image_plugins_are_excluded_before_parsing(tmp_path, monkeypatch, consumer):
    Image.init()
    original, accept = Image.OPEN['XBM']

    def blocked(*args, **kwargs):
        raise ForbiddenPluginAttempt('XBM plugin invoked before format validation')

    monkeypatch.setitem(Image.OPEN, 'XBM', (blocked, accept))
    path = tmp_path / 'disguised.png'
    path.write_bytes(b'#define im_width 1\n#define im_height 1\nstatic char im_bits[] = {0};')
    with pytest.raises((MediaValidationError, UnidentifiedImageError)):
        consumer(path)


async def test_non_ascii_authorization_returns_401():
    server = NewsApiServer(tasks=SimpleNamespace(), token='audit-secret', host='127.0.0.1', port=0, default_lease_seconds=60)
    async with TestClient(TestServer(server.create_app())) as client:
        response = await client.get('/api/news/health', headers={'Authorization': 'Bearer \u00e9'})
        assert response.status == 401


async def test_non_ascii_lease_token_is_rejected_as_invalid_lease():
    task = SimpleNamespace(status='leased', lease_token='ascii-token')
    session = SimpleNamespace(scalar=AsyncMock(return_value=task))
    with pytest.raises(NewsTaskLeaseError):
        await NewsTaskService._leased_task(session, 1, '\u00e9')


def test_entire_cookie_header_is_redacted():
    redacted = SecretFilter.redact('Cookie: sid=first-secret; session=second-secret')
    assert 'first-secret' not in redacted
    assert 'second-secret' not in redacted
