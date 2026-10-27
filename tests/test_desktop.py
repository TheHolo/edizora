from contextlib import closing
import json
from pathlib import Path
import sqlite3

import aiohttp
import httpx
import pytest
from pydantic import ValidationError

from app.desktop.ai import Connection, public_profile
from app.desktop.launcher import SetupInput, initialize, recover_initial_moves, setup_app
from app.host.control import ControlStore, durable_json
from app.news.llm_factory import create_news_client
from app.news.worker_config import HomeWorkerSettings
from app.runtime import InstanceLock


def setup_input(**overrides):
    return SetupInput(admin_id=1234567, password='a-long-private-password',
                      password_repeat='a-long-private-password', **overrides)


async def test_first_install_is_complete_paused_and_can_be_unpaused(tmp_path):
    await initialize(tmp_path, setup_input())
    store = ControlStore(tmp_path / 'control')
    assert store.identity()['role'] == 'active'
    assert Path(store.identity()['data_root']) == tmp_path / 'data'
    assert not store.status()['publication_allowed']
    from app.api.auth import AuthStore
    with AuthStore(tmp_path / 'control').connect() as db:
        credential = db.execute('SELECT * FROM credentials').fetchone()
        assert credential['telegram_id'] == 1234567
        assert credential['password_hash'] != 'a-long-private-password'
    with closing(sqlite3.connect(tmp_path / 'data/database.db')) as db:
        assert json.loads(db.execute('SELECT publication_blocks FROM application_state').fetchone()[0]) == {'user_pause': 'user'}
    store.unblock('user_pause', 'user')
    assert store.status()['publication_allowed']
    before = store.identity()
    with pytest.raises(ValueError, match='уже содержит'):
        await initialize(tmp_path, setup_input())
    assert store.identity() == before


async def test_bad_password_never_creates_content(tmp_path):
    request = setup_input()
    request.password_repeat = 'different-password'
    with pytest.raises(ValueError, match='Пароли'):
        await initialize(tmp_path, request)
    assert not (tmp_path / 'data').exists()


async def test_arbitrary_existing_database_is_never_adopted(tmp_path):
    (tmp_path / 'data').mkdir()
    original = tmp_path / 'data/database.db'
    original.write_bytes(b'unmanaged-original')
    with pytest.raises(ValueError):
        await initialize(tmp_path, setup_input())
    assert original.read_bytes() == b'unmanaged-original'


@pytest.mark.parametrize('first_move', [False, True])
def test_recover_interrupted_initial_directory_moves(tmp_path, first_move):
    stage = tmp_path / 'first-install'
    (stage / 'data').mkdir(parents=True)
    (stage / 'control').mkdir()
    (stage / 'data/database.db').write_bytes(b'fixture')
    (stage / 'control/node.json').write_text('{}')
    durable_json(tmp_path / 'install-pending.json', {'format': 1})
    if first_move:
        (stage / 'data').rename(tmp_path / 'data')
    recover_initial_moves(tmp_path)
    assert (tmp_path / 'data/database.db').read_bytes() == b'fixture'
    assert (tmp_path / 'control/node.json').exists()
    assert not (tmp_path / 'install-pending.json').exists()


