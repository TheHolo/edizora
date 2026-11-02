import asyncio
import hmac
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.extras import WebPlatformService
from app.api.rpc import METHODS, decode, encode
from app.application.platform import PlatformError
from app.db.session import create_database
from app.host.control import ControlStore
from app.runtime import InstanceLock


def runtime_app():
    control = Path(os.environ['PUBLISHER_CONTROL_DIR'])
    data = Path(os.environ['PUBLISHER_DATA_ROOT'])
    db_path = Path(os.environ['PUBLISHER_DATABASE_PATH'])
    token = os.environ['PUBLISHER_RUNTIME_TOKEN']
    gate = ControlStore(control)
    state = {'active': 0, 'closing': False, 'quiesced': False, 'runner': None, 'application': None, 'poller': None, 'polling_task': None}
    lock = InstanceLock(control / 'runtime.lock')
    engine, sessions = create_database('sqlite+aiosqlite:///' + db_path.as_posix())
    service = WebPlatformService(sessions, data_root=data, gate=gate)

    async def drain():
        state['closing'] = True
        application = state['application']
        if state['poller']:
            await state['poller'].close_and_drain()
            if state['polling_task']:
                await state['polling_task']
        if application:
            if application.news_api:
                await application.news_api.stop()
            await application.channel_stats.stop()
            await application.workers.stop()
        if state['runner']:
            await state['runner'].stop()
        while state['active']:
            await asyncio.sleep(.05)
        if gate.status()['active_sends']:
            raise PlatformError(503, 'active_send_remaining', 'Незавершённая отправка требует сверки перед снимком')
        state['quiesced'] = True

    @asynccontextmanager
    async def lifespan(app):
        with lock:
            from app.operations.recovery import recover_orphaned_permits
            await recover_orphaned_permits(sessions,gate,runtime_lock=lock)
            from app.db.models import ApplicationState
            async with sessions() as session:
                identity_state=await session.get(ApplicationState,1)
                if identity_state is not None and identity_state.system_id!=gate.identity()['system_id']:
                    raise PlatformError(503,'system_identity_mismatch','SQLite принадлежит другой системе; фоновые исполнители запрещены')
            if gate.identity().get('role') == 'active' and all(x['reason'] == 'user_pause' for x in gate.status()['blocks']):
                await service.apply_overdue_policy()
                application = None
                if os.environ.get('PUBLISHER_ENABLE_BOT') == '1':
                    from app.bootstrap import bootstrap
                    from app.config import Settings
                    settings = Settings(database_url='sqlite+aiosqlite:///' + db_path.as_posix(), storage_path=data)
                    application = await bootstrap(settings, gate=gate)
                    state['application'] = application
                    service.bind_runtime(application)
                    from app.bot.menu import configure_bot_ui
                    await configure_bot_ui(application.bot,panel_url=os.environ.get('PUBLISHER_PANEL_URL'))
                    # Legacy worker API is exposed through the authenticated host adapter, not another public server.
                    await application.workers.start()
                    await application.channel_stats.start()
                    poller = application.dispatcher
                    state['poller'] = poller
                    state['polling_task'] = asyncio.create_task(poller.start_polling(application.bot,
                        allowed_updates=poller.resolve_used_update_types(), close_bot_session=False, handle_signals=False), name='durable-polling')
                from app.operations.runner import OperationRunner
                state['runner'] = OperationRunner(service, application)
                await state['runner'].start()
            try:
                yield
            finally:
                await drain()
                application = state['application']
                if application:
                    await application.http.close()
                    if application.local_http:
                        await application.local_http.close()
                    await application.bot.session.close()
                    await application.engine.dispose()
                await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None)

    @app.post('/rpc/upload')
    async def upload_stream(request: Request):
        if (not request.client or request.client.host not in {'127.0.0.1', '::1'}
                or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token)):
            return JSONResponse({'error': {'code': 'unauthorized', 'message': 'Invalid host credential'}}, status_code=401)
        try:
            actor_id = int(request.query_params['actor_id'])
            filename = request.query_params.get('filename', 'upload')
            if actor_id <= 0 or len(filename) > 255 or any(ord(c) < 32 for c in filename):
                raise ValueError('Invalid upload metadata')
        except (KeyError, ValueError):
            return JSONResponse({'error': {'code': 'invalid_upload', 'message': 'Invalid upload metadata'}}, status_code=422)
        if state['closing']:
            return JSONResponse({'error': {'code': 'maintenance', 'message': 'Приложение остановлено для переноса'}}, status_code=503)
        state['active'] += 1
        try:
            directory = control / 'private' / 'uploads'
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise ValueError('Invalid upload staging directory')
            if os.name != 'nt':
                directory.chmod(0o700)
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b', dir=directory) as stream:
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 47 * 1024 * 1024:
                        return JSONResponse({'error': {'code': 'upload_too_large', 'message': 'Файл превышает допустимый размер'}}, status_code=413)
                    await asyncio.to_thread(stream.write, chunk)
                stream.seek(0)
                result = await service.upload_asset_stream(actor_id, stream, filename=filename)
                return JSONResponse(result)
        except PlatformError as error:
            return JSONResponse({'error': {'code': error.code, 'message': error.message}}, status_code=error.status)
        except Exception:
            return JSONResponse({'error': {'code': 'runtime_error', 'message': 'Операция не завершена. Проверьте состояние приложения.'}}, status_code=500)
        finally:
            state['active'] -= 1

    @app.post('/rpc')
    async def rpc(request: Request):
        if request.client and request.client.host not in {'127.0.0.1', '::1'}:
            return JSONResponse({'error': {'code': 'local_only', 'message': 'Local transport required'}}, status_code=403)
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return JSONResponse({'error': {'code': 'unauthorized', 'message': 'Invalid host credential'}}, status_code=401)
        if int(request.headers.get('content-length', 0)) > 65 * 1024 * 1024:
            return JSONResponse({'error': {'code': 'payload_too_large', 'message': 'Payload too large'}}, status_code=413)
        body = await request.json()
        method = body.get('method')
        args, kwargs = decode(body.get('args', [])), decode(body.get('kwargs', {}))
        try:
            if method == '_maintenance':
                state['closing']=True
                return {'accepted':True,'active_requests':state['active'],'active_sends':gate.status()['active_sends']}
            if method == '_drain':
                await drain()
                return {'quiesced': True}
            if method == '_quiesced':
                return {'quiesced': state['quiesced'], 'active_requests': state['active']}
            if method not in METHODS:
                raise PlatformError(404, 'rpc_method_unknown', 'Неизвестная команда')
            readonly = method in {'validate_actor', 'user_for_telegram', 'ping', 'list_publications', 'get_publication',
                'list_channels', 'get_channel', 'queue', 'calendar', 'settings', 'operations', 'operation', 'operation_events',
                'preview', 'album', 'history', 'audit', 'asset_metadata', 'asset_content', 'ai_workers', 'ai_models', 'ai_tasks', 'system_status'}
            if state['closing'] and not readonly and method != 'prepare_handoff':
                raise PlatformError(503, 'maintenance', 'Приложение остановлено для переноса')
            state['active'] += 1
            try:
                result = await getattr(service, method)(*args, **kwargs)
                return JSONResponse(encode(result))
            finally:
                state['active'] -= 1
        except PlatformError as error:
            return JSONResponse({'error': {'code': error.code, 'message': error.message,
                'current_version': error.current_version, 'current': error.current}}, status_code=error.status)
        except Exception:
            return JSONResponse({'error': {'code': 'runtime_error', 'message': 'Операция не завершена. Проверьте состояние приложения.'}}, status_code=500)

    return app


def run():
    import uvicorn
    import socket
    import sys
    import threading
    inherited = os.environ.pop('PUBLISHER_RUNTIME_SOCKET', '')
    parent_pipe = os.environ.pop('PUBLISHER_RUNTIME_PARENT_PIPE', '')
    if parent_pipe and (parent_pipe != '1' or inherited != 'shared-v1' or os.name != 'nt'):
        raise SystemExit('Invalid runtime parent monitor')
    if inherited == 'shared-v1' and os.name == 'nt':
        length = int.from_bytes(sys.stdin.buffer.read(4), 'big')
        if not 1 <= length <= 4096:
            raise SystemExit('Invalid runtime listener')
        shared = sys.stdin.buffer.read(length)
        if len(shared) != length:
            raise SystemExit('Incomplete runtime listener')
        listener = socket.fromshare(shared)
    elif inherited.startswith('fd:') and os.name != 'nt':
        listener = socket.socket(fileno=int(inherited[3:]))
    elif inherited:
        raise SystemExit('Invalid runtime listener')
    else:
        uvicorn.run(runtime_app(), host='127.0.0.1', port=int(os.environ['PUBLISHER_RUNTIME_PORT']), workers=1, access_log=False)
        return
    with listener:
        if listener.getsockname() != ('127.0.0.1', int(os.environ['PUBLISHER_RUNTIME_PORT'])):
            raise SystemExit('Runtime listener identity mismatch')
        server = uvicorn.Server(uvicorn.Config(runtime_app(), host='127.0.0.1',
            port=int(os.environ['PUBLISHER_RUNTIME_PORT']), workers=1, access_log=False, log_config=None))
        if parent_pipe:
            def watch_parent():
                try:
                    while sys.stdin.buffer.read(1):
                        pass
                except (OSError, ValueError):
                    pass
                # Uvicorn invokes lifespan shutdown, which waits for the
                # admitted work and sender drain. Never terminate on a timer.
                server.should_exit = True
            threading.Thread(target=watch_parent, name='runtime-parent', daemon=True).start()
        server.run(sockets=[listener])


if __name__ == '__main__':
    run()
