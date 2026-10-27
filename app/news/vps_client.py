from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import aiohttp
from pydantic import ValidationError

from app.news.worker_errors import (
    LeaseLostError,
    TransientVpsApiError,
    VpsApiError,
    VpsAuthenticationError,
)
from app.news.worker_models import NewsTask, ProgressStage, WorkerResult


class VpsNewsApiClient:
    """Authenticated lease client used only for the narrow news-task API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str,
        token: str,
        worker_id: str,
        lease_seconds: int,
        source_types: tuple[str, ...],
        model: str,
        timeout_seconds: float = 30,
        max_retries: int = 3,
        retry_backoff_seconds: float = 1,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.source_types = source_types
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def lease(self) -> NewsTask | None:
        payload = await self._request(
            "POST",
            "/api/news/tasks/lease",
            json={
                "worker_id": self.worker_id,
                "lease_seconds": self.lease_seconds,
                "source_types": list(self.source_types),
                "capabilities": {
                    "local_llm": True,
                    "structured_json": True,
                    "extraction": True,
                    "progress": True,
                    "model": self.model,
                },
            },
            expected={200, 204},
        )
        if payload is None:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
            payload = payload["task"]
        try:
            return NewsTask.model_validate(payload)
        except ValidationError as error:
            raise VpsApiError("VPS returned an invalid news task") from error

    async def complete(self, task: NewsTask, result: WorkerResult) -> None:
        task_id = quote(str(task.id), safe="")
        await self._request(
            "POST",
            f"/api/news/tasks/{task_id}/complete",
            json={
                "lease_token": task.lease_token,
                "result": result.model_dump(mode="json"),
            },
            expected={200, 204},
        )

    async def fail(self, task: NewsTask, error: BaseException, *, retryable: bool) -> None:
        task_id = quote(str(task.id), safe="")
        message = " ".join(str(error).split())[:2000] or error.__class__.__name__
        await self._request(
            "POST",
            f"/api/news/tasks/{task_id}/fail",
            json={
                "lease_token": task.lease_token,
                "error": message,
                "retryable": retryable,
            },
            expected={200, 204},
        )

    async def progress(
        self,
        task: NewsTask,
        stage: ProgressStage | str,
        message: str,
    ) -> None:
        task_id = quote(str(task.id), safe="")
        stage_value = stage.value if isinstance(stage, ProgressStage) else stage
        await self._request(
            "POST",
            f"/api/news/tasks/{task_id}/progress",
            json={
                "lease_token": task.lease_token,
                "stage": stage_value,
                "message": " ".join(message.split())[:300],
            },
            expected={200, 204},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
        expected: set[int],
    ) -> Any:
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                response = await self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers,
                    json=json,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
                if response.status in {409, 410}:
                    raise LeaseLostError(f"task lease is no longer valid (HTTP {response.status})")
                if response.status in {401, 403}:
                    raise VpsAuthenticationError("home worker authentication was rejected")
                if response.status == 429 or response.status >= 500:
                    raise TransientVpsApiError(f"temporary VPS API error (HTTP {response.status})")
                if response.status not in expected:
                    raise VpsApiError(f"unexpected VPS API response (HTTP {response.status})")
                if response.status == 204:
                    return None
                try:
                    return await response.json(content_type=None)
                except (ValueError, TypeError) as error:
                    raise VpsApiError("VPS API returned invalid JSON") from error
            except (LeaseLostError, VpsAuthenticationError, VpsApiError) as error:
                if not getattr(error, "retryable", False):
                    raise
                last_error = error
            except (aiohttp.ClientError, TimeoutError) as error:
                last_error = TransientVpsApiError("could not reach VPS news API")
                last_error.__cause__ = error
            finally:
                if response is not None:
                    response.release()

            if attempt >= self.max_retries:
                if last_error is None:
                    raise VpsApiError("VPS API request failed without an error response")
                raise last_error
            await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))

        raise RuntimeError("unreachable VPS API retry state")
