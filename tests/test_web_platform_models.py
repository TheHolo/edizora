from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.platform_models_ui import RegisteredModels, WorkerModel, build_platform_model_router, registered_model_menu
from test_menu import RecordingBot, make_callback, make_message


def common_service():
    service = SimpleNamespace(
        user_for_telegram=AsyncMock(return_value=71),
        ai_workers=AsyncMock(return_value={"items": [
            {"id": "home", "name": "Домашний ПК", "status": "online"},
            {"id": "other", "name": "Другой ПК", "status": "offline"},
            {"id": "revoked", "name": "Отозван", "status": "revoked"},
        ]}),
        ai_models=AsyncMock(return_value={"items": [
            {"worker_id": "home", "id": "model/текст", "selected": False},
            {"worker_id": "other", "id": "model/другая", "selected": False},
            {"worker_id": "home", "id": "text-embedding-hidden", "selected": False},
            {"worker_id": "revoked", "id": "revoked-model"},
        ]}),
        settings=AsyncMock(return_value={"ai_worker_id": "other", "ai_model": "model/другая"}),
        ai_selection=AsyncMock(),
    )
    return service


async def test_managed_catalog_uses_registered_common_service_and_current_actor_only():
    service = common_service()
    actor, models = await RegisteredModels(SimpleNamespace(platform=service)).catalog(999)
    assert actor == 71
    assert {model.model_id for model in models} == {"model/текст", "model/другая"}
    assert [model.model_id for model in models if model.selected] == ["model/другая"]
    service.user_for_telegram.assert_awaited_once_with(999)
    service.ai_workers.assert_awaited_once_with(71)
    service.ai_models.assert_awaited_once_with(71)
    service.settings.assert_awaited_once_with(71)


async def test_stale_or_revoked_button_cannot_apply_or_fall_back_to_local_model():
    service = common_service()
    registered = RegisteredModels(SimpleNamespace(platform=service))
    _, models = await registered.catalog(999)
    token = next(model.token for model in models if model.worker_id == "home")
    service.ai_models.return_value = {"items": []}
    with pytest.raises(ValueError):
        await registered.select(999, token)
    service.ai_selection.assert_not_awaited()


async def test_selection_reloads_catalog_and_calls_same_command_as_web():
    service = common_service()
    registered = RegisteredModels(SimpleNamespace(platform=service))
    _, models = await registered.catalog(999)
    choice = next(model for model in models if model.worker_id == "home")
    await registered.select(999, choice.token)
    service.ai_selection.assert_awaited_once_with(71, {"worker_id": "home", "model_id": "model/текст"})


async def test_router_resolves_platform_after_construction_without_local_http(monkeypatch):
    from app.services.model_selection import LMStudioModels
    probe = AsyncMock(side_effect=AssertionError("managed router must not query LM Studio"))
    monkeypatch.setattr(LMStudioModels, "list", probe)
    jobs = SimpleNamespace()
    router = build_platform_model_router(jobs)
    jobs.platform = common_service()
    bot = RecordingBot()
    message = make_message(bot, "/models")
    await router.message.handlers[0].callback(message)
    text = bot.requests[0].text
    assert "Модели домашнего AI" in text
    button = bot.requests[0].reply_markup.inline_keyboard[0][0]
    await router.callback_query.handlers[0].callback(make_callback(bot, message, button.callback_data))
    assert jobs.platform.ai_selection.await_count == 1
    probe.assert_not_awaited()


def test_long_model_ids_are_html_escaped_and_callback_data_fits_telegram_limit():
    model = WorkerModel("home", "<дом>", "model/" + "я" * 300, "online", True)
    text, markup = registered_model_menu([model])
    assert "&lt;дом&gt;" in text and "<дом>" not in text
    assert len(markup.inline_keyboard[0][0].callback_data.encode()) <= 64
    assert len(text) < 4096
