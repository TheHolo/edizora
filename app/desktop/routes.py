"""Authenticated desktop controls. Reuses session, CSRF and fresh-login checks."""
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import httpx
from fastapi import Depends, Request
from pydantic import BaseModel, Field

from app.application.platform import PlatformError
from app.desktop.ai import Connection, add_connection, probe, profiles, public_profile
from app.host.secrets import SecretStore
from app.branding import APP_NAME, APP_VERSION


class TelegramInput(BaseModel):
    token: str = Field(min_length=20, max_length=256)


def install_routes(app, principal, auth, runtime, gate):
    secret_store = SecretStore(gate.root)
    mutation_lock = asyncio.Lock()

    def fresh(actor):
        auth.require_fresh(actor)
        gate.assert_mutation_allowed()

    @app.get('/api/v1/desktop/status')
    async def status(actor=Depends(principal)):
        version_path = Path(__file__).resolve().parents[2] / 'desktop-version.json'
        version = json.loads(version_path.read_text(encoding='utf-8')) if version_path.is_file() else {'version': APP_VERSION, 'channel': 'source'}
        return {"installed": True, "telegram_configured": bool(secret_store.read('BOT_TOKEN')),
                "connections": [public_profile(p) for p in profiles(secret_store)],
                "computer_required": True, "autostart": autostart_enabled(), **version}

    @app.get('/api/v1/desktop/report')
    async def report(actor=Depends(principal)):
        from fastapi.responses import JSONResponse
        import platform
        from app.db.migrations import SCHEMA_VERSION
        state = await status(actor)
        return JSONResponse({'application': APP_NAME, 'version': state['version'], 'platform': platform.system(),
            'os_version': platform.version(), 'schema_version': SCHEMA_VERSION,
            'telegram_configured': state['telegram_configured'], 'ai_connection_count': len(state['connections']),
            'publication_allowed': gate.status()['publication_allowed'], 'role': gate.identity()['role'],
            'active_sends': gate.status()['active_sends'], 'autostart': state['autostart']},
            headers={'Content-Disposition': 'attachment; filename="edizora-diagnostics.json"'})

    @app.post('/api/v1/desktop/ai/test')
    async def test(body: Connection, actor=Depends(principal)):
        fresh(actor)
        try:
            return await probe(body)
        except ValueError as exc:
            raise PlatformError(422, 'invalid_ai_connection', str(exc)) from None
        except Exception:
            raise PlatformError(422, 'ai_test_failed', 'Сервис не ответил в нужном формате. Проверьте адрес API, ключ, модель и поддержку JSON Schema. Тест мог использовать платный запрос.') from None

    @app.post('/api/v1/desktop/ai')
    async def add(body: Connection, request: Request, actor=Depends(principal)):
        fresh(actor)
        async with mutation_lock:
            try:
                command_id = request.headers.get('idempotency-key')
                if command_id and len(command_id) > 128:
                    raise ValueError('Некорректный идентификатор запроса')
                return await add_connection(runtime, secret_store, actor.user_id, body, command_id)
            except ValueError as exc:
                raise PlatformError(422, 'invalid_ai_connection', str(exc)) from None

    @app.delete('/api/v1/desktop/ai/{identifier}')
    async def remove(identifier: str, actor=Depends(principal)):
        fresh(actor)
        async with mutation_lock:
            current = profiles(secret_store)
            target = next((p for p in current if p['id'] == identifier), None)
            if not target:
                raise PlatformError(404, 'not_found', 'Подключение не найдено')
            await runtime.revoke_worker(actor.user_id, target['worker_id'])
            secret_store.write('DESKTOP_AI_CONNECTIONS', json.dumps([p for p in current if p['id'] != identifier]))
        return {'status': 'removed'}

    @app.post('/api/v1/desktop/telegram')
    async def telegram(body: TelegramInput, actor=Depends(principal)):
        fresh(actor)
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                result = await client.get('https://api.telegram.org/bot' + body.token + '/getMe')
            payload = result.json()
            if not result.is_success or not payload.get('ok'):
                raise ValueError()
            username = payload['result']['username']
        except Exception:
            raise PlatformError(422, 'telegram_unavailable', 'Не удалось проверить бота. Проверьте токен и подключение к Telegram.') from None
        async with mutation_lock:
            secret_store.write('BOT_TOKEN', body.token)
            # Restart the runtime using its normal drain; never kill a sender.
            await runtime.close_and_drain('desktop-telegram-configuration')
            await runtime.stop()
            runtime.environment['PUBLISHER_ENABLE_BOT'] = '1'
            await runtime.start()
        return {'status': 'connected', 'username': username}

    @app.post('/api/v1/desktop/stop')
    async def stop(actor=Depends(principal)):
        fresh(actor)
        # Send through the same local host control used by the installed launcher.
        from app.desktop.launcher import stop_host
        asyncio.create_task(asyncio.to_thread(stop_host, gate.root.parent))
        return {'status': 'stopping'}

    @app.post('/api/v1/desktop/autostart')
    async def autostart(request: Request, actor=Depends(principal)):
        fresh(actor)
        body = await request.json()
        if type(body.get('enabled')) is not bool:
            raise PlatformError(422, 'invalid_autostart', 'Выберите состояние автозапуска')
        try:
            set_autostart(body['enabled'])
        except OSError:
            raise PlatformError(422, 'autostart_unavailable', 'Windows не разрешил изменить автозапуск. Попробуйте включить его при установке приложения.') from None
        return {'enabled': autostart_enabled()}


def autostart_enabled():
    if os.name != 'nt':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            return any(_autostart_value(winreg, key, name) for name in ('Edizora', 'Publisher'))
    except OSError:
        return False


def set_autostart(enabled):
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        raise PlatformError(422, 'installed_app_required', 'Автозапуск доступен в установленном приложении Windows')
    import subprocess
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        # Retire the legacy entry so upgrades never launch both executables.
        try:
            winreg.DeleteValue(key, 'Publisher')
        except FileNotFoundError:
            pass
        if enabled:
            winreg.SetValueEx(key, 'Edizora', 0, winreg.REG_SZ, subprocess.list2cmdline([sys.executable, '--background']))
        else:
            try:
                winreg.DeleteValue(key, 'Edizora')
            except FileNotFoundError:
                pass


def _autostart_value(registry, key, name):
    try:
        return bool(registry.QueryValueEx(key, name)[0])
    except FileNotFoundError:
        return False
