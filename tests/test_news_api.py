from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from app.domain.exceptions import PublishError
from app.news.api import NewsApiServer
from app.news.worker_models import NewsTask


def api_server(tasks, **callbacks) -> NewsApiServer:
    return NewsApiServer(
        tasks=tasks,
        token="shared-secret",
        host="127.0.0.1",
        port=0,
        default_lease_seconds=1800,
        **callbacks,
    )


async def test_news_api_authenticates_and_returns_worker_contract():
    task = NewsTask.model_validate({
        "id": 7,
        "lease_token": "lease-token",
        "model_name": "gemma4:12b",
        "input_payload": {
            "kind": "website",
            "source_url": "https://example.com/story",
        },
    })
    tasks = SimpleNamespace(lease=AsyncMock(return_value=task))
    server = api_server(tasks)

    async with TestClient(TestServer(server.create_app())) as client:
        unauthorized = await client.post("/api/news/tasks/lease", json={"worker_id": "home"})
        assert unauthorized.status == 401

        response = await client.post(
            "/api/news/tasks/lease",
            headers={"Authorization": "Bearer shared-secret"},
            json={
                "worker_id": "home",
                "lease_seconds": 3600,
                "source_types": ["website", "youtube"],
                "capabilities": {"model": "gemma4:12b"},
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["id"] == 7
    assert payload["model_name"] == "gemma4:12b"
    tasks.lease.assert_awaited_once_with(
        worker_id="home",
        lease_seconds=3600,
        source_types=["website", "youtube"],
        model_name="gemma4:12b",
        use_worker_model=False,
    )


async def test_notification_failure_does_not_turn_committed_progress_into_http_error():
    stored = SimpleNamespace(id=4, status="leased", stage="llm_processing")
    tasks = SimpleNamespace(progress=AsyncMock(return_value=(stored, True)))
    notify = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    server = api_server(tasks, on_progress=notify)

    async with TestClient(TestServer(server.create_app())) as client:
        response = await client.post(
            "/api/news/tasks/4/progress",
            headers={"Authorization": "Bearer shared-secret"},
            json={
                "lease_token": "token",
                "stage": "llm_processing",
                "message": "gemma4:12b создаёт черновик",
            },
        )

    assert response.status == 200
    notify.assert_awaited_once_with(stored)


async def test_news_api_rejects_string_retryable_flag():
    tasks = SimpleNamespace(fail=AsyncMock())
    server = api_server(tasks)

    async with TestClient(TestServer(server.create_app())) as client:
        response = await client.post(
            "/api/news/tasks/4/fail",
            headers={"Authorization": "Bearer shared-secret"},
            json={"lease_token": "token", "error": "failed", "retryable": "false"},
        )

    assert response.status == 400
    tasks.fail.assert_not_awaited()


async def test_news_api_returns_400_for_structurally_invalid_completion():
    tasks = SimpleNamespace(complete=AsyncMock(side_effect=KeyError("source_id")))
    server = api_server(tasks)

    async with TestClient(TestServer(server.create_app())) as client:
        response = await client.post(
            "/api/news/tasks/4/complete",
            headers={"Authorization": "Bearer shared-secret"},
            json={"lease_token": "token", "result": {"source": {}}},
        )

    assert response.status == 400


async def test_news_api_returns_400_when_rendered_draft_exceeds_telegram_limit():
    tasks = SimpleNamespace(
        complete=AsyncMock(side_effect=PublishError("Текст превышает лимит")),
    )
    server = api_server(tasks)

    async with TestClient(TestServer(server.create_app())) as client:
        response = await client.post(
            "/api/news/tasks/4/complete",
            headers={"Authorization": "Bearer shared-secret"},
            json={"lease_token": "token", "result": {"source": {}}},
        )

    assert response.status == 400


async def test_lease_poll_notifies_about_exhausted_expired_tasks():
    expired = SimpleNamespace(id=91)
    tasks = SimpleNamespace(
        expire_exhausted=AsyncMock(return_value=[expired]),
        lease=AsyncMock(return_value=None),
    )
    notify = AsyncMock()
    server = api_server(tasks, on_failure=notify)

    async with TestClient(TestServer(server.create_app())) as client:
        response = await client.post(
            "/api/news/tasks/lease",
            headers={"Authorization": "Bearer shared-secret"},
            json={"worker_id": "home"},
        )

    assert response.status == 204
    notify.assert_awaited_once_with(expired)
