import asyncio
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import local
from app.config import Settings
from app.news.worker_config import HomeWorkerSettings
from app.news.home_runtime import HomeNewsWorker
from app.news.worker_errors import LlmError
from app.runtime import InstanceLock, supervise


def test_local_mode_shares_ephemeral_secret_and_model_and_forces_loopback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "bot-settings.toml"
    config.write_text('[bot]\nnews_model_name="old-model"\nnews_api_port=9099\n'
                      '[home_worker]\nlmstudio_model="new-model"\n', encoding="utf-8")
    (tmp_path / ".env").write_text('BOT_TOKEN=\nNEWS_API_BIND_HOST=0.0.0.0\n'
                                    'HOME_WORKER_VPS_API_URL=https://remote.example\n', encoding="utf-8")
    bot, worker = local.load_local_settings(config)
    assert worker.llm_provider == "lmstudio"
    assert bot.news_model_name == worker.model_name == "new-model"
    assert worker.token.get_secret_value() == bot.news_worker_token
    assert len(bot.news_worker_token) > 30
    assert bot.news_api_bind_host == "127.0.0.1"
    assert worker.vps_api_url == "http://127.0.0.1:9099"
    assert local.load_local_settings(config)[0].news_worker_token != bot.news_worker_token


def test_instance_lock_rejects_second_start_and_can_be_reused(tmp_path):
    path = tmp_path / "publisher.lock"
    with InstanceLock(path):
        with pytest.raises(RuntimeError, match="Другой экземпляр"):
            with InstanceLock(path):
                pytest.fail("second instance obtained the lock")
    with InstanceLock(path):
        pass


@pytest.mark.parametrize("fail", [False, True])
async def test_supervisor_stops_other_services_on_exit_or_failure(fail):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def polling():
        await started.wait()
        if fail:
            raise RuntimeError("polling failed")

    if fail:
        with pytest.raises(RuntimeError, match="polling failed"):
            await supervise(worker(), polling())
    else:
        await supervise(worker(), polling())
    assert stopped.is_set()


async def test_diagnostics_never_bootstrap_telegram(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "bot-settings.toml"
    config.write_text("[bot]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("BOT_TOKEN=\n", encoding="utf-8")
    bootstrap = AsyncMock()
    monkeypatch.setattr(local, "bootstrap", bootstrap)
    monkeypatch.setattr(local, "diagnose", AsyncMock(return_value=[local.Check("ERROR", "No token")]))
    result = await local.main(local.parser().parse_args(["--check"]))
    assert result == 1
    bootstrap.assert_not_awaited()


async def test_diagnostics_detect_model_mismatch_and_missing_token(tmp_path, monkeypatch):
    settings = Settings(
        _config_file="missing.toml", _env_file=None, bot_token="", admin_ids={1},
        channels_json={"artwork": {"chat_id": "-1001"}}, storage_path=tmp_path / "storage",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'db.sqlite3').as_posix()}",
    )
    worker = HomeWorkerSettings(
        _config_file="missing.toml", _env_file=None, token="secret", llm_provider="lmstudio", lmstudio_model="missing",
    )
    monkeypatch.setattr(local, "list_model_ids", AsyncMock(return_value=["loaded"]))
    reports = await local.diagnose(settings, worker)
    errors = [r.message for r in reports if r.level == "ERROR"]
    assert any("BOT_TOKEN" in msg for msg in errors)
    assert any("модель отсутствует" in r.message and r.level == "WARN" for r in reports)


def test_sqlite_backup_includes_wal_and_does_not_copy_secrets(tmp_path):
    database = tmp_path / "source.sqlite3"
    url = f"sqlite+aiosqlite:///{database.as_posix()}"
    with closing(sqlite3.connect(database)) as original:
        original.execute("PRAGMA journal_mode=WAL")
        original.execute("CREATE TABLE test (value TEXT)")
        original.execute("INSERT INTO test VALUES ('saved')")
        original.commit()
        target = local.backup_database(url, tmp_path / "backups")
        with closing(sqlite3.connect(target)) as copied:
            assert copied.execute("SELECT value FROM test").fetchone() == ("saved",)
        assert original.execute("SELECT value FROM test").fetchone() == ("saved",)
    assert list(target.parent.iterdir()) == [target]


def test_backup_missing_database_does_not_create_an_empty_source(tmp_path):
    source = tmp_path / "absent.db"
    with pytest.raises(ValueError, match="ещё не создана"):
        local.backup_database(f"sqlite+aiosqlite:///{source.as_posix()}", tmp_path / "backups")
    assert not source.exists()


async def test_unavailable_model_does_not_lease_or_consume_task_attempts():
    api = SimpleNamespace(lease=AsyncMock(return_value=None))
    worker = HomeNewsWorker(api=api, extractor=None, ollama=None, ready=AsyncMock(return_value=False))
    assert await worker.run_once() is False
    api.lease.assert_not_awaited()


async def test_offline_lmstudio_allows_bot_start_with_fallback_but_fails_smoke_test(tmp_path, monkeypatch):
    settings = Settings(
        _config_file="missing.toml", _env_file=None, bot_token="123456:TestToken", admin_ids={1},
        channels_json={"artwork": {"chat_id": "-1001"}}, storage_path=tmp_path / "storage",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'offline.db'}",
    )
    worker = HomeWorkerSettings(
        _config_file="missing.toml", _env_file=None, token="unused", llm_provider="lmstudio", lmstudio_model="local",
    )
    monkeypatch.setattr(local, "list_model_ids", AsyncMock(side_effect=LlmError("LM Studio offline")))
    reports = await local.diagnose(settings, worker)
    assert not any(r.level == "ERROR" for r in reports)
    assert any("MyMemory" in r.message for r in reports)
    smoke = await local.diagnose(settings, worker, smoke_test=True)
    assert any(r.level == "ERROR" and "offline" in r.message for r in smoke)


async def test_existing_database_channels_work_with_empty_channels_json(tmp_path, monkeypatch):
    database = tmp_path / "existing.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE channels (is_enabled INTEGER)")
        connection.execute("INSERT INTO channels VALUES (1)")
        connection.commit()
    settings = Settings(
        _config_file="missing.toml", _env_file=None, bot_token="", admin_ids={1},
        channels_json={}, storage_path=tmp_path / "storage",
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",
    )
    worker = HomeWorkerSettings(_config_file="missing.toml", _env_file=None, token="secret")
    monkeypatch.setattr(local, "list_model_ids", AsyncMock(return_value=[]))
    reports = await local.diagnose(settings, worker)
    assert any(r.level == "OK" and "сохранённые" in r.message for r in reports)
    assert not any(r.level == "ERROR" and "CHANNELS_JSON" in r.message for r in reports)
