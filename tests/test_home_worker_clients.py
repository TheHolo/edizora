from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

import aiohttp
import pytest

from app.news.models import ExtractedNewsSource, NewsSourceKind
from app.news.ollama_client import OllamaNewsClient
from app.news.vps_client import VpsNewsApiClient
from app.news.worker_errors import LeaseLostError, LlmOutputTruncatedError
from app.news.worker_models import NewsDraft, NewsTask, WorkerResult


class FakeResponse:
    def __init__(self, status: int, payload: Any = None) -> None:
        self.status = status
        self.payload = payload
        self.released = False

    async def json(self, *, content_type=None):
        return self.payload

    def release(self) -> None:
        self.released = True


class FakeSession:
    def __init__(self, *responses: FakeResponse | BaseException) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def make_api(session: FakeSession, *, max_retries: int = 0) -> VpsNewsApiClient:
    return VpsNewsApiClient(
        session,  # type: ignore[arg-type]
        base_url="https://publisher.example",
        token="secret-token",
        worker_id="home-pc",
        lease_seconds=1800,
        source_types=("website", "youtube", "telegram", "manual"),
        model="gemma4:12b",
        max_retries=max_retries,
        retry_backoff_seconds=0,
    )


def make_task() -> NewsTask:
    return NewsTask.model_validate(
        {
            "id": 17,
            "lease_token": "lease-17",
            "input_payload": {"kind": "manual", "source_text": "Исходный текст"},
        }
    )


def make_result() -> WorkerResult:
    source = ExtractedNewsSource(
        kind=NewsSourceKind.MANUAL,
        source_id="manual-17",
        source_url=None,
        normalized_url=None,
        title="Исходный заголовок",
        raw_text="Исходный текст",
        author_name=None,
        published_at=datetime(2026, 7, 31, tzinfo=UTC),
    )
    draft = NewsDraft(
        headline="Заголовок",
        lead="Лид",
        body="Текст новости",
        suggested_tags=["новости"],
        facts_used=["Факт"],
        warnings=[],
    )
    return WorkerResult(source=source, draft=draft)


async def test_vps_client_leases_task_with_capabilities_and_bearer_token():
    response = FakeResponse(
        200,
        {
            "id": 17,
            "lease_token": "lease-17",
            "input_payload": {"kind": "manual", "source_text": "Исходный текст"},
        },
    )
    session = FakeSession(response)

    task = await make_api(session).lease()

    assert task is not None and task.id == 17
    request = session.requests[0]
    assert request["url"].endswith("/api/news/tasks/lease")
    assert request["headers"]["Authorization"] == "Bearer secret-token"
    assert request["json"]["capabilities"]["model"] == "gemma4:12b"
    assert request["json"]["source_types"] == ["website", "youtube", "telegram", "manual"]
    assert response.released is True


async def test_vps_client_sends_source_and_draft_in_complete_result():
    session = FakeSession(FakeResponse(204))

    await make_api(session).complete(make_task(), make_result())

    request = session.requests[0]
    assert request["url"].endswith("/api/news/tasks/17/complete")
    assert request["json"]["lease_token"] == "lease-17"
    assert request["json"]["result"]["source"]["source_id"] == "manual-17"
    assert request["json"]["result"]["draft"]["headline"] == "Заголовок"


async def test_vps_client_does_not_retry_expired_completion_lease():
    session = FakeSession(FakeResponse(410), FakeResponse(204))

    with pytest.raises(LeaseLostError):
        await make_api(session, max_retries=3).complete(make_task(), make_result())

    assert len(session.requests) == 1


async def test_vps_client_retries_transient_http_error():
    session = FakeSession(FakeResponse(503), FakeResponse(204))

    task = await make_api(session, max_retries=1).lease()

    assert task is None
    assert len(session.requests) == 2


async def test_ollama_uses_gemma_structured_schema_and_disables_thinking():
    response = FakeResponse(
        200,
        {
            "message": {
                "content": (
                    '{"headline":"Заголовок","lead":"Лид","body":"Текст",'
                    '"suggested_tags":[],"facts_used":[],"warnings":[]}'
                )
            }
        },
    )
    session = FakeSession(response)
    client = OllamaNewsClient(
        session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:11434",
        model="gemma4:12b",
        max_retries=0,
    )

    draft = await client.rewrite(make_result().source)

    assert draft.headline == "Заголовок"
    request = session.requests[0]
    assert request["url"] == "http://127.0.0.1:11434/api/chat"
    assert request["json"]["model"] == "gemma4:12b"
    assert request["json"]["think"] is False
    assert request["json"]["options"]["temperature"] == 0.1
    assert request["json"]["format"]["additionalProperties"] is False


async def test_ollama_rejects_truncated_output_even_if_it_is_valid_json():
    response = FakeResponse(200, {"done_reason": "length", "message": {
        "content": '{"headline":"Заголовок","body":"Текст"}'}})
    session = FakeSession(response)
    client = OllamaNewsClient(session, base_url="http://127.0.0.1:11434", max_retries=2)
    with pytest.raises(LlmOutputTruncatedError):
        await client.rewrite(make_result().source)
    assert len(session.requests) == 1 and response.released


async def test_ollama_retries_without_think_when_runtime_does_not_support_it():
    unsupported = FakeResponse(400, {"error": "unknown field think: thinking unsupported"})
    valid = FakeResponse(
        200,
        {
            "message": {
                "content": (
                    '{"headline":"Заголовок","lead":"","body":"Текст",'
                    '"suggested_tags":[],"facts_used":[],"warnings":[]}'
                )
            }
        },
    )
    session = FakeSession(unsupported, valid)
    client = OllamaNewsClient(
        session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:11434",
        max_retries=0,
    )

    await client.rewrite(make_result().source)

    assert session.requests[0]["json"]["think"] is False
    assert "think" not in session.requests[1]["json"]


async def test_ollama_retries_invalid_structured_output():
    invalid = FakeResponse(200, {"message": {"content": "{}"}})
    valid = FakeResponse(
        200,
        {
            "message": {
                "content": (
                    '{"headline":"Заголовок","lead":"","body":"Текст",'
                    '"suggested_tags":[],"facts_used":[],"warnings":[]}'
                )
            }
        },
    )
    session = FakeSession(invalid, valid)
    client = OllamaNewsClient(
        session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:11434",
        max_retries=1,
        retry_backoff_seconds=0,
    )

    draft = await client.rewrite(make_result().source)

    assert draft.body == "Текст"
    assert len(session.requests) == 2


async def test_vps_network_failure_is_retried():
    session = FakeSession(aiohttp.ClientConnectionError(), FakeResponse(204))

    assert await make_api(session, max_retries=1).lease() is None
    assert len(session.requests) == 2
