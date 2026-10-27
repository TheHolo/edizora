"""Managed web launcher. Host state, content and runtime have separate lifetimes."""
import argparse
import asyncio
import getpass
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.api.auth import AuthStore
from app.api.rpc import RuntimeClient
from app.api.server import WebConfig, create_app
from app.db.session import create_database, create_schema
from app.host.control import ControlStore, durable_json
from app.runtime import InstanceLock


def paths():
    default = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'Publisher'
    control = Path(os.environ.get('PUBLISHER_CONTROL_DIR', default / 'control')).resolve()
    data = Path(os.environ.get('PUBLISHER_DATA_ROOT', default / 'data')).resolve()
    database = Path(os.environ.get('PUBLISHER_DATABASE_PATH', data / 'database.db')).resolve()
    node_path=control/'node.json'
    if node_path.is_file():
        try:
            node=json.loads(node_path.read_text(encoding='utf-8'))
            data=Path(node['data_root']).resolve()
            if node.get('database_key'):
                from app.storage.assets import safe_path
                database=safe_path(data,node['database_key'])
        except (ValueError,KeyError,TypeError):
            raise SystemExit('Не удалось проверить пути идентичности узла; требуется восстановление control state.') from None
    return control, data, database


async def trusted_setup(options, store, auth, data, database):
    from app.db.models import User,ApplicationState
    from app.services.job_service import JobService
    from sqlalchemy import select
    if options.initialize and not options.admin_id:
        raise SystemExit('Для первичной настройки укажите --admin-id <Telegram ID>.')
    activation_intent = None
    initial_password = None
    if getattr(options, 'activate_initial_installation', False):
        if not options.initialize:
            raise SystemExit('--activate-initial-installation требует --initialize и пустую установку.')
        from app.host.bootstrap import prepare_initial_activation
        activation_intent = prepare_initial_activation(store, data, database, host_lock=options._host_lock)
        if options.set_password:
            initial_password = getpass.getpass('Новый пароль панели (минимум 12 символов): ')
            if initial_password != getpass.getpass('Повторите пароль: '):
                raise SystemExit('Пароли не совпали.')
            if not 12 <= len(initial_password) <= 1024:
                raise SystemExit('Пароль должен содержать от 12 до 1024 символов.')
    if options.initialize and database.exists() and not store.node_path.exists():
        raise SystemExit('Обнаружена существующая БД без идентичности узла. Используйте проверенный план восстановления/переноса; автоматическая активация запрещена.')
    data.mkdir(parents=True, exist_ok=True)
    engine, sessions = create_database('sqlite+aiosqlite:///' + database.as_posix())
    try:
        if options.initialize or options.upgrade:
            if options.upgrade and not store.node_path.exists():
                raise SystemExit('Обновление требует проверенной идентичности установки.')
            if store.status()['active_sends']:
                raise SystemExit('Сначала корректно остановите текущие отправки.')
            await create_schema(engine)
        if options.initialize:
            user = await JobService(sessions).ensure_user(options.admin_id, None, str(options.admin_id))
            if not store.node_path.exists():
                from uuid import uuid4
                from app.db.migrations import SCHEMA_VERSION
                release_manifest=Path(__file__).resolve().parents[1]/'release-manifest.json'
                release_id=json.loads(release_manifest.read_text(encoding='utf-8'))['release_id'] if release_manifest.is_file() else 'development'
                store.initialize(system_id=str(uuid4()), data_root=data, role='standby' if options.standby else 'active', schema_version=SCHEMA_VERSION,release_id=release_id)
            async with sessions() as session,session.begin():
                state=await session.get(ApplicationState,1)
                if state is None:
                    session.add(ApplicationState(id=1,system_id=store.identity()['system_id'],version=1,settings={},publication_blocks={},maintenance_state='normal'))
                elif state.system_id!=store.identity()['system_id']:
                    raise SystemExit('SQLite принадлежит другой системе. Активация запрещена; требуется проверенный план восстановления.')
            if options.adopt_checkout:
                from app.runtime import mark_managed_checkout
                mark_managed_checkout(store.root)
        if options.set_password:
            if not options.admin_id:
                raise SystemExit('Для настройки пароля укажите --admin-id <Telegram ID>.')
            async with sessions() as session:
                user = await session.scalar(select(User).where(User.telegram_user_id == options.admin_id, User.is_active.is_(True), User.is_admin.is_(True)))
                if not user:
                    raise SystemExit('Такого активного администратора нет в БД.')
            password = initial_password
            if password is None:
                password = getpass.getpass('Новый пароль панели (минимум 12 символов): ')
                if password != getpass.getpass('Повторите пароль: '):
                    raise SystemExit('Пароли не совпали.')
            auth.provision(user.id, user.telegram_user_id, password)
        if activation_intent is not None:
            from app.host.bootstrap import commit_initial_activation
            commit_initial_activation(store, activation_intent, host_lock=options._host_lock)
    finally:
        await engine.dispose()


def run():
    parser = argparse.ArgumentParser(description='Publisher — управляемая веб-панель')
    parser.add_argument('--initialize', action='store_true', help='Доверенная первичная настройка пустой установки')
    parser.add_argument('--activate-initial-installation', action='store_true', help='Явно активировать пустой standby после установки; только вместе с --initialize')
    parser.add_argument('--upgrade', action='store_true', help='Обновление схемы при остановленном runtime')
    parser.add_argument('--admin-id', type=int)
    parser.add_argument('--set-password', action='store_true')
    parser.add_argument('--setup-only', action='store_true')
    parser.add_argument('--adopt-checkout',action='store_true',help='Явно заблокировать прежний launcher в этом каталоге после перехода на managed host')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--standby', action='store_true')
    parser.add_argument('--enable-bot', action='store_true', help='Включить настроенные Telegram polling и публикатор')
    parser.add_argument('--stop', action='store_true', help='Корректно завершить управляемую установку')
    parser.add_argument('--host', default=os.environ.get('PUBLISHER_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PUBLISHER_PORT', '8000')))
    options = parser.parse_args()
    if options.activate_initial_installation and (not options.initialize or options.standby):
        parser.error('--activate-initial-installation требует --initialize без --standby')
    control, data, database = paths()
    control.mkdir(parents=True, exist_ok=True)
    store, auth = ControlStore(control), AuthStore(control)
    origin = os.environ.get('PUBLISHER_ORIGIN', f'http://127.0.0.1:{options.port}').rstrip('/')
    host_record = control / 'host.json'
    if options.stop:
        if not host_record.exists():
            raise SystemExit('Управляющая служба не найдена.')
        record = json.loads(host_record.read_text(encoding='utf-8'))
        try:
            response = httpx.post(record['local_url'] + '/_host/stop', headers={'Authorization': 'Bearer ' + (control / 'host-control.key').read_text()}, timeout=5, trust_env=False)
            response.raise_for_status()
            print('Остановка принята. Текущие отправки завершатся перед выходом.')
        except httpx.HTTPError:
            raise SystemExit('Нет связи с управляющей службой. Автоматическое уничтожение процесса запрещено.') from None
        return
    lock = InstanceLock(control / 'host.lock')
    try:
        lock.__enter__()
    except RuntimeError:
        if options.initialize or (options.upgrade and os.environ.get('PUBLISHER_DESKTOP') != '1') or options.set_password:
            raise SystemExit('Сначала корректно остановите работающий host.')
        if options.no_browser and os.environ.get('PUBLISHER_DESKTOP') == '1':
            return
        if not options.no_browser and host_record.exists():
            existing = json.loads(host_record.read_text(encoding='utf-8'))
            if options.admin_id:
                import sqlite3
                with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
                    user = connection.execute('SELECT id FROM users WHERE telegram_user_id=? AND is_active=1 AND is_admin=1', (options.admin_id,)).fetchone()
                if user:
                    webbrowser.open(existing['origin'] + '/#code=' + auth.local_code(user[0], options.admin_id))
                    return
            webbrowser.open(existing['origin'])
            return
        raise SystemExit('Эта установка уже запущена. Второй публикатор не создан.')
    try:
        options._host_lock = lock
        if options.initialize or options.upgrade or options.set_password:
            if options.upgrade and os.environ.get('PUBLISHER_DESKTOP') == '1' and store.identity()['role'] != 'active':
                options.upgrade = False
            asyncio.run(trusted_setup(options, store, auth, data, database))
            if os.environ.get('PUBLISHER_DESKTOP') == '1' and store.identity()['role'] == 'active':
                from app.desktop.maintenance import register_bundled_release
                register_bundled_release(store)
        if options.setup_only:
            print('Настройка завершена; фоновые исполнители не запускались.')
            return
        if not store.node_path.exists() or (not database.exists() and store.identity()['role']=='active'):
            raise SystemExit('Сначала выполните доверенную настройку: python -m app.web --initialize --admin-id ID --setup-only')
        if options.host not in {'127.0.0.1', '::1', 'localhost'} and not origin.startswith('https://'):
            raise SystemExit('Внешний доступ требует явно настроенного HTTPS PUBLISHER_ORIGIN.')
        if options.enable_bot:
            candidates=[os.environ.get(name) for name in ('OneDrive','OneDriveConsumer','OneDriveCommercial') if os.environ.get(name)]
            synchronized=any(data.is_relative_to(Path(candidate).resolve()) for candidate in candidates)
            synchronized=synchronized or any(part.casefold()=='onedrive' or part.casefold().startswith('onedrive - ') for part in data.parts)
            remote=str(data).startswith('\\\\')
            if os.name=='nt':
                import ctypes
                remote=remote or ctypes.windll.kernel32.GetDriveTypeW(str(data.anchor))==4
            if synchronized or remote or not database.is_relative_to(data):
                raise SystemExit('Рабочие публикации требуют локального data root вне OneDrive/UNC; SQLite должна находиться внутри него. Сначала составьте план переноса данных.')
        if options.standby and store.identity()['role'] == 'active':
            raise SystemExit('Активный узел нельзя превратить в standby параметром запуска. Используйте план переноса.')
        _serve(options, control, data, database, store, auth, origin)
    finally:
        lock.__exit__()


def _serve(options, control, data, database, store, auth, origin):
    import uvicorn
    if options.enable_bot:
        from dotenv import load_dotenv
        load_dotenv(override=False)
        from app.host.secrets import SecretStore
        SecretStore(store.root).bootstrap_environment(dict(os.environ))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        runtime_port = probe.getsockname()[1]
    runtime_token, local_token = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    (control / 'host-control.key').write_text(local_token)
    (control / 'host-control.key').chmod(0o600)
    env = {**os.environ, 'PUBLISHER_CONTROL_DIR': str(control), 'PUBLISHER_DATA_ROOT': str(data),
           'PUBLISHER_DATABASE_PATH': str(database), 'PUBLISHER_RUNTIME_TOKEN': runtime_token,
           'PUBLISHER_RUNTIME_PORT': str(runtime_port), 'PUBLISHER_ENABLE_BOT': '1' if options.enable_bot else '0'}
    from app.host.runtime_manager import RuntimeManager
    runtime = RuntimeManager(f'http://127.0.0.1:{runtime_port}', runtime_token, store, env)
    admin_ids = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()}
    if options.admin_id:
        admin_ids.add(options.admin_id)
    # Existing active administrators are authoritative; no unauthenticated bootstrap writes.
    import sqlite3
    rows=[]
    if database.is_file():
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            rows = connection.execute('SELECT id,telegram_user_id FROM users WHERE is_active=1 AND is_admin=1').fetchall()
    admin_ids.update(x[1] for x in rows)
    config = WebConfig(origin=origin, allowed_hosts={urlsplit(origin).hostname, '127.0.0.1', 'localhost', '::1'},
                       bot_token=os.environ.get('BOT_TOKEN', ''), admin_ids=admin_ids,
                       desktop=os.environ.get('PUBLISHER_DESKTOP') == '1',
                       frontend=Path(os.environ.get('PUBLISHER_FRONTEND_DIR', 'frontend/dist')))
    from app.host.secrets import SecretStore
    secret_store=SecretStore(store.root)
    config.bot_token=secret_store.read('BOT_TOKEN') or ('' if secret_store.manages_environment() else config.bot_token)
    def refresh_identity():
        nonlocal database,data
        data=Path(runtime.environment['PUBLISHER_DATA_ROOT'])
        database=Path(runtime.environment['PUBLISHER_DATABASE_PATH'])
        deployment.data_root=data;deployment.db_path=database
        if not database.is_file():return
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
            current=connection.execute('SELECT telegram_user_id FROM users WHERE is_active=1 AND is_admin=1').fetchall()
        config.admin_ids={row[0] for row in current}
        config.bot_token=SecretStore(store.root).read('BOT_TOKEN') or ('' if SecretStore(store.root).manages_environment() else config.bot_token)
    runtime.on_start=refresh_identity
    from app.deployment.controller import DeploymentService
    deployment = DeploymentService(store, data, database, runtime_hooks=runtime)

    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        desktop_ai = None
        tray = None
        if config.desktop:
            from app.desktop.ai import run_connections
            from app.desktop.tray import start_tray
            desktop_ai = asyncio.create_task(run_connections(runtime, control), name='desktop-ai')
            tray = start_tray(control.parent)
        manager_task = asyncio.create_task(runtime.consume_commands(), name='host-runtime-manager')
        deployment.recover_pending()
        scheduler_task=asyncio.create_task(deployment.scheduler_loop(),name='host-backup-scheduler')
        durable_json(control / 'host.json', {'origin': origin, 'local_url': f'http://127.0.0.1:{options.port}', 'pid': os.getpid()})
        if not options.no_browser:
            chosen = next((row for row in rows if row[1] == options.admin_id), rows[0] if len(rows) == 1 else None)
            url = origin
            if chosen and urlsplit(origin).hostname in {'127.0.0.1', 'localhost', '::1'}:
                url += '/#code=' + auth.local_code(*chosen)
            webbrowser.open(url)
        try:
            yield
        finally:
            if tray:
                tray.stop()
            if desktop_ai:
                desktop_ai.cancel()
                await asyncio.gather(desktop_ai, return_exceptions=True)
            manager_task.cancel()
            scheduler_task.cancel()
            await asyncio.gather(manager_task,scheduler_task, return_exceptions=True)
            # A browser closing never reaches this block. Host shutdown drains all writers first.
            await runtime.close_and_drain('host-shutdown')
            await runtime.stop()

    app = create_app(runtime, auth, config=config, deployment=deployment, gate=store, lifespan=lifespan)
    server = uvicorn.Server(uvicorn.Config(app, host=options.host, port=options.port, workers=1, access_log=False))

    @app.post('/_host/stop', include_in_schema=False)
    async def stop(request: __import__('fastapi').Request):
        from fastapi.responses import JSONResponse
        if (request.client.host not in {'127.0.0.1', '::1'} or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + local_token)):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        server.should_exit = True
        return {'status': 'stopping'}

    server.run()


if __name__ == '__main__':
    run()
