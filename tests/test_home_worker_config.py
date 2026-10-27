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
