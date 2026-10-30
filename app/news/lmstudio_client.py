from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from pydantic import ValidationError

from app.news.structured_client import StructuredModel, StructuredNewsClient
from app.news.worker_errors import LlmError, LlmOutputError, LlmOutputTruncatedError, TransientLlmError


class LMStudioNewsClient(StructuredNewsClient):
    """Local OpenAI-compatible transport; extraction and context budgeting are shared."""

    def __init__(self, session: aiohttp.ClientSession, *, api_key: str | None = None,
                 disable_reasoning: bool = False, **kwargs: Any) -> None:
        super().__init__(session, **kwargs)
        self._disable_reasoning = disable_reasoning
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    async def _structured_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredModel],
    ) -> StructuredModel:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": min(self.max_predict_tokens, max(256, self.context_length // 3)),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            },
        }
        if self._disable_reasoning:
            body["reasoning_effort"] = "none"
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = await self.session.request(
                    "POST", f"{self.base_url}/chat/completions",
                    headers=self._headers, json=body, timeout=self.timeout,
                    allow_redirects=False,
                )
                check_lmstudio_status(response.status)
                try:
                    payload = await response.json(content_type=None)
                    choice = payload["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise LlmOutputTruncatedError(
                            "LM Studio: ответ обрезан. Увеличьте HOME_WORKER_LMSTUDIO_MAX_PREDICT_TOKENS "
                            "и контекст модели или отключите reasoning в LM Studio."
                        )
                    content = choice["message"]["content"]
                    if not isinstance(content, str):
                        raise TypeError("content must be a string")
                    return response_model.model_validate_json(content)
                except (KeyError, IndexError, AttributeError, ValidationError, ValueError, TypeError) as error:
                    raise LlmOutputError(
                        f"LM Studio: ответ не соответствует схеме {response_model.__name__}"
                    ) from error
            except LlmError as error:
                if isinstance(error, LlmOutputTruncatedError):
                    # Retrying an identical prompt is unlikely to reduce its output;
                    # the video pipeline can instead split the material and retry.
                    raise
                if not error.retryable:
                    raise
                last_error: LlmError = error
            except (aiohttp.ClientError, TimeoutError) as error:
                last_error = TransientLlmError(
                    "LM Studio недоступен или не успел ответить. Проверьте Developer → Start Server "
                    "и HOME_WORKER_LMSTUDIO_TIMEOUT_SECONDS."
                )
                last_error.__cause__ = error
            finally:
                if response is not None:
                    response.release()
            if attempt >= self.max_retries:
                raise last_error
            await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
        raise RuntimeError("unreachable LM Studio retry state")


def check_lmstudio_status(status: int) -> None:
    if status in {401, 403}:
        raise LlmError(
            "LM Studio отклонил авторизацию. Проверьте HOME_WORKER_LMSTUDIO_API_KEY "
            "и разрешения токена в Server Settings."
        )
    if status == 408 or status == 429 or status >= 500:
        raise TransientLlmError(f"Временная ошибка LM Studio (HTTP {status})")
    if status != 200:
        raise LlmError(
            f"LM Studio отклонил запрос (HTTP {status}). Проверьте адрес /v1, ID модели, "
            "контекст и поддержку structured output; подробности в логах LM Studio."
        )
