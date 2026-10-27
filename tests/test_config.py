import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize("channel", [
    {}, {"chat_id": ""}, {"chat_id": True}, {"chat_id": "-1001", "publish_mode": []},
    {"chat_id": "-1001", "publish_interval_seconds": -1}, {"chat_id": "-1001", "enabled": "false"},
])
def test_bad_channel_settings_fail_before_database_startup(channel):
    with pytest.raises(ValidationError):
        Settings(_config_file="missing.toml", _env_file=None, bot_token="test", channels_json={"art": channel})


def test_comma_separated_admin_ids(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI")
    monkeypatch.setenv("ADMIN_IDS", "123,456")
    assert Settings(_env_file=None).admin_ids == {123, 456}


def test_repository_settings_are_loaded_from_toml_and_override_environment(tmp_path, monkeypatch):
    config = tmp_path / "bot-settings.toml"
    config.write_text(
        "[bot]\nauto_translate_titles = true\npixiv_max_images = 7\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_TRANSLATE_TITLES", "false")
    monkeypatch.setenv("PIXIV_MAX_IMAGES", "99")

    settings = Settings(
        _config_file=config, _env_file=None,
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
    )

    assert settings.auto_translate_titles is True
    assert settings.pixiv_max_images == 7


def test_repository_settings_reject_secrets(tmp_path):
    config = tmp_path / "bot-settings.toml"
    config.write_text('[bot]\nbot_token = "must-not-be-committed"\n', encoding="utf-8")

    try:
        Settings(
            _config_file=config, _env_file=None,
            bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
        )
    except ValueError as error:
        assert "секретные параметры" in str(error)
    else:
        raise AssertionError("A secret in bot-settings.toml must be rejected")


def test_env_file_accepts_single_quoted_channel_json(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI\n"
        "ADMIN_IDS=123456789\n"
        "DEFAULT_CHANNEL_ALIAS=artwork\n"
        'CHANNELS_JSON=\'{"artwork":{"chat_id":"-1001234567890","publish_mode":"auto"}}\'',
        encoding="utf-8",
    )
    for name in ("BOT_TOKEN", "ADMIN_IDS", "DEFAULT_CHANNEL_ALIAS", "CHANNELS_JSON"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_config_file=tmp_path / "missing.toml", _env_file=env_file)

    assert settings.default_channel_alias == "artwork"
    assert settings.channels_json["artwork"]["chat_id"] == "-1001234567890"


def test_news_api_is_opt_in_and_uses_gemma4_12b_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWS_WORKER_TOKEN", raising=False)
    settings = Settings(
        _config_file=tmp_path / "missing.toml",
        _env_file=None,
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
    )
    assert settings.news_api_enabled is False
    assert settings.news_model_name == "gemma4:12b"
    assert settings.news_api_host == "127.0.0.1"

    enabled = Settings(
        _config_file=tmp_path / "missing.toml",
        _env_file=None,
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
        news_worker_token="shared-secret",
        news_api_bind_host="0.0.0.0",
    )
    assert enabled.news_api_enabled is True
    assert enabled.news_api_bind_host == "0.0.0.0"
