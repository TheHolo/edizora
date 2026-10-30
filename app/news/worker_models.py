from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.news.models import ExtractedNewsSource, NewsSourceKind


class NewsTaskInput(BaseModel):
    """Evolvable input envelope sent by the VPS to a home worker."""

    model_config = ConfigDict(extra="allow")

    kind: NewsSourceKind
    source_url: str | None = None
    source_text: str | None = None
    telegram: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_url", "source_text")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        stripped = value.strip() if value is not None else None
        return stripped or None

    @model_validator(mode="after")
    def validate_required_source(self) -> NewsTaskInput:
        if self.kind in {NewsSourceKind.WEBSITE, NewsSourceKind.YOUTUBE} and not self.source_url:
            raise ValueError(f"{self.kind.value} task requires source_url")
        if self.kind == NewsSourceKind.MANUAL and not self.source_text:
            raise ValueError("manual task requires source_text")
        if (
            self.kind == NewsSourceKind.TELEGRAM
            and not self.source_url
            and not self.source_text
            and not self.telegram
        ):
            raise ValueError("telegram task requires source_url, source_text, or telegram payload")
        return self

    def extraction_value(self) -> str:
        if self.source_text:
            return self.source_text
        if self.source_url:
            return self.source_url
        if self.telegram:
            text = self.telegram.get("text") or self.telegram.get("caption")
            if isinstance(text, str) and text.strip():
                return text.strip()
        raise ValueError("task does not contain an extractable value")


class NewsTask(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: StrictInt | StrictStr
    lease_token: str = Field(min_length=1, max_length=512)
    input_payload: NewsTaskInput
    model_name: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_flat_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "input_payload" in value:
            return value
        kind = value.get("kind") or value.get("source_type")
        if kind is None:
            return value
        copied = dict(value)
        copied["input_payload"] = {
            key: value[key]
            for key in ("source_url", "source_text", "telegram", "metadata")
            if key in value
        } | {"kind": kind}
        return copied


class NewsDraft(BaseModel):
    """Only model-produced fields; attribution is always added by application code."""

    model_config = ConfigDict(extra="forbid", strict=True)

    headline: str = Field(min_length=1, max_length=160)
    lead: str = Field(default="", max_length=400)
    # Leave deterministic room for the headline, lead, attribution and hashtags
    # inside Telegram's 4096-character post limit.
    body: str = Field(min_length=1, max_length=2800)
    suggested_tags: list[str] = Field(default_factory=list, max_length=5)
    facts_used: list[str] = Field(default_factory=list, max_length=30)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("headline", "lead", "body", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("suggested_tags")
    @classmethod
    def normalize_tags(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            tag = item.strip().lstrip("#").replace(" ", "_")
            if not tag or len(tag) > 64:
                raise ValueError("suggested tags must contain 1-64 characters")
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result

    @field_validator("facts_used", "warnings")
    @classmethod
    def validate_short_lists(cls, value: list[str]) -> list[str]:
        result = [item.strip() for item in value]
        if any(not item or len(item) > 500 for item in result):
            raise ValueError("facts and warnings must contain 1-500 characters")
        return result


class WorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: ExtractedNewsSource
    draft: NewsDraft

    @field_validator("source", mode="before")
    @classmethod
    def parse_source(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            value = ExtractedNewsSource.from_dict(value)
        if isinstance(value, ExtractedNewsSource) and (
            not value.source_id.strip() or not value.raw_text.strip()
        ):
            raise ValueError("source_id and raw_text must not be empty")
        return value


class ProgressStage(StrEnum):
    EXTRACTING_METADATA = "extracting_metadata"
    EXTRACTING_CONTENT = "extracting_content"
    EXTRACTING_SUBTITLES = "extracting_subtitles"
    TRANSCRIBING = "transcribing"
    LLM_PROCESSING = "llm_processing"
    VALIDATING = "validating"


class FactBundle(BaseModel):
    """Compact evidence extracted from one chunk of a long source."""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str = Field(min_length=1, max_length=1200)
    facts: list[str] = Field(default_factory=list, max_length=40)
    names: list[str] = Field(default_factory=list, max_length=30)
    dates: list[str] = Field(default_factory=list, max_length=30)
    numbers: list[str] = Field(default_factory=list, max_length=30)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("summary", mode="before")
    @classmethod
    def strip_summary(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("facts", "names", "dates", "numbers", "warnings")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        result = [item.strip() for item in value]
        if any(not item or len(item) > 500 for item in result):
            raise ValueError("fact bundle items must contain 1-500 characters")
        return result


def _split_long_list_items(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    result = []
    for item in value:
        if not isinstance(item, str):
            result.append(item)
            continue
        remaining = item.strip()
        while len(remaining) > 500:
            boundary = max(remaining.rfind("\n", 0, 501), remaining.rfind(" ", 0, 501))
            if boundary < 250:
                boundary = 500
            result.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        result.append(remaining)
    return result


class VideoFactBundle(FactBundle):
    """Preserve verbose model output while respecting the existing list-item limits."""

    @field_validator("facts", "names", "dates", "numbers", "warnings", mode="before")
    @classmethod
    def split_long_items(cls, value: Any) -> Any:
        return _split_long_list_items(value)


class VideoNewsDraft(NewsDraft):
    @field_validator("facts_used", "warnings", mode="before")
    @classmethod
    def split_long_items(cls, value: Any) -> Any:
        return _split_long_list_items(value)
