import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import AuthError, AuthStore
from app.api.extras import WebPlatformService
from app.api.server import WebConfig, create_app
from app.db.models import User
from app.db.session import create_database, create_schema


def signed_init(token, timestamp, telegram_id=17):
    fields = {'auth_date': str(timestamp), 'query_id': 'test-query', 'user': json.dumps({'id': telegram_id})}
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_local_code_once_and_ttl(tmp_path):
    clock = [1000]
    store = AuthStore(tmp_path, clock=lambda: clock[0])
    code = store.local_code(1, 17)
    result = store.exchange_local(code)
    assert store.authenticate(result['token']).user_id == 1
    with pytest.raises(AuthError):
        store.exchange_local(code)
    code = store.local_code(1, 17)
    clock[0] += 61
    with pytest.raises(AuthError):
        store.exchange_local(code)


def test_long_operation_grant_is_bound_to_exact_session_id_and_actions(tmp_path):
    now=[1000]
    store=AuthStore(tmp_path,clock=lambda:now[0])
    token=store.exchange_local(store.local_code(1,17))['token']
    other=store.exchange_local(store.local_code(1,17))['token']
    principal=store.authenticate(token)
    store.grant_operation(principal,'accepted-migration',['resume','resolve'])
    now[0]+=601
    principal=store.authenticate(token)
    with pytest.raises(AuthError):store.require_fresh(principal)
    store.require_operation_action(principal,'accepted-migration','resume')
    with pytest.raises(AuthError):store.require_operation_action(principal,'another-migration','resume')
    with pytest.raises(AuthError):store.require_operation_action(principal,'accepted-migration','new-secret')
    with pytest.raises(AuthError):store.require_operation_action(store.authenticate(other),'accepted-migration','resume')
    store.logout(principal)
    with pytest.raises(AuthError):store.require_operation_action(principal,'accepted-migration','resolve')


def test_telegram_signature_time_allowlist_and_exchange_replay(tmp_path):
    clock = [1000]
    store = AuthStore(tmp_path, clock=lambda: clock[0])
    init = signed_init('bot-token', 1000)
    nonce = 'client-nonce-0123456789'
    first = store.telegram_login(init, nonce, 'bot-token', {17}, 1)
    second = store.telegram_login(init, nonce, 'bot-token', {17}, 1)
    assert first['token'] == second['token']
    for data, client_nonce, token, ids in [(init, nonce+'x', 'bot-token', {17}),
            (init, nonce, 'wrong-token', {17}), (init, nonce, 'bot-token', {18}),
            (signed_init('bot-token', 600), nonce, 'bot-token', {17}),
            (signed_init('bot-token', 1031), nonce, 'bot-token', {17})]:
        with pytest.raises(AuthError):
            store.telegram_login(data, client_nonce, token, ids, 1)
    # Plain session storage never contains the bearer or the original initData.
    assert first['token'].encode() not in (tmp_path / 'auth.sqlite').read_bytes()
    assert init.encode() not in (tmp_path / 'auth.sqlite').read_bytes()
    clock[0] += 61
    with pytest.raises(AuthError):
        store.telegram_login(init, nonce, 'bot-token', {17}, 1)


def test_password_argon2_and_revocation(tmp_path):
    store = AuthStore(tmp_path)
    store.provision(1, 17, 'a strong password 123')
    result = store.password_login(17, 'a strong password 123', 'local')
    assert store.authenticate(result['token']).telegram_id == 17
    with store.connect() as db:
        assert db.execute('SELECT password_hash FROM credentials').fetchone()[0].startswith('$argon2id$')
    store.provision(1, 17, 'another strong password')
    with pytest.raises(AuthError):
        store.authenticate(result['token'])


def test_bruteforce_limit(tmp_path):
    store = AuthStore(tmp_path)
    for _ in range(8):
        with pytest.raises(AuthError):
            store.password_login(17, 'incorrect', 'local')
    with pytest.raises(AuthError) as error:
        store.password_login(17, 'incorrect', 'local')
    assert error.value.status == 429


