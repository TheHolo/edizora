import json
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from app.db.models import Job
from app.domain.enums import JobStatus
from app.home_worker import run_worker
from app.news.api import NewsApiServer
from app.news.worker_config import HomeWorkerSettings
from app.services.news_task_service import NewsTaskService
from test_news_pipeline import _news_context


async def test_manual_news_passes_through_http_worker_and_lmstudio_to_durable_draft(tmp_path):
    engine, sessions, user_id, channel_id = await _news_context(tmp_path, "lmstudio-flow.db")
    tasks = NewsTaskService(sessions, lease_extension_seconds=1800)
    notification = AsyncMock()
    news_api = NewsApiServer(
        tasks=tasks, token="local-secret", host="127.0.0.1", port=8091,
        default_lease_seconds=1800, on_complete=notification,
    )
    received = []

    async def models(request):
        return web.json_response({"data": [{"id": "local-test-model"}]})

    async def chat(request):
        payload = await request.json()
        received.append(payload)
        assert payload["model"] == "local-test-model"
        assert payload["response_format"]["type"] == "json_schema"
        return web.json_response({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"headline": "Открыт читальный зал", "body": "В библиотеке 20 мест."}),
        }}]})

    lmstudio = web.Application()
    lmstudio.router.add_get("/v1/models", models)
    lmstudio.router.add_post("/v1/chat/completions", chat)
    try:
        await tasks.create(
            user_id=user_id, channel_id=channel_id, origin_chat_id="100", source_kind="manual",
            input_payload={"kind": "manual", "source_text": "Библиотека открыла читальный зал на 20 мест."},
            user_tags=["новости"], model_name="local-test-model", max_attempts=3,
        )
        async with TestServer(news_api.create_app()) as api, TestServer(lmstudio) as llm:
            settings = HomeWorkerSettings(
                _config_file="missing.toml", _env_file=None, token="local-secret",
                vps_api_url=str(api.make_url("")), llm_provider="lmstudio",
                lmstudio_base_url=str(llm.make_url("/v1")), lmstudio_model="local-test-model",
                retry_backoff_seconds=0,
            )
            await run_worker(settings, once=True)
        async with sessions() as session:
            job = await session.scalar(select(Job))
            assert job is not None and job.status == JobStatus.WAITING_CONFIRMATION
            assert job.post_data["title"] == "Открыт читальный зал"
        notification.assert_awaited_once()
        assert len(received) == 1
    finally:
        await engine.dispose()
