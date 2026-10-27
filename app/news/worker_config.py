from __future__ import annotations

import ipaddress
import socket
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HOME_WORKER_TOML_FIELDS = {
    "vps_api_url",
    "worker_id",
    "poll_interval_seconds",
    "lease_seconds",
    "request_timeout_seconds",
    "max_retries",
    "retry_backoff_seconds",
    "ollama_base_url",
    "ollama_model",
    "ollama_timeout_seconds",
    "ollama_max_retries",
    "ollama_keep_alive",
    "ollama_context_length",
    "ollama_max_predict_tokens",
    "llm_provider",
    "openai_base_url",
    "openai_model",
    "lmstudio_base_url",
    "lmstudio_model",
    "lmstudio_timeout_seconds",
    "lmstudio_context_length",
    "lmstudio_max_predict_tokens",
    "lmstudio_max_retries",
    "temperature",
    "max_source_chars_per_chunk",
    "max_source_chunks",
    "source_types",
    "log_level",
}


class HomeWorkerSettings(BaseSettings):
    """Settings for the process that runs on the user's home computer.

    The API token is intentionally accepted only from ``HOME_WORKER_TOKEN`` (or
    an explicit constructor argument). Non-secret tuning values may live in the
    ``[home_worker]`` section of the repository TOML file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="HOME_WORKER_",
        extra="ignore",
    )

    token: SecretStr
    vps_api_url: str = "http://127.0.0.1:8091"
    worker_id: str = Field(default_factory=socket.gethostname, min_length=1, max_length=128)
    poll_interval_seconds: float = Field(5.0, ge=0.1, le=300)
    lease_seconds: int = Field(1800, ge=60, le=7200)
    request_timeout_seconds: float = Field(30.0, ge=1, le=300)
    max_retries: int = Field(3, ge=0, le=10)
    retry_backoff_seconds: float = Field(1.0, ge=0, le=60)

    # Ollama must remain reachable only from the same host as this worker.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = Field("gemma4:12b", min_length=1, max_length=128)
    ollama_timeout_seconds: float = Field(600.0, ge=10, le=3600)
    ollama_max_retries: int = Field(2, ge=0, le=5)
    ollama_keep_alive: str = Field("10m", min_length=1, max_length=32)
    ollama_context_length: int = Field(8192, ge=2048, le=131072)
    ollama_max_predict_tokens: int = Field(1600, ge=256, le=8192)

    # Keep the standalone worker's Ollama default for existing installations.
    llm_provider: Literal["ollama", "lmstudio", "openai"] = "ollama"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = Field("", max_length=255)
    openai_api_key: SecretStr | None = None
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = Field("", max_length=128)
    lmstudio_api_key: SecretStr | None = None
    lmstudio_timeout_seconds: float = Field(600.0, ge=10, le=3600)
    lmstudio_context_length: int = Field(8192, ge=2048, le=131072)
    lmstudio_max_predict_tokens: int = Field(1600, ge=256, le=8192)
    lmstudio_max_retries: int = Field(2, ge=0, le=5)
    temperature: float = Field(0.1, ge=0, le=0.5)
    max_source_chars_per_chunk: int = Field(24000, ge=4000, le=200000)
    max_source_chunks: int = Field(16, ge=1, le=64)

    source_types: tuple[str, ...] = ("website", "youtube", "telegram", "manual")
    log_level: str = "INFO"

    def __init__(self, _config_file: str | Path = "bot-settings.toml", **values: Any) -> None:
        repository_values: dict[str, Any] = {}
        config_path = Path(_config_file)
        if config_path.is_file():
            with config_path.open("rb") as stream:
                document = tomllib.load(stream)
            section = document.get("home_worker", {})
            if not isinstance(section, dict):
                raise ValueError("Section [home_worker] must be a TOML table")
            unknown = set(section) - HOME_WORKER_TOML_FIELDS
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(
                    f"Unknown or secret settings in [home_worker]: {names}. "
                    "Keep HOME_WORKER_TOKEN in the environment."
                )
            repository_values.update(section)
        super().__init__(**{**repository_values, **values})

    @field_validator("vps_api_url")
    @classmethod
    def validate_vps_api_url(cls, value: str) -> str:
        parsed = _validated_base_url(value, "vps_api_url")
        host = parsed.hostname or ""
        if parsed.scheme != "https" and not _is_loopback_host(host):
            raise ValueError("vps_api_url must use HTTPS unless it points to localhost")
        return value.rstrip("/")

    @field_validator("ollama_base_url")
    @classmethod
    def validate_local_ollama_url(cls, value: str) -> str:
        parsed = _validated_base_url(value, "ollama_base_url")
        if not _is_loopback_host(parsed.hostname or ""):
            raise ValueError(
                "ollama_base_url must point to localhost; never expose Ollama to the network"
            )
        return value.rstrip("/")

    @field_validator("lmstudio_base_url")
    @classmethod
    def validate_local_lmstudio_url(cls, value: str) -> str:
        parsed = _validated_base_url(value, "lmstudio_base_url")
        if not _is_loopback_host(parsed.hostname or ""):
            raise ValueError("lmstudio_base_url must point to localhost")
        if parsed.path.rstrip("/") not in {"", "/v1"}:
            raise ValueError("lmstudio_base_url must end with /v1 or contain only the server address")
        return value.rstrip("/") if parsed.path.rstrip("/") == "/v1" else value.rstrip("/") + "/v1"

    @field_validator("lmstudio_model", "ollama_model", mode="before")
    @classmethod
    def strip_model_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("HOME_WORKER_TOKEN must not be empty")
        return value

    @property
    def model_name(self) -> str:
        if self.llm_provider == "openai":
            return self.openai_model
        return self.lmstudio_model if self.llm_provider == "lmstudio" else self.ollama_model

    @field_validator("openai_base_url")
    @classmethod
    def validate_openai_url(cls, value: str) -> str:
        parsed = _validated_base_url(value, "openai_base_url")
        if parsed.scheme != "https" and not _is_loopback_host(parsed.hostname or ""):
            # An explicitly configured private-network endpoint may use HTTP.
            # Public endpoints must use TLS.
            try:
                local = ipaddress.ip_address(parsed.hostname or "").is_private
            except ValueError:
                local = False
            if not local:
                raise ValueError("Облачный AI требует HTTPS; HTTP разрешён для локальных адресов")
        return value.rstrip("/")

    @field_validator("source_types")
    @classmethod
    def validate_source_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        supported = {"website", "youtube", "telegram", "manual"}
        normalized = tuple(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        unknown = set(normalized) - supported
        if unknown:
            raise ValueError(f"unsupported source types: {', '.join(sorted(unknown))}")
        if not normalized:
            raise ValueError("at least one source type is required")
        return normalized

    @model_validator(mode="after")
    def validate_total_source_budget(self) -> HomeWorkerSettings:
        if self.max_source_chars_per_chunk * self.max_source_chunks > 2_000_000:
            raise ValueError(
                "max_source_chars_per_chunk * max_source_chunks must not exceed 2000000"
            )
        return self


def _validated_base_url(value: str, field_name: str):
    parsed = urlsplit(value)
    # Accessing .port also validates non-numeric and out-of-range ports.
    if parsed.port == 0:
        raise ValueError(f"{field_name} must use a non-zero port")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain credentials, query, or fragment")
    return parsed


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