async def test_wizard_requires_launch_secret_origin_and_host(tmp_path):
    app = setup_app(tmp_path, 'local-secret', 'http://127.0.0.1:54321', lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:54321') as client:
        assert (await client.get('/')).status_code == 200
        endpoint = '/setup-api/initialize'
        assert (await client.post(endpoint, json={})).status_code == 403
        assert (await client.post(endpoint, headers={'x-setup-token': 'local-secret', 'origin': 'https://evil.example'}, json={})).status_code == 403
        assert (await client.post(endpoint, headers={'x-setup-token': 'local-secret', 'origin': 'http://127.0.0.1:54321'}, json={})).status_code == 422
        assert (await client.get('/', headers={'host': 'evil.example'})).status_code == 403
        assert not (tmp_path / 'data').exists()


def test_cloud_ai_requires_explicit_consent_and_https():
    connection = Connection(name='Cloud', base_url='https://provider.example/v1', model='test', api_key='private')
    with pytest.raises(ValueError, match='Подтвердите'):
        connection.settings()
    connection.cloud_consent = True
    settings = connection.settings()
    assert settings.openai_api_key.get_secret_value() == 'private'
    connection.base_url = 'http://provider.example/v1'
    with pytest.raises(ValidationError):
        connection.settings()


def test_local_network_openai_and_manual_model_are_supported():
    settings = Connection(name='LAN', base_url='http://192.168.1.25:8080/v1', model='my-model').settings()
    assert settings.model_name == 'my-model'
    assert settings.openai_base_url == 'http://192.168.1.25:8080/v1'


def test_profile_never_returns_credentials():
    assert public_profile({'name': 'Cloud', 'api_key': 'private', 'credential': 'worker-token'}) == {'name': 'Cloud'}


async def test_openai_factory_uses_configured_endpoint_and_key():
    settings = HomeWorkerSettings(_config_file='', _env_file=None, token='fixture', llm_provider='openai',
        openai_base_url='https://provider.example/api/v1', openai_model='remote-model', openai_api_key='private')
    async with aiohttp.ClientSession() as session:
        client = create_news_client(session, settings)
        assert client.base_url == 'https://provider.example/api/v1'
        assert client.model == 'remote-model'
        assert client._headers['Authorization'] == 'Bearer private'


async def test_update_backup_is_consistent_and_refuses_active_host(tmp_path, monkeypatch):
    await initialize(tmp_path, setup_input())
    from app.desktop.maintenance import prepare_update
    monkeypatch.setattr('app.desktop.launcher.stop_host', lambda root: (_ for _ in ()).throw(RuntimeError('do not stop fixture')))
    prepare_update(tmp_path)
    backups = list((tmp_path / 'update-backups').iterdir())
    assert len(backups) == 1
    assert json.loads((backups[0] / 'backup.json').read_text())['verified'] is True
    with closing(sqlite3.connect(backups[0] / 'data/database.db')) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with InstanceLock(tmp_path / 'control/host.lock'):
        with pytest.raises(RuntimeError, match='do not stop'):
            prepare_update(tmp_path)


async def test_restore_into_empty_app_preserves_owner_and_keeps_publications_paused(tmp_path):
    from app.deployment.snapshot import make_snapshot
    original = tmp_path / 'original'
    await initialize(original, setup_input())
    identity = ControlStore(original / 'control').identity()
    snapshot = tmp_path / 'snapshot'
    make_snapshot(original / 'data', original / 'data/database.db', snapshot,
        metadata={'system_id': identity['system_id'], 'release_id': identity['release_id'], 'schema_version': identity['schema_version'], 'backup_id': 'isolated-backup'})
    recovered = tmp_path / 'recovered'
    await initialize(recovered, setup_input(mode='restore', backup=str(snapshot), confirmed_stopped=True))
    store = ControlStore(recovered / 'control')
    assert store.identity()['system_id'] == identity['system_id']
    assert store.identity()['node_id'] != identity['node_id']
    assert not store.status()['publication_allowed']
    assert [b['reason'] for b in store.status()['blocks']] == ['user_pause']
    other = tmp_path / 'not-confirmed'
    with pytest.raises(ValueError, match='Подтвердите'):
        await initialize(other, setup_input(mode='restore', backup=str(snapshot)))
    assert not (other / 'control/node.json').exists()


async def test_connection_retry_does_not_register_another_worker(tmp_path):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from app.desktop.ai import add_connection, profiles
    from app.host.secrets import SecretStore
    runtime = SimpleNamespace(pair_worker=AsyncMock(return_value={'pairing_code': 'fixture'}),
        worker_command=AsyncMock(return_value={'worker_id': 'fixture-worker', 'credential': 'private-worker-token'}),
        ai_selection=AsyncMock())
    store = SecretStore(tmp_path)
    connection = Connection(name='Local', provider='openai', base_url='http://127.0.0.1:1234/v1', model='fixture')
    first = await add_connection(runtime, store, 1, connection, 'same-command')
    assert await add_connection(runtime, store, 1, connection, 'same-command') == first
    assert len(profiles(store)) == 1
    assert runtime.pair_worker.await_count == 1


def test_restricted_windows_registry_does_not_break_application_status(monkeypatch):
    import os
    if os.name != 'nt':
        pytest.skip('Windows registry check')
    import winreg
    from app.desktop.routes import autostart_enabled
    monkeypatch.setattr(winreg, 'OpenKey', lambda *args: (_ for _ in ()).throw(PermissionError('restricted account')))
    assert autostart_enabled() is False
