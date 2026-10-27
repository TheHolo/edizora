import json
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPOSITORY_SETTING_FIELDS = {
    "storage_path",
    "log_level",
    "worker_count",
    "max_job_attempts",
    "download_timeout",
    "max_download_size_mb",
    "max_tags",
    "max_tag_length",
    "max_urls_per_message",
    "pixiv_media_limit_enabled",
    "pixiv_max_images",
    "auto_add_source_tags",
    "auto_translate_titles",
    "translation_timeout",
    "translation_lmstudio_timeout",
    "translation_fallback_mymemory",
    "delete_files_after_publish",
    "files_ttl_hours",
    "timezone",
    "news_api_host",
    "news_api_port",
    "news_task_lease_seconds",
    "news_model_name",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: Annotated[set[int], NoDecode] = Field(default_factory=set)
    database_url: str = "sqlite+aiosqlite:///./data/database.db"
    storage_path: Path = Path("./storage")
    log_level: str = "INFO"
    worker_count: int = Field(1, ge=1, le=8)
    max_job_attempts: int = Field(3, ge=1, le=10)
    download_timeout: int = Field(60, ge=5)
    max_download_size_mb: int = Field(47, ge=1, le=47)
    max_tags: int = Field(20, ge=1)
    max_tag_length: int = Field(64, ge=1)
    max_urls_per_message: int = Field(10, ge=1, le=50)
    default_channel_alias: str = "artwork"
    channels_json: dict[str, dict[str, Any]] = Field(default_factory=dict)
    http_proxy: str | None = None
    socks_proxy: str | None = None
    pixiv_cookies: str | None = None
    pixiv_media_limit_enabled: bool = True
    pixiv_max_images: int = Field(10, ge=1, le=1000)
    auto_add_source_tags: bool = True
    auto_translate_titles: bool = True
    translation_timeout: int = Field(5, ge=1, le=30)
    translation_lmstudio_timeout: int = Field(60, ge=1, le=600)
    translation_fallback_mymemory: bool = True
    delete_files_after_publish: bool = True
    files_ttl_hours: int = 24
    timezone: str = "UTC"
    news_worker_token: str | None = None
    news_api_bind_host: str | None = None
    news_api_host: str = "127.0.0.1"
    news_api_port: int = Field(8091, ge=1, le=65535)
    news_task_lease_seconds: int = Field(1800, ge=60, le=21600)
    news_model_name: str = "gemma4:12b"

    @property
    def news_api_enabled(self) -> bool:
        return bool(self.news_worker_token)

    def __init__(self, _config_file: str | Path = "bot-settings.toml", **values: Any) -> None:
        repository_values: dict[str, Any] = {}
        config_path = Path(_config_file)
        if config_path.is_file():
            with config_path.open("rb") as stream:
                document = tomllib.load(stream)
            section = document.get("bot", {})
            if not isinstance(section, dict):
                raise ValueError("Секция [bot] в bot-settings.toml должна быть таблицей TOML")
            unknown = set(section) - REPOSITORY_SETTING_FIELDS
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"Неизвестные или секретные параметры в bot-settings.toml: {names}")
            repository_values.update(section)
        super().__init__(**{**repository_values, **values})

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> set[int]:
        if isinstance(value, str):
            return {int(item.strip()) for item in value.split(",") if item.strip()}
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set, frozenset)):
            return {int(item) for item in value}
        raise ValueError("admin_ids must be a comma-separated string or a collection")

    @field_validator("channels_json", mode="before")
    @classmethod
    def parse_channels(cls, value: object) -> dict[str, dict[str, Any]]:
        parsed: object = json.loads(value) if isinstance(value, str) else value
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            # Pydantic validators must raise ValueError to produce a field validation error.
            raise ValueError("channels_json must be a JSON object")  # noqa: TRY004

        channels: dict[str, dict[str, Any]] = {}
        for alias, settings in parsed.items():
            if not isinstance(alias, str) or not isinstance(settings, dict):
                raise ValueError(  # noqa: TRY004
                    "each channel must have a string alias and an object value"
                )
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", alias):
                raise ValueError("channel alias must contain 1-64 Latin letters, digits, _ or -")
            chat_id = settings.get("chat_id")
            if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)) or not str(chat_id).strip():
                raise ValueError(f"channel {alias}: chat_id is required")
            if settings.get("publish_mode", "auto") not in ("auto", "photo", "document"):
                raise ValueError(f"channel {alias}: publish_mode must be auto, photo or document")
            interval = settings.get("publish_interval_seconds", 0)
            if isinstance(interval, bool) or not isinstance(interval, int) or interval < 0:
                raise ValueError(f"channel {alias}: publish_interval_seconds must be a non-negative integer")
            if "enabled" in settings and not isinstance(settings["enabled"], bool):
                raise ValueError(f"channel {alias}: enabled must be true or false")
            if "tags" in settings:
                from app.utils.tags import normalize_tags

                tags = settings["tags"]
                if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                    raise ValueError(f"channel {alias}: tags must be a list of strings")
                settings = {**settings, "tags": normalize_tags(tags)}
            channels[alias] = settings
        return channels

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown IANA timezone: {value}") from error
        return value

    @property
    def max_download_bytes(self) -> int:
        return self.max_download_size_mb * 1024 * 1024

    @property
    def proxy_url(self) -> str | None:
        return self.socks_proxy or self.http_proxy
