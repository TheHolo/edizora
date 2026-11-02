"""Exercise legacy import or backup restore in the installed prototype wizard.

All input is generated in a new disposable directory. The old database and
snapshot are checked for changes; no real installation or credential is read.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.desktop.headless import stop_managed
from app.desktop.launcher import SetupInput, initialize
from app.host.control import ControlStore
from app.host.secrets import ALLOWED_SECRETS, SecretStore
from app.runtime import InstanceLock
from measure_prototype import process_tree_private_bytes
from recovery_native import terminate_verified


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def prepare_legacy(root: Path) -> dict:
    from app.db.models import Channel, Job, User
    from app.db.session import create_database, create_schema

    checkout = root / 'legacy-fixture'
    (checkout / 'app').mkdir(parents=True)
    (checkout / 'app/runtime.py').write_text('def managed_checkout_marker(): pass', encoding='utf-8')
    storage = checkout / 'storage'
    storage.mkdir()
    image = storage / 'synthetic.png'
    Image.new('RGB', (8, 8), 'blue').save(image)
    database = checkout / 'data/database.db'
    database.parent.mkdir()
    engine, sessions = create_database('sqlite+aiosqlite:///' + database.as_posix())
    await create_schema(engine)
    async with sessions() as session, session.begin():
        session.add_all([User(id=7, telegram_user_id=1234567, is_admin=True, is_active=True),
                         Channel(id=9, alias='old-fixture', telegram_chat_id='-1001', title='Legacy fixture', default_tags=[])])
        await session.flush()
        session.add(Job(id=11, created_by_user_id=7, target_channel_id=9, provider='manual',
                        source_id='11', source_url='https://example.org/11', normalized_url='https://example.org/11',
                        status='publishing', queue_position=11, post_data={'media_items': []}))
    await engine.dispose()
    return {'checkout': checkout, 'storage': storage, 'database': database, 'database_hash': digest(database),
            'image_hash': digest(image)}


async def prepare_restore(root: Path) -> dict:
    from app.deployment.snapshot import make_snapshot

    source = root / 'restore-source'
    await initialize(source, SetupInput(admin_id=1234567, password='isolated-password-123',
                                        password_repeat='isolated-password-123'))
    identity = ControlStore(source / 'control').identity()
    snapshot = root / 'snapshot'
    make_snapshot(source / 'data', source / 'data/database.db', snapshot,
                  metadata={'system_id': identity['system_id'], 'release_id': identity['release_id'],
                            'schema_version': identity['schema_version'], 'backup_id': 'native-fixture'})
    return {'source': source, 'snapshot': snapshot, 'identity': identity,
            'database_hash': digest(source / 'data/database.db'), 'manifest_hash': digest(snapshot / 'manifest.json')}


def main() -> None:
    if os.name != 'nt':
        raise SystemExit('Windows is required')
    mode = os.environ.get('PUBLISHER_SETUP_MODE')
    if mode not in {'legacy', 'restore'}:
        raise SystemExit('PUBLISHER_SETUP_MODE must be legacy or restore')
    executable = Path(os.environ['PUBLISHER_TEST_APPLICATION']).resolve(strict=True)
    edge = Path(os.environ['PUBLISHER_TEST_EDGE_DRIVER']).resolve(strict=True)
    driver_binary = Path(os.environ['PUBLISHER_TEST_TAURI_DRIVER']).resolve(strict=True)
    root = Path(os.environ['PUBLISHER_NATIVE_FIXTURE'])
    if not root.is_absolute() or root.exists():
        raise SystemExit('PUBLISHER_NATIVE_FIXTURE must name a new absolute directory')
    root.mkdir(parents=True)
    root = root.resolve()
    (root / '.edizora-prototype').write_text('isolated test profile', encoding='utf-8')
    fixture = asyncio.run(prepare_legacy(root) if mode == 'legacy' else prepare_restore(root))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    blocked = ALLOWED_SECRETS | {name.upper() for name in Settings.model_fields}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in blocked and not key.upper().startswith('PUBLISHER_')}
    environment.update(EDIZORA_PROTOTYPE_ROOT=str(root), MSEDGEDRIVER_TELEMETRY_OPTOUT='1')
    driver = subprocess.Popen([str(driver_binary), '--port', str(port), '--native-driver', str(edge)],
                              env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=120, trust_env=False)
    session = None

    def call(method, path, body=None):
        response = client.request(method, path, json=body)
        value = response.json().get('value')
        if response.status_code >= 400:
            raise RuntimeError(f'WebDriver {path}: {value}')
        return value

    def invoke(command, arguments=None):
        result = call('POST', f'/session/{session}/execute/async', {
            'script': 'const [name,args,done]=arguments;window.__TAURI_INTERNALS__.invoke(name,args).then(value=>done({value}),error=>done({error:String(error)}));',
            'args': [command, arguments or {}]})
        if 'error' in result:
            raise RuntimeError(f'{command}: {result["error"]}')
        return result['value']

    def wait(predicate, description, timeout=90):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as error:
                last = str(error)
            time.sleep(.2)
        raise AssertionError(f'Timed out waiting for {description}: {last}')

    def script(source, args=None):
        return call('POST', f'/session/{session}/execute/sync', {'script': source, 'args': args or []})

    try:
        wait(lambda: call('GET', '/status'), 'driver', timeout=30)
        created = call('POST', '/session', {'capabilities': {'alwaysMatch': {
            'browserName': 'wry', 'tauri:options': {'application': str(executable)}}}})
        session = created['sessionId']
        call('POST', f'/session/{session}/timeouts', {'script': 120000})
        wait(lambda: script('return !!window.__TAURI_INTERNALS__'), 'Tauri bridge')
        wait(lambda: invoke('desktop_status')['phase'] == 'setup', 'setup')
        wait(lambda: script("return !!document.querySelector('select')"), 'setup form')
        script("const select=document.querySelector('select');select.value=arguments[0];select.dispatchEvent(new Event('change',{bubbles:true}));return true", [mode])
        selector = 'input[name=checkout]' if mode == 'legacy' else 'input[name=backup]'
        wait(lambda: script('return !!document.querySelector(arguments[0])', [selector]), f'{mode} fields')
        fields = {'admin': '1234567', 'password': 'isolated-password-123', 'repeat': 'isolated-password-123'}
        if mode == 'legacy':
            fields.update(checkout=str(fixture['checkout']), database=str(fixture['database']), storage=str(fixture['storage']))
        else:
            fields['backup'] = str(fixture['snapshot'])
        script("""
          const [selector, values] = arguments;
          const form=document.querySelector(selector).form;
          for (const [name,value] of Object.entries(values)) {
            const input=form.elements.namedItem(name); input.value=value;
            input.dispatchEvent(new Event('input',{bubbles:true}));
            input.dispatchEvent(new Event('change',{bubbles:true}));
          }
          form.elements.namedItem('confirmed').checked=true;
          form.requestSubmit(); return true;
        """, [selector, fields])
        wait(lambda: invoke('desktop_status')['phase'] == 'ready', f'{mode} completion', timeout=120)
        store = ControlStore(root / 'control')
        assert not store.status()['publication_allowed'] and not store.status()['active_sends']
        assert not SecretStore(root / 'control').read('BOT_TOKEN')
        if mode == 'legacy':
            assert digest(fixture['database']) == fixture['database_hash']
            assert digest(fixture['storage'] / 'synthetic.png') == fixture['image_hash']
            assert (fixture['checkout'] / '.publisher-managed.json').is_file()
            with sqlite3.connect((root / 'data/database.db').as_uri() + '?mode=ro', uri=True) as database:
                status, code = database.execute('SELECT status,error_code FROM jobs WHERE id=11').fetchone()
                assert (status, code) == ('failed', 'uncertain_publish')
        else:
            assert digest(fixture['source'] / 'data/database.db') == fixture['database_hash']
            assert digest(fixture['snapshot'] / 'manifest.json') == fixture['manifest_hash']
            assert store.identity()['system_id'] == fixture['identity']['system_id']
            assert store.identity()['node_id'] != fixture['identity']['node_id']
        login = invoke('desktop_request', {'method': 'POST', 'path': '/api/v1/auth/password',
            'body': {'telegram_id': 1234567, 'password': 'isolated-password-123'}, 'key': str(uuid4())})
        assert login['status'] == 200
        channels = invoke('desktop_request', {'method': 'GET', 'path': '/api/v1/channels', 'body': None, 'key': None})
        assert channels['status'] == 200
        invoke('desktop_shutdown')
        def stopped():
            try:
                with InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
                    return True
            except RuntimeError:
                return False
        wait(stopped, 'managed shutdown')
        print(f'PASS: native {mode} setup preserved the source and starts paused', flush=True)
    finally:
        try:
            _, processes = process_tree_private_bytes(driver.pid)
            for item in processes:
                if item['name'].lower() == executable.name.lower():
                    terminate_verified(item['pid'], executable)
            stop_managed(root, lambda _: None)
        finally:
            client.close()
            driver.terminate()
            driver.wait(timeout=15)


if __name__ == '__main__':
    main()
