"""Opt-in acceptance against the real Windows executable, without Telegram or cloud calls."""
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest

from app.desktop.launcher import SetupInput, initialize

EXECUTABLE = os.environ.get('PUBLISHER_TEST_EXECUTABLE')
pytestmark = pytest.mark.skipif(not EXECUTABLE, reason='Set PUBLISHER_TEST_EXECUTABLE to test a built desktop bundle')


async def test_real_executable_runtime_ai_and_clean_shutdown(tmp_path):
    await initialize(tmp_path, SetupInput(admin_id=1234567, password='isolated-test-password', password_repeat='isolated-test-password'))
    environment = {**os.environ, 'PUBLISHER_DESKTOP_ROOT': str(tmp_path)}
    executable = str(Path(EXECUTABLE).resolve())
    process = subprocess.Popen([executable, '--background'], env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    endpoint = None
    fake = None
    try:
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            record = tmp_path / 'control/host.json'
            if record.exists():
                endpoint = json.loads(record.read_text())['origin']
                break
            if process.poll() is not None:
                break
            log = tmp_path / 'logs/edizora.log'
            if log.exists() and 'ERROR root' in log.read_text(encoding='utf-8', errors='replace'):
                break
            await asyncio.sleep(.2)
        if not endpoint:
            logs = '\n'.join(p.read_text(encoding='utf-8', errors='replace')[-6000:] for p in (tmp_path / 'logs').glob('*.log'))
            pytest.fail('Packaged host did not start: ' + logs)
        async with httpx.AsyncClient(base_url=endpoint, trust_env=False, timeout=60, headers={'Origin': endpoint}) as client:
            assert (await client.get('/api/v1/desktop/status')).status_code == 401
            response = await client.post('/api/v1/auth/password', json={'telegram_id': 1234567, 'password': 'isolated-test-password'})
            assert response.status_code == 200, response.text
            session = response.json()
            client.headers['X-CSRF-Token'] = session['csrf_token']
            status = await client.get('/api/v1/desktop/status')
            assert status.status_code == 200, status.text
            assert status.json()['telegram_configured'] is False
            assert status.json()['version'] == '0.1'
            page = await client.get('/')
            assert page.status_code == 200 and 'assets/' in page.text
            assert '<title>Edizora</title>' in page.text

            class AIHandler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    self.reply({'data': [{'id': 'fixture-model'}]})

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    assert body['model'] == 'fixture-model'
                    assert body['response_format']['type'] == 'json_schema'
                    self.reply({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'translated_text': 'Test title', 'source_language': 'ru'})}}]})

                def reply(self, value):
                    data = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            fake = ThreadingHTTPServer(('127.0.0.1', 0), AIHandler)
            threading.Thread(target=fake.serve_forever, daemon=True).start()
            connection = {'name': 'Isolated AI', 'provider': 'openai', 'base_url': f'http://127.0.0.1:{fake.server_port}/v1', 'model': 'fixture-model', 'api_key': 'isolated-private-key'}
            checked = await client.post('/api/v1/desktop/ai/test', json=connection)
            assert checked.status_code == 200, checked.text
            added = await client.post('/api/v1/desktop/ai', json=connection)
            assert added.status_code == 200, added.text
            assert 'api_key' not in added.json() and 'credential' not in added.json()
            # The host-owned worker advertises its model without any pairing command.
            workers = (await client.get('/api/v1/ai/workers')).json()
            assert 'fixture-model' in json.dumps(workers)
            assert 'isolated-private-key' not in (await client.get('/api/v1/desktop/status')).text

            channel = await client.post('/api/v1/channels', json={'alias': 'fixture', 'title': 'Test channel', 'telegram_chat_id': '-1001234567'}, headers={'Idempotency-Key': str(uuid4())})
            assert channel.status_code == 201, channel.text
            enabled = await client.patch(f"/api/v1/channels/{channel.json()['id']}", json={'expected_version': channel.json()['version'], 'is_enabled': True})
            assert enabled.status_code == 200, enabled.text
            created = await client.post('/api/v1/publications/text-draft', json={'content_kind': 'text', 'channel_id': channel.json()['id'], 'title': 'Тестовое название', 'text': 'Isolated body'}, headers={'Idempotency-Key': str(uuid4())})
            assert created.status_code == 202, created.text
            job_id = created.json()['publication_id']
            publication = (await client.get(f'/api/v1/publications/{job_id}')).json()
            operation = await client.post(f'/api/v1/publications/{job_id}/translate-title', json={'expected_version': publication['version']}, headers={'Idempotency-Key': str(uuid4())})
            assert operation.status_code == 202, operation.text
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                publication = (await client.get(f'/api/v1/publications/{job_id}')).json()
                if publication.get('title_translation') == 'Test title':
                    break
                await asyncio.sleep(.5)
            assert publication.get('title_translation') == 'Test title', publication
            report = await client.get('/api/v1/desktop/report')
            assert report.status_code == 200
            assert report.json()['application'] == 'Edizora'
            assert report.json()['version'] == '0.1'

            # A second application launch must reuse this host and exit.
            repeated = subprocess.Popen([executable, '--background'], env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            assert await asyncio.to_thread(repeated.wait, 15) == 0
            assert process.poll() is None
            assert (await client.get('/api/v1/desktop/status')).status_code == 200
            assert (await client.post('/api/v1/desktop/stop', json={})).status_code == 200
        assert await asyncio.to_thread(process.wait, 30) == 0
    finally:
        if fake:
            fake.shutdown()
            fake.server_close()
        if process.poll() is None:
            try:
                from app.desktop.launcher import stop_host
                await asyncio.to_thread(stop_host, tmp_path)
                await asyncio.to_thread(process.wait, 15)
            except Exception:
                # Isolated fixture has no bot token and cannot publish anything.
                process.terminate()
                await asyncio.to_thread(process.wait, 10)