class ActorService:
    async def validate_actor(self, actor_id):
        if actor_id != 1:
            raise AuthError('forbidden', 'Нет права', 403)
    async def user_for_telegram(self, telegram_id):
        return 1
    async def ping(self):
        return {'status': 'ok'}
    async def list_channels(self, actor_id):
        return {'items': []}


def test_host_origin_csrf_cookie_and_worker_isolation(tmp_path):
    auth = AuthStore(tmp_path)
    app = create_app(ActorService(), auth, config=WebConfig(origin='http://localhost', admin_ids={17}))
    with TestClient(app, base_url='http://localhost') as client:
        assert client.get('/api/v1/channels').status_code == 401
        assert client.get('/api/v1/health/live', headers={'Host': 'attacker.example'}).status_code == 400
        code = auth.local_code(1, 17)
        assert client.post('/api/v1/auth/local-exchange', json={'code': code}, headers={'Origin': 'https://attacker.example'}).status_code == 403
        response = client.post('/api/v1/auth/local-exchange', json={'code': code}, headers={'Origin': 'http://localhost'})
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie']
        csrf = response.json()['csrf_token']
        assert client.get('/api/v1/channels').status_code == 200
        assert client.post('/api/v1/auth/logout', headers={'Origin': 'http://localhost'}).status_code == 403
        assert client.post('/api/v1/auth/logout', headers={'Origin': 'http://localhost', 'X-CSRF-Token': csrf}).status_code == 200
        assert client.get('/api/v1/channels', headers={'Authorization': 'Bearer worker-token', 'X-User-ID': '1'}).status_code == 401


async def test_api_create_cas_and_idempotency_with_real_sqlite(tmp_path):
    engine, sessions = create_database('sqlite+aiosqlite:///' + (tmp_path / 'content.db').as_posix())
    await create_schema(engine)
    async with sessions() as session, session.begin():
        session.add(User(id=1, telegram_user_id=17, is_active=True, is_admin=True))
    service = WebPlatformService(sessions, data_root=tmp_path)
    auth = AuthStore(tmp_path / 'control')
    auth.provision(1, 17, 'a strong password 123')
    app = create_app(service, auth, config=WebConfig(origin='http://localhost', admin_ids={17}))
    with TestClient(app, base_url='http://localhost') as client:
        login = client.post('/api/v1/auth/password', json={'telegram_id': 17, 'password': 'a strong password 123'}, headers={'Origin': 'http://localhost'})
        headers = {'Origin': 'http://localhost', 'X-CSRF-Token': login.json()['csrf_token'], 'Idempotency-Key': str(uuid4())}
        channel = client.post('/api/v1/channels', json={'alias': 'test', 'title': 'Тестовый канал', 'telegram_chat_id': '-1001'}, headers=headers)
        assert channel.status_code == 201, channel.text
        channel_id = channel.json()['id']
        enabled = client.patch(f'/api/v1/channels/{channel_id}', json={'expected_version': channel.json()['version'], 'is_enabled': True}, headers=headers)
        assert enabled.status_code == 200, enabled.text
        headers['Idempotency-Key'] = str(uuid4())
        body = {'title': 'Длинный русский текст', 'text': 'Сохранённый материал', 'channel_id': channel_id}
        first = client.post('/api/v1/publications/text-draft', json=body, headers=headers)
        second = client.post('/api/v1/publications/text-draft', json=body, headers=headers)
        assert first.status_code == second.status_code == 202, first.text
        assert first.json() == second.json()
        bad = client.post('/api/v1/publications/text-draft', json={**body, 'title': 'Иное содержимое'}, headers=headers)
        assert bad.status_code == 409, bad.text
        listing = client.get('/api/v1/publications')
        assert listing.status_code == 200, listing.text
        assert listing.json()['total'] == 1
        item = listing.json()['items'][0]
        patch = {'expected_version': item['version'], 'title': 'Новое название'}
        saved = client.patch(f"/api/v1/publications/{item['id']}", json=patch, headers=headers)
        assert saved.status_code == 200, saved.text
        conflict = client.patch(f"/api/v1/publications/{item['id']}", json=patch, headers=headers)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()['error']['code'] == 'version_conflict'
        assert 'bot_token' not in json.dumps(listing.json())
    await engine.dispose()
