from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.news.errors import InvalidNewsInputError


class NewsSourceKind(StrEnum):
    WEBSITE = "website"
    YOUTUBE = "youtube"
    TELEGRAM = "telegram"
    MANUAL = "manual"


class NewsMediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    ANIMATION = "animation"
    DOCUMENT = "document"


class ExtractionStage(StrEnum):
    EXTRACTING_CONTENT = "extracting_content"
    EXTRACTING_METADATA = "extracting_metadata"
    EXTRACTING_SUBTITLES = "extracting_subtitles"
    TRANSCRIBING = "transcribing"


@dataclass(slots=True, frozen=True)
class ExtractionProgress:
    stage: ExtractionStage
    message: str


ProgressCallback = Callable[[ExtractionProgress], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class NewsSourceRequest:
    kind: NewsSourceKind
    value: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "NewsSourceRequest":
        try:
            kind = NewsSourceKind(str(payload["kind"]))
        except (KeyError, ValueError) as exc:
            raise InvalidNewsInputError(
                "Не указан корректный тип источника новости"
            ) from exc

        if kind is NewsSourceKind.MANUAL:
            candidates = ("raw_text", "source_text", "text", "value")
        elif kind is NewsSourceKind.TELEGRAM:
            candidates = (
                "source_url",
                "url",
                "source_text",
                "raw_text",
                "text",
                "value",
            )
        else:
            candidates = ("source_url", "url", "value")
        value = next(
            (str(payload[key]).strip() for key in candidates if payload.get(key)),
            "",
        )
        if not value:
            raise InvalidNewsInputError("Источник новости пуст")
        return cls(kind=kind, value=value)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "value": self.value}


@dataclass(slots=True)
class NewsMedia:
    kind: NewsMediaKind
    url: str | None = None
    telegram_file_id: str | None = None
    preview_url: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url and not self.telegram_file_id:
            raise ValueError("NewsMedia requires url or telegram_file_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "url": self.url,
            "telegram_file_id": self.telegram_file_id,
            "preview_url": self.preview_url,
            "mime_type": self.mime_type,
            "filename": self.filename,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "metadata": _json_safe(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NewsMedia":
        return cls(
            kind=NewsMediaKind(str(payload["kind"])),
            url=_optional_str(payload.get("url")),
            telegram_file_id=_optional_str(payload.get("telegram_file_id")),
            preview_url=_optional_str(payload.get("preview_url")),
            mime_type=_optional_str(payload.get("mime_type")),
            filename=_optional_str(payload.get("filename")),
            width=_optional_int(payload.get("width")),
            height=_optional_int(payload.get("height")),
            duration=_optional_int(payload.get("duration")),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class ExtractedNewsSource:
    kind: NewsSourceKind
    source_id: str
    source_url: str | None
    normalized_url: str | None
    title: str
    raw_text: str
    author_name: str | None = None
    author_url: str | None = None
    published_at: datetime | None = None
    media: list[NewsMedia] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "normalized_url": self.normalized_url,
            "title": self.title,
            "raw_text": self.raw_text,
            "author_name": self.author_name,
            "author_url": self.author_url,
            "published_at": self.published_at.isoformat()
            if self.published_at
            else None,
            "media": [item.to_dict() for item in self.media],
            "metadata": _json_safe(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExtractedNewsSource":
        published_at = payload.get("published_at")
        return cls(
            kind=NewsSourceKind(str(payload["kind"])),
            source_id=str(payload["source_id"]),
            source_url=_optional_str(payload.get("source_url")),
            normalized_url=_optional_str(payload.get("normalized_url")),
            title=str(payload.get("title") or ""),
            raw_text=str(payload.get("raw_text") or ""),
            author_name=_optional_str(payload.get("author_name")),
            author_url=_optional_str(payload.get("author_url")),
            published_at=(
                datetime.fromisoformat(str(published_at)) if published_at else None
            ),
            media=[NewsMedia.from_dict(item) for item in payload.get("media") or []],
            metadata=dict(payload.get("metadata") or {}),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)
