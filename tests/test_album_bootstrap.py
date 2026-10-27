from app.bootstrap import bootstrap
from app.config import Settings
from app.news.http import PublicOnlyResolver
from app.news.worker_config import HomeWorkerSettings


async def test_editor_is_wired_before_catchall_and_translation_uses_separate_loopback_session(tmp_path):
    settings = Settings(
        _config_file="missing.toml", _env_file=None, bot_token="123456:TestToken", admin_ids={1},
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bootstrap.db'}", storage_path=tmp_path / "storage",
        auto_translate_titles=True, translation_fallback_mymemory=True,
        channels_json={"artwork": {"chat_id": "-1001"}},
    )
    worker = HomeWorkerSettings(
        _config_file="missing.toml", _env_file=None, token="unused", lmstudio_model="translation-model",
    )
    application = await bootstrap(settings, home_worker_settings=worker)
    try:
        names = [r.name for r in application.dispatcher.sub_routers]
        assert names[0] == "lmstudio_models"
        assert names[2] == "album_editor"
        router = application.dispatcher.sub_routers[2]
        assert len(router.message.outer_middleware) == 1
        assert len(router.callback_query.outer_middleware) == 1
        translator = application.workers.translator
        assert translator.local_client.model == "translation-model"
        assert translator.local_client.session is application.local_http
        assert translator.session is application.http
        assert isinstance(application.http.connector._resolver, PublicOnlyResolver)
        assert not isinstance(application.local_http.connector._resolver, PublicOnlyResolver)
        assert translator.local_client.timeout == settings.translation_lmstudio_timeout
        model_router = application.dispatcher.sub_routers[0]
        assert len(model_router.message.outer_middleware) == 1
        assert len(model_router.callback_query.outer_middleware) == 1
    finally:
        await application.http.close()
        await application.local_http.close()
        await application.bot.session.close()
        await application.engine.dispose()
