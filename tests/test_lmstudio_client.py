import asyncio
import json

import aiohttp
import pytest

from app.news.llm_factory import create_news_client, list_model_ids
from app.news.lmstudio_client import LMStudioNewsClient
from app.news.ollama_client import OllamaNewsClient
from app.news.worker_config import HomeWorkerSettings
from app.news.worker_errors import LlmError, LlmOutputError, TransientLlmError
from app.news.worker_models import NewsDraft
from test_home_worker_clients import FakeResponse, FakeSession, make_result


def valid_response():
    return FakeResponse(200, {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"headline": "Новость", "body": "Текст новости"}),
    }}]})


def client(session, **kwargs):
    return LMStudioNewsClient(
        session, base_url="http://127.0.0.1:1234/v1", model="test-model",
        retry_backoff_seconds=0, **kwargs,
    )


async def test_lmstudio_rewrites_using_schema_and_optional_authorization():
    response = valid_response()
    session = FakeSession(response)
    draft = await client(session, api_key="private", max_retries=0).rewrite(make_result().source)
    request = session.requests[0]
    assert draft.headline == "Новость"
    assert request["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer private"
    assert request["allow_redirects"] is False
    body = request["json"]
    assert body["response_format"]["json_schema"]["schema"] == NewsDraft.model_json_schema()
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["max_tokens"] == 1600 and body["stream"] is False
    assert not ({"keep_alive", "format", "options", "think"} & body.keys())
    assert response.released


@pytest.mark.parametrize("payload", [None, [], {}, {"choices": []},
    {"choices": [{"message": {"content": "{}"}}]},
    {"choices": [{"message": {"content": "invalid json"}}]},
])
async def test_malformed_llm_output_retries_and_releases_responses(payload):
    broken, valid = FakeResponse(200, payload), valid_response()
    session = FakeSession(broken, valid)
    await client(session, max_retries=1).rewrite(make_result().source)
    assert len(session.requests) == 2 and broken.released and valid.released


@pytest.mark.parametrize("status", [401, 403, 404, 400, 302])
async def test_permanent_lmstudio_errors_do_not_retry(status):
    response = FakeResponse(status, "not json")
    session = FakeSession(response)
    with pytest.raises(LlmError):
        await client(session).rewrite(make_result().source)
    assert len(session.requests) == 1 and response.released


@pytest.mark.parametrize("failure", [FakeResponse(503), FakeResponse(429), TimeoutError(), aiohttp.ClientConnectionError()])
async def test_transient_lmstudio_failures_retry(failure):
    session = FakeSession(failure, valid_response())
    await client(session, max_retries=1).rewrite(make_result().source)
    assert len(session.requests) == 2


async def test_truncated_output_is_not_accepted_even_when_json_is_valid():
    response = valid_response()
    response.payload["choices"][0]["finish_reason"] = "length"
    with pytest.raises(LlmOutputError, match="обрезан"):
        await client(FakeSession(response), max_retries=0).rewrite(make_result().source)


async def test_cancellation_is_not_retried():
    session = FakeSession(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await client(session).rewrite(make_result().source)
    assert len(session.requests) == 1


async def test_retry_budget_is_bounded():
    session = FakeSession(FakeResponse(500), FakeResponse(500))
    with pytest.raises(TransientLlmError):
        await client(session, max_retries=1).rewrite(make_result().source)
    assert len(session.requests) == 2


async def test_factory_and_catalog_use_provider_model_and_keep_secret_private():
    settings = HomeWorkerSettings(
        _config_file="missing.toml", _env_file=None, token="token",
        llm_provider="lmstudio", lmstudio_model="  local/model  ", lmstudio_api_key="private-key",
    )
    response = FakeResponse(200, {"data": [{"id": "local/model"}]})
    session = FakeSession(response)
    assert await list_model_ids(session, settings) == ["local/model"]
    assert "private-key" not in repr(settings)
    assert session.requests[0]["headers"]["Authorization"] == "Bearer private-key"
    assert response.released
    llm = create_news_client(session, settings)
    assert isinstance(llm, LMStudioNewsClient) and llm.model == "local/model"
    legacy = settings.model_copy(update={"llm_provider": "ollama"})
    assert isinstance(create_news_client(session, legacy), OllamaNewsClient)


async def test_empty_source_fails_before_any_request():
    source = make_result().source
    source.raw_text = "   "
    session = FakeSession()
    with pytest.raises(LlmError, match="empty"):
        await client(session).rewrite(source)
    assert not session.requests
