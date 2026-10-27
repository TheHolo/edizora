import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import home_worker, local
from app.bot.model_ui import build_model_router, model_menu
from app.news.worker_config import HomeWorkerSettings
from app.news.worker_errors import LlmError
from app.services.model_selection import LMStudioModels, LocalModel, ModelSelection
from app.services.title_client import SelectedTitleClient
from app.services.translation_service import TranslationService
from test_home_worker_clients import FakeResponse, FakeSession
from test_menu import RecordingBot, make_callback, make_message


def selection(tmp_path, model="old-model"):
    return ModelSelection(tmp_path, HomeWorkerSettings(
        _config_file="missing.toml", _env_file=None, token="worker-secret", llm_provider="lmstudio",
        lmstudio_model=model, lmstudio_api_key="private-key",
    ))


def catalog(*ids):
    return FakeResponse(200, {"models": [
        {"key": value, "type": "llm", "loaded_instances": []} for value in ids
    ]})


def test_selection_persists_without_secrets_and_is_scoped_to_server(tmp_path):
    first = selection(tmp_path)
    first.save("publisher/new-model")
    assert selection(tmp_path).selected == "publisher/new-model"
    assert first.worker_settings().model_name == "publisher/new-model"
    saved = first.path.read_text()
    assert "private-key" not in saved and "worker-secret" not in saved
    other = ModelSelection(tmp_path, first.settings.model_copy(update={"lmstudio_base_url": "http://localhost:9999/v1"}))
    assert other.selected == "old-model"
    first.path.write_text("broken JSON")
    assert first.selected == "old-model"


async def test_native_catalog_filters_embeddings_and_uses_loaded_instance_ids(tmp_path):
    response = catalog("installed-model")
    response.payload["models"].extend([
        {"key": "embedding", "type": "embedding", "loaded_instances": []},
        {"key": "publisher/model", "type": "llm", "loaded_instances": [{"id": "custom-loaded-id"}]},
    ])
    session = FakeSession(response)
    models = await LMStudioModels(session, selection(tmp_path)).list()
    assert models == [LocalModel("custom-loaded-id", True), LocalModel("installed-model", False)]
    assert session.requests[0]["url"] == "http://127.0.0.1:1234/api/v1/models"
    assert session.requests[0]["headers"]["Authorization"] == "Bearer private-key"
    assert session.requests[0]["allow_redirects"] is False
    assert response.released


async def test_older_server_falls_back_to_openai_catalog(tmp_path):
    session = FakeSession(FakeResponse(404), FakeResponse(200, {"data": [
        {"id": "text-embedding-nomic"}, {"id": "text-model"}, {"id": "reranker"},
    ]}))
    assert await LMStudioModels(session, selection(tmp_path)).list() == [LocalModel("text-model")]
    assert session.requests[1]["url"].endswith("/v1/models")


@pytest.mark.parametrize("response", [FakeResponse(401), FakeResponse(503), FakeResponse(200, {}), TimeoutError()])
async def test_unavailable_catalog_leaves_selection_unchanged(tmp_path, response):
    chosen = selection(tmp_path)
    with pytest.raises(LlmError):
        await LMStudioModels(FakeSession(response), chosen).select("new-model")
    assert chosen.selected == "old-model"
    assert not chosen.path.exists()


async def test_selection_rechecks_stale_buttons_and_survives_catalog_reordering(tmp_path):
    chosen = selection(tmp_path)
    service = LMStudioModels(FakeSession(catalog("z", "a"), catalog("a", "z"), catalog("a")), chosen)
    button = next(model for model in await service.list() if model.id == "z")
    await service.select(button.token, token=True)
    assert chosen.selected == "z"
    with pytest.raises(ValueError, match="недоступна"):
        await service.select(button.token, token=True)
    assert chosen.selected == "z"


def test_model_menu_paginates_and_fits_long_unicode_ids_in_callbacks():
    models = [LocalModel(f"model-{n}-" + "я" * 95) for n in range(20)]
    text, markup = model_menu(models, "<absent>", page=1)
    assert "&lt;absent&gt;" in text and "Страница 2 из 3" in text
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len([b for b in buttons if b.callback_data.startswith("lm:select:")]) == 8
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert len({model.token for model in models}) == 20


async def test_model_command_and_button_share_persisted_choice(tmp_path):
    chosen = selection(tmp_path)
    service = LMStudioModels(FakeSession(catalog("new"), catalog("new"), catalog("new")), chosen)
    router = build_model_router(service)
    bot = RecordingBot()
    message = make_message(bot, "/models")
    await router.message.handlers[0].callback(message)
    data = bot.requests[0].reply_markup.inline_keyboard[0][0].callback_data
    await router.callback_query.handlers[0].callback(make_callback(bot, message, data))
    assert chosen.selected == "new"
    assert "<code>new</code>" in bot.requests[-1].text


async def test_each_title_request_snapshots_model_even_if_selection_changes_in_flight(tmp_path):
    chosen = selection(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    class Session:
        def __init__(self):
            self.requests = []

        async def request(self, method, url, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                started.set()
                await release.wait()
            return FakeResponse(200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "translated_text": "Autumn Garden", "source_language": "ja",
            })}}]})

    session = Session()
    client = SelectedTitleClient(session, chosen, timeout=5)
    first = asyncio.create_task(client.translate("秋の庭"))
    await started.wait()
    chosen.save("new-model")
    release.set()
    await first
    await client.translate("庭")
    assert [r["json"]["model"] for r in session.requests] == ["old-model", "new-model"]
    assert all("English" in r["json"]["messages"][0]["content"] for r in session.requests)


async def test_model_change_resets_translation_failure_cooldown_and_cached_fallback():
    client = SimpleNamespace(model="old", translate=AsyncMock(side_effect=TimeoutError()))
    translator = TranslationService(SimpleNamespace(), local_client=client, fallback_mymemory=False)
    assert await translator.translate_to_english("庭") is None
    client.model = "new"
    client.translate.side_effect = None
    client.translate.return_value = SimpleNamespace(translated_text="Garden", source_language="ja")
    assert await translator.translate_to_english("庭") == ("Garden", "ja")
    assert client.translate.await_count == 2


async def test_worker_switches_models_between_jobs_without_restarting(tmp_path, monkeypatch):
    chosen = selection(tmp_path)
    monkeypatch.setattr(home_worker, "list_model_ids", AsyncMock(return_value=["old-model", "new-model"]))
    monkeypatch.setattr(home_worker, "create_news_client", lambda session, settings: SimpleNamespace(model=settings.model_name))
    observed = []

    async def exercise(worker):
        assert worker.ollama is None
        assert await worker.ready()
        first = worker.ollama
        observed.append((worker.api.model, first.model))
        chosen.save("new-model")
        assert first.model == "old-model"
        assert await worker.ready()
        observed.append((worker.api.model, worker.ollama.model))
        assert worker.ollama is not first

    monkeypatch.setattr(home_worker.HomeNewsWorker, "run_forever", exercise)
    await home_worker.run_worker(chosen.settings, model_selection=chosen)
    assert observed == [("old-model", "old-model"), ("new-model", "new-model")]


def test_local_launcher_restores_selected_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BOT_TOKEN=\n", encoding="utf-8")
    config = tmp_path / "bot-settings.toml"
    config.write_text('[bot]\n[home_worker]\nlmstudio_model="old-model"\n', encoding="utf-8")
    selection(tmp_path / "storage").save("new-model")
    settings, worker = local.load_local_settings(config)
    assert settings.news_model_name == worker.model_name == "new-model"
