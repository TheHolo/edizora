from dataclasses import dataclass
from collections.abc import Callable

from app.db.models import Channel, NewsTask
from app.news.models import NewsSourceKind, NewsSourceRequest
from app.services.job_service import JobService
from app.services.news_task_service import NewsTaskService


@dataclass(frozen=True)
class QueuedNews:
    task: NewsTask
    channel: Channel


class NewsSubmissionService:
    def __init__(
        self, *, jobs: JobService, tasks: NewsTaskService, default_channel_alias: str,
        model_name: str, max_attempts: int, enabled: bool = True,
        model_resolver: Callable[[], str] | None = None,
    ) -> None:
        self.jobs = jobs
        self.tasks = tasks
        self.default_channel_alias = default_channel_alias
        self.model_name = model_name
        self.model_resolver = model_resolver
        self.max_attempts = max_attempts
        self.enabled = enabled

    async def create(
        self, *, telegram_user_id: int, username: str | None, display_name: str,
        origin_chat_id: int | str, request: NewsSourceRequest,
        user_tags: list[str] | None = None, extra_payload: dict | None = None,
        channel_alias: str | None = None,
    ) -> QueuedNews:
        if not self.enabled:
            raise ValueError(
                "Обработка новостей не настроена: задайте NEWS_WORKER_TOKEN на сервере."
            )
        user = await self.jobs.ensure_user(telegram_user_id, username, display_name)
        channel = (
            await self.jobs.get_channel(channel_alias)
            if channel_alias
            else await self.jobs.get_preferred_channel(user.id, self.default_channel_alias)
        )
        if channel is None:
            raise ValueError("Нет активного канала для публикации")
        forwarded_telegram = (
            request.kind == NewsSourceKind.TELEGRAM
            and extra_payload
            and bool(extra_payload.get("telegram"))
        )
        payload = {"kind": request.kind.value}
        if not forwarded_telegram:
            payload[
                "source_text" if request.kind == NewsSourceKind.MANUAL else "source_url"
            ] = request.value
        if extra_payload:
            payload.update(extra_payload)
        task = await self.tasks.create(
            user_id=user.id,
            channel_id=channel.id,
            origin_chat_id=origin_chat_id,
            source_kind=request.kind.value,
            input_payload=payload,
            user_tags=user_tags or [],
            model_name=(self.model_resolver() if self.model_resolver else self.model_name) or None,
            max_attempts=self.max_attempts,
        )
        return QueuedNews(task=task, channel=channel)
