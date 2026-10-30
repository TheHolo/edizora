from __future__ import annotations

import aiohttp

from app.news.lmstudio_client import LMStudioNewsClient, check_lmstudio_status
from app.news.ollama_client import OllamaNewsClient
from app.news.structured_client import StructuredNewsClient
from app.news.worker_config import HomeWorkerSettings
from app.news.worker_errors import LlmError


def create_news_client(session: aiohttp.ClientSession, settings: HomeWorkerSettings) -> StructuredNewsClient:
    common = dict(
        model=settings.model_name,
        retry_backoff_seconds=settings.retry_backoff_seconds,
        temperature=settings.temperature,
        max_source_chars_per_chunk=settings.max_source_chars_per_chunk,
        max_source_chunks=settings.max_source_chunks,
    )
    if settings.llm_provider in {"lmstudio", "openai"}:
        generic = settings.llm_provider == "openai"
        if not settings.model_name:
            raise ValueError(
                "Заполните HOME_WORKER_LMSTUDIO_MODEL точным ID модели. "
                "Посмотреть ID: python -m app.local --models"
            )
        client = LMStudioNewsClient(
            session, **common,
            base_url=settings.openai_base_url if generic else settings.lmstudio_base_url,
            api_key=(settings.openai_api_key.get_secret_value() if settings.openai_api_key else None) if generic else (settings.lmstudio_api_key.get_secret_value() if settings.lmstudio_api_key else None),
            timeout_seconds=settings.lmstudio_timeout_seconds,
            max_retries=settings.lmstudio_max_retries,
            context_length=settings.lmstudio_context_length,
            max_predict_tokens=settings.lmstudio_max_predict_tokens,
        )
        client.youtube_max_source_chunks = settings.youtube_max_source_chunks
        return client
    client = OllamaNewsClient(
        session, **common,
        base_url=settings.ollama_base_url,
        timeout_seconds=settings.ollama_timeout_seconds,
        max_retries=settings.ollama_max_retries,
        keep_alive=settings.ollama_keep_alive,
        context_length=settings.ollama_context_length,
        max_predict_tokens=settings.ollama_max_predict_tokens,
    )
    client.youtube_max_source_chunks = settings.youtube_max_source_chunks
    return client


async def list_model_ids(session: aiohttp.ClientSession, settings: HomeWorkerSettings) -> list[str]:
    """Query the configured local server without loading a model or running inference."""
    headers = {"Accept": "application/json"}
    if settings.llm_provider in {"lmstudio", "openai"}:
        generic = settings.llm_provider == "openai"
        url = f"{settings.openai_base_url if generic else settings.lmstudio_base_url}/models"
        key = settings.openai_api_key if generic else settings.lmstudio_api_key
        if key and key.get_secret_value():
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        list_key, id_key = "data", "id"
    else:
        url = f"{settings.ollama_base_url}/api/tags"
        list_key, id_key = "models", "name"
    response = None
    try:
        response = await session.request(
            "GET", url, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        )
        if settings.llm_provider in {"lmstudio", "openai"}:
            check_lmstudio_status(response.status)
        elif response.status != 200:
            raise LlmError(f"Ollama: список моделей недоступен (HTTP {response.status})")
        payload = await response.json(content_type=None)
        items = payload[list_key]
        if not isinstance(items, list):
            raise TypeError("model catalog must be a list")
        ids = [item[id_key] for item in items]
        if any(not isinstance(model_id, str) or not model_id.strip() for model_id in ids):
            raise TypeError("model IDs must be non-empty strings")
        return sorted(set(ids))
    except (aiohttp.ClientError, TimeoutError) as error:
        raise LlmError(
            f"Сервер {settings.llm_provider} недоступен. Включите локальный API и проверьте его порт."
        ) from error
    except (KeyError, ValueError, TypeError) as error:
        raise LlmError(f"{settings.llm_provider}: некорректный список моделей") from error
    finally:
        if response is not None:
            response.release()
