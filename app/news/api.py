import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web
from pydantic import ValidationError

from app.db.models import NewsTask
from app.domain.exceptions import PublishError
from app.news.models import NewsSourceKind
from app.news.worker_models import ProgressStage
from app.services.news_task_service import (
    CompletedNewsTask,
    NewsTaskLeaseError,
    NewsTaskService,
)

logger = logging.getLogger(__name__)

TaskCallback = Callable[[NewsTask], Awaitable[None]]
CompletionCallback = Callable[[CompletedNewsTask], Awaitable[None]]


class NewsApiServer:
    def __init__(
        self, *, tasks: NewsTaskService, token: str, host: str, port: int,
        default_lease_seconds: int, on_progress: TaskCallback | None = None,
        on_complete: CompletionCallback | None = None,
        on_failure: TaskCallback | None = None,
        use_worker_model: bool = False,
    ) -> None:
        self.tasks = tasks
        self.token = token
        self.host = host
        self.port = port
        self.default_lease_seconds = default_lease_seconds
        self.on_progress = on_progress
        self.on_complete = on_complete
        self.on_failure = on_failure
        self.use_worker_model = use_worker_model
        self.runner: web.AppRunner | None = None
        self.sweeper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.runner is not None:
            return
        app = self.create_app()
        self.runner = web.AppRunner(app, access_log=logger)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.sweeper_task = asyncio.create_task(
            self._sweep_expired(), name="news-task-expiry-sweeper",
        )
        logger.info("news_worker_api_started host=%s port=%s", self.host, self.port)

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[self._auth])
        app.add_routes([
            web.post("/api/news/tasks/lease", self._lease),
            web.post("/api/news/tasks/{task_id:\\d+}/progress", self._progress),
            web.post("/api/news/tasks/{task_id:\\d+}/complete", self._complete),
            web.post("/api/news/tasks/{task_id:\\d+}/fail", self._fail),
            web.get("/api/news/health", self._health),
        ])
        return app

    async def stop(self) -> None:
        if self.sweeper_task is not None:
            self.sweeper_task.cancel()
            await asyncio.gather(self.sweeper_task, return_exceptions=True)
            self.sweeper_task = None
        if self.runner is None:
            return
        await self.runner.cleanup()
        self.runner = None

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
            raise web.HTTPUnauthorized(text="unauthorized")
        return await handler(request)

    async def _lease(self, request: web.Request) -> web.Response:
        body = await _json_body(request)
        worker_id = str(body.get("worker_id") or "").strip()[:128]
        if not worker_id:
            raise web.HTTPBadRequest(text="worker_id is required")
        try:
            requested = int(body.get("lease_seconds", self.default_lease_seconds))
        except (TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="lease_seconds must be an integer") from error
        lease_seconds = min(max(requested, 60), 21600)
        source_types = body.get("source_types")
        if source_types is not None and not isinstance(source_types, list):
            raise web.HTTPBadRequest(text="source_types must be a list")
        try:
            normalized_source_types = (
                [NewsSourceKind(str(value)).value for value in source_types]
                if source_types else None
            )
        except ValueError as error:
            raise web.HTTPBadRequest(text="unsupported source type") from error
        capabilities = body.get("capabilities") or {}
        if not isinstance(capabilities, dict):
            raise web.HTTPBadRequest(text="capabilities must be an object")
        model_name = str(capabilities.get("model") or "").strip()[:128] or None
        await self._expire_and_notify()
        task = await self.tasks.lease(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            source_types=normalized_source_types,
            model_name=model_name,
            use_worker_model=self.use_worker_model,
        )
        if task is None:
            return web.Response(status=204)
        return web.json_response(task.model_dump(mode="json"))

    async def _progress(self, request: web.Request) -> web.Response:
        task_id = int(request.match_info["task_id"])
        body = await _json_body(request)
        token = _lease_token(body)
        stage = str(body.get("stage") or "").strip()
        message = str(body.get("message") or "").strip()
        if not stage or not message:
            raise web.HTTPBadRequest(text="stage and message are required")
        try:
            stage = ProgressStage(stage).value
        except ValueError as error:
            raise web.HTTPBadRequest(text="unsupported progress stage") from error
        try:
            task, changed = await self.tasks.progress(
                task_id, lease_token=token, stage=stage, message=message,
            )
        except NewsTaskLeaseError as error:
            raise web.HTTPConflict(text=str(error)) from error
        if changed and self.on_progress is not None:
            try:
                await self.on_progress(task)
            except Exception:
                logger.exception("news_progress_notification_failed task_id=%s", task.id)
        return web.json_response({"status": task.status, "stage": task.stage})

    async def _complete(self, request: web.Request) -> web.Response:
        task_id = int(request.match_info["task_id"])
        body = await _json_body(request)
        token = _lease_token(body)
        result = body.get("result")
        if not isinstance(result, dict):
            raise web.HTTPBadRequest(text="result is required")
        try:
            completed = await self.tasks.complete(
                task_id, lease_token=token, result_payload=result,
            )
        except (KeyError, TypeError, PublishError, ValidationError, ValueError) as error:
            raise web.HTTPBadRequest(text="invalid worker result") from error
        except NewsTaskLeaseError as error:
            raise web.HTTPConflict(text=str(error)) from error
        if self.on_complete is not None:
            try:
                await self.on_complete(completed)
            except Exception:
                logger.exception(
                    "news_completion_notification_failed task_id=%s job_id=%s",
                    completed.task_id, completed.job_id,
                )
        return web.json_response({"status": "completed", "job_id": completed.job_id})

    async def _fail(self, request: web.Request) -> web.Response:
        task_id = int(request.match_info["task_id"])
        body = await _json_body(request)
        token = _lease_token(body)
        error_message = str(body.get("error") or "").strip()
        if not error_message:
            raise web.HTTPBadRequest(text="error is required")
        retryable = body.get("retryable", False)
        if not isinstance(retryable, bool):
            raise web.HTTPBadRequest(text="retryable must be a boolean")
        try:
            task = await self.tasks.fail(
                task_id,
                lease_token=token,
                error=error_message,
                retryable=retryable,
            )
        except NewsTaskLeaseError as error:
            raise web.HTTPConflict(text=str(error)) from error
        if self.on_failure is not None:
            try:
                await self.on_failure(task)
            except Exception:
                logger.exception("news_failure_notification_failed task_id=%s", task.id)
        return web.json_response({"status": task.status, "stage": task.stage})

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _sweep_expired(self) -> None:
        interval = max(30.0, min(self.default_lease_seconds / 3, 300.0))
        while True:
            await asyncio.sleep(interval)
            await self._expire_and_notify()

    async def _expire_and_notify(self) -> None:
        expire_exhausted = getattr(self.tasks, "expire_exhausted", None)
        if expire_exhausted is None:
            return
        try:
            expired_tasks = await expire_exhausted()
        except Exception:
            logger.exception("news_expiry_sweep_failed")
            return
        if self.on_failure is None:
            return
        for expired_task in expired_tasks:
            try:
                await self.on_failure(expired_task)
            except Exception:
                logger.exception(
                    "news_expiry_notification_failed task_id=%s", expired_task.id,
                )


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception as error:
        raise web.HTTPBadRequest(text="invalid JSON") from error
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON object required")
    return body


def _lease_token(body: dict) -> str:
    token = str(body.get("lease_token") or "")
    if not token:
        raise web.HTTPBadRequest(text="lease_token is required")
    return token
