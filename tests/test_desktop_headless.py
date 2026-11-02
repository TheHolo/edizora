"""The desktop backend is exercised with disposable data and no Telegram token."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from queue import Queue
import secrets
import subprocess
import sys
import threading
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.api.auth import AuthStore
from app.api.server import WebConfig, create_app
from app.desktop.headless import _loopback_origin, _read_bridge_token, identity_proof, write_bridge_token
from app.desktop.launcher import stop_host
from app.runtime import InstanceLock


class ActorService:
    async def validate_actor(self, actor_id):
        assert actor_id == 1

    async def user_for_telegram(self, telegram_id):
        return 1

    async def list_channels(self, actor_id):
        return {'items': []}


def test_bridge_credential_does_not_replace_user_or_csrf(tmp_path):
    token = 'a' * 64
    auth = AuthStore(tmp_path)
    auth.provision(1, 17, 'isolated-password-123')
    config = WebConfig(origin='http://localhost', admin_ids={17}, desktop_bridge_token=token)
    with TestClient(create_app(ActorService(), auth, config=config), base_url=config.origin) as client:
        assert client.get('/api/v1/channels').status_code == 403
        headers = {'X-Edizora-Bridge': token, 'Origin': config.origin}
        assert client.get('/api/v1/channels', headers=headers).status_code == 401
        login = client.post('/api/v1/auth/password', headers=headers,
                            json={'telegram_id': 17, 'password': 'isolated-password-123'})
        assert login.status_code == 200
        assert client.get('/api/v1/channels', headers=headers).status_code == 200
        assert client.post('/api/v1/auth/logout', headers=headers).status_code == 403
        assert client.post('/api/v1/auth/logout', headers={**headers, 'X-CSRF-Token': login.json()['csrf_token']}).status_code == 200
        assert client.get('/api/v1/channels', headers=headers).status_code == 401


@pytest.mark.parametrize('address', [
    'http://localhost:123', 'https://127.0.0.1:123', 'http://127.0.0.2:123',
    'http://127.0.0.1:123/path', 'http://127.0.0.1:123?x=1',
    'http://user@127.0.0.1:123', 'http://127.0.0.1:123#x',
])
def test_attach_rejects_untrusted_addresses(address):
    with pytest.raises(ValueError):
        _loopback_origin(address)


def test_reconnect_credential_and_proof_bind_installation(tmp_path):
    token = secrets.token_hex(32)
    write_bridge_token(tmp_path, token)
    assert _read_bridge_token(tmp_path) == token
    if os.name == 'nt':
        assert token.encode() not in (tmp_path / 'private/desktop-bridge.key').read_bytes()
    identity = {'node_id': 'node-a', 'system_id': 'system-a'}
    first = identity_proof(token, 'a' * 64, 123, 'http://127.0.0.1:456', identity)
    assert first['proof'] != identity_proof(token, 'b' * 64, 123, first['origin'], identity)['proof']
    assert first['proof'] != identity_proof(token, 'a' * 64, 123, first['origin'], {**identity, 'node_id': 'node-b'})['proof']


def test_setup_quiesce_rejects_late_initialization(tmp_path):
    from app.desktop.launcher import setup_app
    origin, token = 'http://127.0.0.1:8765', 'a' * 64
    with TestClient(setup_app(tmp_path, token, origin, lambda: None), base_url=origin) as client:
        assert client.post('/setup-api/quiesce', headers={'Origin': origin}).status_code == 403
        headers = {'Origin': origin, 'X-Setup-Token': token}
        response = client.post('/setup-api/quiesce', headers=headers)
        assert response.status_code == 200 and response.json() == {'completed': False}
        response = client.post('/setup-api/initialize', headers=headers, json={
            'admin_id': 1234567, 'password': 'isolated-password-123',
            'password_repeat': 'isolated-password-123',
        })
        assert response.status_code == 409
        assert not (tmp_path / 'control/node.json').exists()


async def test_setup_quiesce_waits_for_inflight_initialization(tmp_path, monkeypatch):
    import asyncio
    from app.desktop import launcher

    started, release = asyncio.Event(), asyncio.Event()
    completed = []

    async def initialize(_root, _body):
        started.set()
        await release.wait()

    monkeypatch.setattr(launcher, 'initialize', initialize)
    origin, token = 'http://127.0.0.1:8765', 'a' * 64
    app = launcher.setup_app(tmp_path, token, origin, lambda: completed.append(True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                                 headers={'Origin': origin, 'X-Setup-Token': token}) as client:
        initializing = asyncio.create_task(client.post('/setup-api/initialize', json={
            'admin_id': 1234567, 'password': 'isolated-password-123',
            'password_repeat': 'isolated-password-123',
        }))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            quiescing = asyncio.create_task(client.post('/setup-api/quiesce'))
            await asyncio.sleep(.05)
            assert not quiescing.done()
            release.set()
            assert (await initializing).status_code == 200
            assert (await quiescing).json() == {'completed': True}
            assert completed == [True]
        finally:
            release.set()


def _start(root: Path, *arguments: str):
    nonce, token = secrets.token_hex(32), secrets.token_hex(32)
    environment = {**os.environ, 'PUBLISHER_DESKTOP_ROOT': str(root)}
    executable = os.environ.get('PUBLISHER_TEST_SIDECAR')
    command = ([str(Path(executable).resolve()), '--publisher-module', 'app.desktop.headless']
               if executable else [sys.executable, '-m', 'app.desktop.headless'])
    command.extend(arguments)
    process = subprocess.Popen(command, env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    lines: Queue[str] = Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()
    process.stdin.write(json.dumps({'protocol': 1, 'nonce': nonce, 'token': token}) + '\n')
    process.stdin.flush()

    def event():
        value = json.loads(lines.get(timeout=60))
        assert value['protocol'] == 1 and value['nonce'] == nonce
        return value
    return process, token, event


def test_first_setup_login_mutation_upload_media_logout_and_attach(tmp_path):
    root = tmp_path / 'installation'
    process, token, event = _start(root)
    try:
        setup = event()
        assert setup['phase'] == 'setup'
        origin = setup['origin']
        with httpx.Client(base_url=origin, trust_env=False, timeout=30, headers={'Origin': origin}) as client:
            body = {'admin_id': 1234567, 'password': 'isolated-password-123', 'password_repeat': 'isolated-password-123'}
            assert client.post('/setup-api/initialize', json=body).status_code == 403
            response = client.post('/setup-api/initialize', json=body, headers={'X-Setup-Token': token})
            assert response.status_code == 200, response.text
        ready = event()
        assert ready['phase'] == 'ready' and ready['token'] == token and not ready['attached']
        origin = ready['origin']
        headers = {'Origin': origin, 'X-Edizora-Bridge': token}
        with httpx.Client(base_url=origin, trust_env=False, timeout=30, headers=headers) as client:
            assert httpx.get(origin + '/api/v1/channels', trust_env=False).status_code == 403
            assert client.get('/api/v1/channels').status_code == 401
            login = client.post('/api/v1/auth/password', json={'telegram_id': 1234567, 'password': 'isolated-password-123'})
            assert login.status_code == 200, login.text
            client.headers['X-CSRF-Token'] = login.json()['csrf_token']
            client.headers['Idempotency-Key'] = str(uuid4())
            channel = client.post('/api/v1/channels', json={'alias': 'fixture', 'title': 'Fixture', 'telegram_chat_id': '-1001234567'})
            assert channel.status_code == 201, channel.text
            assert len(client.get('/api/v1/channels').json()['items']) == 1
            data = io.BytesIO()
            Image.new('RGB', (16, 16), 'red').save(data, format='PNG')
            uploaded = client.post('/api/v1/assets/uploads', files={'file': ('fixture.png', data.getvalue(), 'image/png')})
            assert uploaded.status_code == 201, uploaded.text
            asset_id = uploaded.json()['id']
            media = client.get(f'/api/v1/assets/{asset_id}/content')
            assert media.status_code == 200 and media.content == data.getvalue()
            other, _, attached_event = _start(root)
            try:
                attached = attached_event()
                assert attached['phase'] == 'ready' and attached['attached']
                assert (attached['pid'], attached['origin'], attached['token']) == (ready['pid'], origin, token)
                assert other.wait(timeout=20) == 0 and process.poll() is None
            finally:
                if other.poll() is None:
                    other.terminate()
                    other.wait(timeout=10)
            assert client.post('/api/v1/auth/logout', json={}).status_code == 200
            assert client.get(f'/api/v1/assets/{asset_id}/content').status_code == 401
    finally:
        if process.poll() is None:
            try:
                stop_host(root)
                process.wait(timeout=30)
            finally:
                if process.poll() is None:
                    # The disposable fixture has no bot token or active sends.
                    process.terminate()
                    process.wait(timeout=10)


def test_authenticated_stop_waits_for_host_and_runtime_locks(tmp_path):
    root = tmp_path / 'installation'
    process, token, event = _start(root)
    try:
        setup = event()
        with httpx.Client(base_url=setup['origin'], trust_env=False, timeout=30,
                          headers={'Origin': setup['origin'], 'X-Setup-Token': token}) as client:
            response = client.post('/setup-api/initialize', json={
                'admin_id': 1234567, 'password': 'isolated-password-123',
                'password_repeat': 'isolated-password-123',
            })
            assert response.status_code == 200, response.text
        assert event()['phase'] == 'ready'
        stopper, _, stop_event = _start(root, '--stop')
        try:
            assert stop_event()['phase'] == 'stopping'
            assert stop_event()['phase'] == 'stopped'
            assert stopper.wait(timeout=20) == 0
            assert process.wait(timeout=20) == 0
        finally:
            if stopper.poll() is None:
                stopper.terminate()
                stopper.wait(timeout=10)
        with InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
            pass
        again, _, again_event = _start(root, '--stop')
        try:
            assert again_event()['phase'] == 'stopped'
            assert again.wait(timeout=20) == 0
        finally:
            if again.poll() is None:
                again.terminate()
                again.wait(timeout=10)
    finally:
        if process.poll() is None:
            try:
                stop_host(root)
                process.wait(timeout=30)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)


def test_stop_refuses_a_runtime_without_its_host(tmp_path):
    root = tmp_path / 'installation'
    root.mkdir()
    with InstanceLock(root / 'control/runtime.lock'):
        stopper, _, stop_event = _start(root, '--stop')
        try:
            assert stop_event()['phase'] == 'stop_failed'
            assert stopper.wait(timeout=20) != 0
        finally:
            if stopper.poll() is None:
                stopper.terminate()
                stopper.wait(timeout=10)


def test_stop_waits_for_setup_to_release_its_lock(tmp_path):
    root = tmp_path / 'installation'
    root.mkdir()
    with InstanceLock(root / 'desktop-setup.lock'):
        stopper, _, stop_event = _start(root, '--stop')
        try:
            assert stop_event()['phase'] == 'stopping'
            assert stopper.poll() is None
        except BaseException:
            stopper.terminate()
            stopper.wait(timeout=10)
            raise
    try:
        assert stop_event()['phase'] == 'stopped'
        assert stopper.wait(timeout=20) == 0
    finally:
        if stopper.poll() is None:
            stopper.terminate()
            stopper.wait(timeout=10)


def test_stop_refuses_to_claim_success_with_an_unresolved_send(tmp_path):
    from app.host.control import ControlStore
    root = tmp_path / 'installation'
    store = ControlStore(root / 'control')
    store.initialize(system_id=str(uuid4()), data_root=root / 'data', role='active')
    permit = store.acquire_permit(job_id=123, job_version=1, channel_id=1)
    stopper, _, stop_event = _start(root, '--stop')
    try:
        assert stop_event()['phase'] == 'stop_failed'
        assert stopper.wait(timeout=20) != 0
        assert store.validate_permit(permit)['id'] == permit['id']
    finally:
        if stopper.poll() is None:
            stopper.terminate()
            stopper.wait(timeout=10)
