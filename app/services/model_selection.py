"""Shared, persistent LM Studio selection; no credentials are written here."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from app.news.llm_factory import list_model_ids
from app.news.lmstudio_client import check_lmstudio_status
from app.news.worker_config import HomeWorkerSettings
from app.news.worker_errors import LlmError

logger = logging.getLogger(__name__)


def valid_model_id(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 128
        and value == value.strip() and all(ord(char) >= 32 for char in value)
    )


@dataclass(frozen=True)
class LocalModel:
    id: str
    loaded: bool | None = None

    @property
    def token(self) -> str:
        # Model IDs can exceed Telegram's 64-byte callback_data limit.
        return hashlib.sha256(self.id.encode()).hexdigest()[:24]


class ModelSelection:
    def __init__(self, storage: Path, settings: HomeWorkerSettings) -> None:
        self.path = storage / "lmstudio-model.json"
        self.settings = settings

    @property
    def selected(self) -> str:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and payload.get("base_url") == self.settings.lmstudio_base_url
                and valid_model_id(payload.get("model"))
            ):
                return payload["model"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("lmstudio_selection_unreadable; using_configured_model")
        return self.settings.lmstudio_model

    def worker_settings(self) -> HomeWorkerSettings:
        return self.settings.model_copy(update={"lmstudio_model": self.selected})

    def save(self, model_id: str) -> None:
        if not valid_model_id(model_id):
            raise ValueError("Некорректный ID модели LM Studio")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".lmstudio-model-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"base_url": self.settings.lmstudio_base_url, "model": model_id}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class LMStudioModels:
    def __init__(self, session: aiohttp.ClientSession, selection: ModelSelection) -> None:
        self.session = session
        self.selection = selection

    async def list(self) -> list[LocalModel]:
        settings = self.selection.settings
        headers = {"Accept": "application/json"}
        if settings.lmstudio_api_key:
            headers["Authorization"] = f"Bearer {settings.lmstudio_api_key.get_secret_value()}"
        response = None
        try:
            # The native catalog distinguishes LLMs from embedding models and
            # lists installed models even when none is currently loaded.
            response = await self.session.request(
                "GET", f"{settings.lmstudio_base_url.removesuffix('/v1')}/api/v1/models",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
            )
            if response.status in (404, 405):
                response.release()
                response = None
                ids = await list_model_ids(
                    self.session, settings.model_copy(update={"llm_provider": "lmstudio"}),
                )
                # Older OpenAI-compatible catalogs do not expose model type.
                return [LocalModel(value) for value in ids if valid_model_id(value)
                        and not any(marker in value.casefold() for marker in ("embed", "rerank"))]
            check_lmstudio_status(response.status)
            payload = await response.json(content_type=None)
            entries = payload["models"]
            if not isinstance(entries, list):
                raise TypeError("invalid model catalog")
            models: dict[str, LocalModel] = {}
            for item in entries:
                if item["type"] != "llm":
                    continue
                model_id = item["key"]
                if not valid_model_id(model_id):
                    continue
                instances = item.get("loaded_instances", [])
                if not isinstance(instances, list):
                    raise TypeError("invalid loaded instances")
                # A custom loaded-instance ID is the actual chat/completions ID.
                ids = [instance["id"] for instance in instances] or [model_id]
                for value in ids:
                    if valid_model_id(value):
                        models[value] = LocalModel(value, bool(instances))
            return sorted(models.values(), key=lambda model: model.id)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise LlmError("LM Studio недоступна. Включите Local Server и проверьте порт.") from error
        except (KeyError, ValueError, TypeError) as error:
            raise LlmError("LM Studio вернула некорректный список моделей.") from error
        finally:
            if response is not None:
                response.release()

    async def select(self, value: str, *, token: bool = False) -> LocalModel:
        # Recheck the catalog so stale buttons cannot select a removed model.
        models = await self.list()
        matches = [model for model in models if (model.token if token else model.id) == value]
        if len(matches) != 1:
            raise ValueError("Модель больше недоступна. Обновите список через /models.")
        model = matches[0]
        self.selection.save(model.id)
        return model
