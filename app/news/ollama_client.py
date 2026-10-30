from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from pydantic import ValidationError

# Re-export the prompts for compatibility with existing integrations.
from app.news.structured_client import (
    FACT_SYSTEM_PROMPT as FACT_SYSTEM_PROMPT,
    NEWS_SYSTEM_PROMPT as NEWS_SYSTEM_PROMPT,
    REDUCE_SYSTEM_PROMPT as REDUCE_SYSTEM_PROMPT,
    StructuredModel,
    StructuredNewsClient,
)
from app.news.worker_errors import LlmOutputTruncatedError, OllamaError, OllamaOutputError, TransientOllamaError


class OllamaNewsClient(StructuredNewsClient):
    async def _structured_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[StructuredModel],
    ) -> StructuredModel:
        use_thinking_flag = True
        retries = 0
        while True:
            body: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "format": response_model.model_json_schema(),
                "keep_alive": self.keep_alive,
                "options": {
                    "temperature": self.temperature,
                    "num_ctx": self.context_length,
                    "num_predict": min(
                        self.max_predict_tokens,
                        max(256, self.context_length // 3),
                    ),
                },
            }
            if use_thinking_flag:
                body["think"] = False

            response = None
            try:
                response = await self.session.request(
                    "POST",
                    f"{self.base_url}/api/chat",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json=body,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if response.status == 429 or response.status >= 500:
                    raise TransientOllamaError(
                        f"temporary Ollama error (HTTP {response.status})"
                    )
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, TypeError) as error:
                    raise OllamaOutputError("Ollama returned invalid JSON") from error

                if response.status == 400 and use_thinking_flag and _thinking_is_unsupported(payload):
                    use_thinking_flag = False
                    continue
                if response.status != 200:
                    raise OllamaError(f"Ollama rejected the request (HTTP {response.status})")

                if isinstance(payload, dict) and payload.get("done_reason") == "length":
                    raise LlmOutputTruncatedError("Ollama: ответ обрезан по лимиту длины")
                try:
                    content = payload["message"]["content"]
                    if isinstance(content, str):
                        return response_model.model_validate_json(content)
                    return response_model.model_validate(content)
                except (KeyError, ValidationError, ValueError, TypeError) as error:
                    raise OllamaOutputError(
                        f"Ollama output does not match {response_model.__name__}"
                    ) from error
            except OllamaError as error:
                if not getattr(error, "retryable", False):
                    raise
                last_error: BaseException = error
            except (aiohttp.ClientError, TimeoutError) as error:
                last_error = TransientOllamaError("could not reach local Ollama")
                last_error.__cause__ = error
            finally:
                if response is not None:
                    response.release()

            if retries >= self.max_retries:
                raise last_error
            await asyncio.sleep(self.retry_backoff_seconds * (2**retries))
            retries += 1


def _thinking_is_unsupported(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    message = str(payload.get("error") or "").casefold()
    return any(word in message for word in ("think", "thinking", "reasoning")) and any(
        word in message for word in ("unsupported", "not support", "unknown", "invalid")
    )
