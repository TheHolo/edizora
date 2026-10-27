from __future__ import annotations

import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from app.domain.exceptions import InvalidUrlError
from app.news.errors import (
    NewsContentTooLargeError,
    NewsExtractionError,
    NewsSourceAccessError,
    NewsSourceNotFoundError,
    NewsSourceRateLimitedError,
    NewsSourceUnavailableError,
    UnsafeNewsUrlError,
)
from app.utils.urls import ensure_public_dns, validate_public_url

DnsValidator = Callable[[str], Awaitable[None]]


class PublicOnlyResolver(AbstractResolver):
    """Reject non-public addresses in the same DNS result aiohttp connects to.

    The resolver closes the DNS-rebinding gap left by a separate preflight
    lookup. Pass it to ``aiohttp.TCPConnector(resolver=...)`` for every session
    that fetches user-supplied news URLs.
    """

    def __init__(self, resolver: AbstractResolver | None = None) -> None:
        self._resolver = resolver or DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[Any]:
        direct_address = _ip_address(host)
        if direct_address is not None and not _is_public_address(direct_address):
            raise OSError(f"DNS target is not public: {host}")

        results = await self._resolver.resolve(host, port, family)
        if not results:
            raise OSError(f"DNS returned no addresses for {host}")
        for result in results:
            address = _ip_address(str(result["host"]))
            if address is None or not _is_public_address(address):
                raise OSError(f"DNS target is not public: {result['host']}")
        return results

    async def close(self) -> None:
        await self._resolver.close()


@dataclass(slots=True, frozen=True)
class FetchedText:
    requested_url: str
    final_url: str
    status: int
    content_type: str | None
    text: str
    headers: Mapping[str, str]


class SafeHttpFetcher:
    """A small HTTP reader with explicit redirect and response limits.

    DNS is checked before every request, including redirect targets. Consumers
    must still apply the same policy when downloading media at a later time.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        max_bytes: int = 4 * 1024 * 1024,
        max_redirects: int = 5,
        timeout_seconds: float = 20,
        dns_validator: DnsValidator = ensure_public_dns,
        user_agent: str = "Edizora/0.1 (+private editorial bot)",
    ) -> None:
        if max_bytes <= 0 or max_redirects < 0 or timeout_seconds <= 0:
            raise ValueError("HTTP fetch limits must be positive")
        self._session = session
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._dns_validator = dns_validator
        self._user_agent = user_agent

    async def ensure_safe_url(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
    ) -> str:
        try:
            validate_public_url(url, set(allowed_hosts) if allowed_hosts else None)
            parsed = urlsplit(url)
            if parsed.port not in {None, 80, 443}:
                raise InvalidUrlError("Разрешены только стандартные HTTP-порты")
            await self._dns_validator(url)
        except (InvalidUrlError, OSError, ValueError) as exc:
            raise UnsafeNewsUrlError(
                "Ссылка ведёт на локальный или служебный адрес"
            ) from exc
        return url

    async def fetch_text(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        allowed_content_types: Collection[str] = (
            "text/html",
            "application/xhtml+xml",
            "text/plain",
        ),
        accept: str = "text/html,application/xhtml+xml,text/plain;q=0.8",
    ) -> FetchedText:
        requested_url = url
        current_url = url
        headers = {
            "Accept": accept,
            "Accept-Language": "ru,en;q=0.8",
            "User-Agent": self._user_agent,
        }
        for redirect_number in range(self._max_redirects + 1):
            await self.ensure_safe_url(current_url, allowed_hosts=allowed_hosts)
            try:
                async with self._session.get(
                    current_url,
                    allow_redirects=False,
                    headers=headers,
                    timeout=self._timeout,
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect_number >= self._max_redirects:
                            raise NewsExtractionError(
                                "Источник перенаправляет запрос слишком много раз"
                            )
                        location = response.headers.get("Location")
                        if not location:
                            raise NewsExtractionError(
                                "Источник вернул перенаправление без адреса"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    self._raise_for_status(response.status)
                    content_type = _content_type(response.headers.get("Content-Type"))
                    if content_type and content_type not in set(allowed_content_types):
                        raise NewsExtractionError(
                            f"Источник вернул неподдерживаемый Content-Type: {content_type}"
                        )
                    declared_size = _content_length(
                        response.headers.get("Content-Length")
                    )
                    if declared_size is not None and declared_size > self._max_bytes:
                        raise NewsContentTooLargeError(
                            "Текст источника превышает допустимый размер"
                        )
                    body = await self._read_limited(response)
                    charset = _charset(response.headers.get("Content-Type")) or "utf-8"
                    try:
                        text = body.decode(charset)
                    except (LookupError, UnicodeDecodeError):
                        text = body.decode("utf-8", errors="replace")
                    return FetchedText(
                        requested_url=requested_url,
                        final_url=current_url,
                        status=response.status,
                        content_type=content_type,
                        text=text,
                        headers=dict(response.headers),
                    )
            except NewsExtractionError:
                raise
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                raise NewsSourceUnavailableError(
                    "Не удалось загрузить источник новости"
                ) from exc
        raise NewsExtractionError("Источник перенаправляет запрос слишком много раз")

    async def _read_limited(self, response: aiohttp.ClientResponse) -> bytes:
        result = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            result.extend(chunk)
            if len(result) > self._max_bytes:
                raise NewsContentTooLargeError(
                    "Текст источника превышает допустимый размер"
                )
        return bytes(result)

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status in {404, 410}:
            raise NewsSourceNotFoundError("Источник новости не найден")
        if status in {401, 403, 451}:
            raise NewsSourceAccessError("Источник не предоставил доступ к публикации")
        if status == 429:
            raise NewsSourceRateLimitedError("Источник временно ограничил запросы")
        if status >= 500:
            raise NewsSourceUnavailableError(
                f"Источник временно недоступен: HTTP {status}"
            )
        if status < 200 or status >= 300:
            raise NewsExtractionError(f"Источник вернул HTTP {status}")


def _content_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def _charset(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";")[1:]:
        key, separator, candidate = part.partition("=")
        if separator and key.strip().lower() == "charset":
            return candidate.strip().strip("\"'") or None
    return None


def _content_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result >= 0 else None


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def _is_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )
