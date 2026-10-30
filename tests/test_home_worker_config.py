import pytest
from pydantic import ValidationError

from app.news.worker_config import HomeWorkerSettings


def test_home_worker_loads_non_secret_toml_and_secret_env(tmp_path, monkeypatch):
    config = tmp_path / "worker.toml"
    config.write_text(
        """[home_worker]
vps_api_url = "https://publisher.example"
ollama_model = "gemma4:12b"
temperature = 0.05
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME_WORKER_TOKEN", "worker-secret")

    settings = HomeWorkerSettings(_config_file=config, _env_file=None)

    assert settings.vps_api_url == "https://publisher.example"
    assert settings.ollama_model == "gemma4:12b"
    assert settings.temperature == 0.05
    assert settings.token.get_secret_value() == "worker-secret"


def test_home_worker_rejects_token_in_toml(tmp_path):
    config = tmp_path / "worker.toml"
    config.write_text('[home_worker]\ntoken = "must-not-be-committed"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="HOME_WORKER_TOKEN"):
        HomeWorkerSettings(_config_file=config, _env_file=None)


def test_home_worker_rejects_network_exposed_ollama():
    with pytest.raises(ValidationError, match="never expose Ollama"):
        HomeWorkerSettings(
            _config_file="missing.toml",
            _env_file=None,
            token="secret",
            vps_api_url="https://publisher.example",
            ollama_base_url="http://192.168.1.5:11434",
        )


def test_home_worker_requires_https_for_remote_vps():
    with pytest.raises(ValidationError, match="must use HTTPS"):
        HomeWorkerSettings(
            _config_file="missing.toml",
            _env_file=None,
            token="secret",
            vps_api_url="http://publisher.example",
        )


@pytest.mark.parametrize("url, expected", [
    ("http://127.0.0.1:1234", "http://127.0.0.1:1234/v1"),
    ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
    ("http://[::1]:1234", "http://[::1]:1234/v1"),
])
def test_lmstudio_normalizes_local_base_url(url, expected):
    settings = HomeWorkerSettings(_config_file="missing.toml", _env_file=None, token="secret", lmstudio_base_url=url)
    assert settings.lmstudio_base_url == expected


@pytest.mark.parametrize("url", [
    "http://192.168.1.2:1234/v1", "https://example.com/v1",
    "http://user:password@localhost:1234/v1", "http://localhost:1234/v1?token=secret",
    "http://localhost:1234/v1/chat/completions",
])
def test_lmstudio_rejects_remote_or_invalid_base_url(url):
    with pytest.raises(ValidationError):
        HomeWorkerSettings(_config_file="missing.toml", _env_file=None, token="secret", lmstudio_base_url=url)


def test_lmstudio_secret_is_not_allowed_in_toml(tmp_path):
    config = tmp_path / "worker.toml"
    config.write_text('[home_worker]\nlmstudio_api_key="private"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="secret"):
        HomeWorkerSettings(_config_file=config, _env_file=None, token="secret")


def test_standalone_worker_rejects_empty_token():
    with pytest.raises(ValidationError, match="must not be empty"):
        HomeWorkerSettings(_config_file="missing.toml", _env_file=None, token="   ")


def test_youtube_video_settings_are_loaded_from_toml(tmp_path):
    config = tmp_path / "worker.toml"
    config.write_text(
        '[home_worker]\nyoutube_sample_fps = 5\nyoutube_max_height = 1080\n'
        'youtube_vision_parallelism = 1\nyoutube_max_source_chunks = 80\n', encoding="utf-8",
    )
    settings = HomeWorkerSettings(_config_file=config, _env_file=None, token="test")
    assert settings.youtube_video_analysis is True
    assert settings.youtube_sample_fps == 5 and settings.youtube_max_height == 1080
    assert settings.youtube_vision_parallelism == 1 and settings.youtube_max_source_chunks == 80


@pytest.mark.parametrize("settings", [
    {"youtube_sample_fps": 0}, {"youtube_max_height": 3000}, {"youtube_vision_parallelism": 5},
])
def test_youtube_video_settings_reject_invalid_limits(settings):
    with pytest.raises(ValidationError):
        HomeWorkerSettings(_config_file="missing.toml", _env_file=None, token="test", **settings)
