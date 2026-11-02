import asyncio
import os
import socket
import subprocess
import sys
import time

from app.api.rpc import RuntimeClient
from app.application.errors import PlatformError


class RuntimeManager(RuntimeClient):
    """Host-owned process lifecycle and receiving-node command acknowledgments."""
    def __init__(self, origin, token, store, environment, *, prebind=False):
        super().__init__(origin, token)
        self.store, self.environment = store, environment
        self.process = None
        self.monitor = None
        self.process_lock = asyncio.Lock()
        self.quiesced = False
        self.on_start=None
        self.prebind = prebind

    def status(self):
        value = {'running': self.process is not None and self.process.poll() is None,
                 'quiesced': self.quiesced, 'pid': self.process.pid if self.process else None,
                 'heartbeat_at': time.time()}
        self.store.save('runtime-status', 'runtime', value)
        return value

    async def start(self):
        async with self.process_lock:
            if self.process is not None and self.process.poll() is None:
                return
            from pathlib import Path
            identity=self.store.identity()
            if identity.get('data_root'):
                self.environment['PUBLISHER_DATA_ROOT']=identity['data_root']
            if identity.get('database_key'):
                from app.storage.assets import safe_path
                self.environment['PUBLISHER_DATABASE_PATH']=str(safe_path(Path(self.environment['PUBLISHER_DATA_ROOT']),identity['database_key']))
            database=Path(self.environment['PUBLISHER_DATABASE_PATH'])
            if not database.is_file() and self.store.identity()['role'] in {'standby','retired','recovery_required'}:
                self.quiesced=True;self.status()
                return
            import sqlite3
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as connection:
                administrators=connection.execute('SELECT telegram_user_id FROM users WHERE is_active=1 AND is_admin=1').fetchall()
            self.environment['ADMIN_IDS']=','.join(str(row[0]) for row in administrators)
            self.quiesced = False
            from app.host.secrets import SecretStore
            secrets_store=SecretStore(self.store.root)
            for name,configured in secrets_store.status().items():
                if name == 'DESKTOP_AI_CONNECTIONS':continue
                if configured or secrets_store.manages_environment():self.environment[name]=secrets_store.read(name) or ''
            from app.process import module_command
            if self.prebind:
                # Hold the listener from bind through child startup: no other
                # local process can claim the runtime port or receive its token.
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    if os.name == 'nt':
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    listener.bind(('127.0.0.1', 0))
                    listener.listen(128)
                    port = listener.getsockname()[1]
                    self.origin = f'http://127.0.0.1:{port}'
                    self.environment['PUBLISHER_RUNTIME_PORT'] = str(port)
                    environment = dict(self.environment)
                    if os.name == 'nt':
                        environment['PUBLISHER_RUNTIME_SOCKET'] = 'shared-v1'
                        # The remaining pipe is a lifetime signal, not a control
                        # channel. On host death EOF lets the runtime drain its
                        # own writers before releasing the sender lock.
                        environment['PUBLISHER_RUNTIME_PARENT_PIPE'] = '1'
                        self.process = subprocess.Popen(module_command('app.api.runtime'), env=environment,
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                        try:
                            shared = listener.share(self.process.pid)
                            self.process.stdin.write(len(shared).to_bytes(4, 'big') + shared)
                            self.process.stdin.flush()
                        except BaseException:
                            self.process.stdin.close()
                            raise
                    else:
                        environment['PUBLISHER_RUNTIME_SOCKET'] = f'fd:{listener.fileno()}'
                        self.process = subprocess.Popen(module_command('app.api.runtime'), env=environment,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            pass_fds=(listener.fileno(),))
            else:
                environment = dict(self.environment)
                environment.pop('PUBLISHER_RUNTIME_SOCKET', None)
                self.process = subprocess.Popen(module_command('app.api.runtime'), env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.status()
            for _ in range(150):
                if self.process.poll() is not None:
                    self.status()
                    raise PlatformError(503, 'runtime_start_failed', 'Runtime не запустился; проверьте журнал')
                try:
                    await self.ping()
                    if self.on_start:self.on_start()
                    self.status()
                    return
                except Exception:
                    await asyncio.sleep(.1)
            raise PlatformError(503, 'runtime_start_timeout', 'Runtime не подтвердил готовность')

    async def close_and_drain(self, migration_id):
        if self.process is None or self.process.poll() is not None:
            # A dead process alone is not proof that an unresolved Telegram permit is safe.
            if self.store.status()['active_sends']:
                raise PlatformError(503, 'unresolved_publication', 'Нужна сверка незавершённых отправок')
            self.quiesced = True
            self.status()
            return
        await super().close_and_drain(migration_id)
        self.quiesced = True
        self.status()

    async def assert_quiesced(self):
        if not self.quiesced or self.store.status()['active_sends']:
            raise PlatformError(503, 'runtime_not_quiesced', 'Приложение ещё не завершило запись')
        if self.process is not None and self.process.poll() is None:
            await super().assert_quiesced()

    async def stop(self):
        async with self.process_lock:
            await self.assert_quiesced()
            if self.process is not None and self.process.poll() is None:
                # Termination is permitted only after explicit freeze proof; no send can be killed.
                self.process.terminate()
                await asyncio.to_thread(self.process.wait, 15)
            if self.process is not None and self.process.stdin is not None:
                self.process.stdin.close()
            self.status()

    async def resume(self):
        self.store.assert_mutation_allowed()
        # Reopen the actual current database after backup/restore/handoff.
        if self.process is not None and self.process.poll() is None:
            await self.stop()
        await self.start()

    async def consume_commands(self):
        while True:
            self.status()
            for command in self.store.list('runtime-command'):
                identifier = command.get('id') or command.get('migration_id')
                if not identifier or self.store.load('runtime-result', identifier):
                    continue
                try:
                    node = self.store.identity()
                    if command.get('node_id') != node.get('node_id'):
                        raise ValueError('Команда адресована другому узлу')
                    if command.get('action') == 'stop':
                        await self.close_and_drain(identifier)
                        await self.stop()
                    elif command.get('action') == 'resume':
                        if command.get('generation') != node['generation']:
                            raise ValueError('Поколение команды устарело')
                        await self.resume()
                    else:
                        raise ValueError('Неизвестная управляющая команда')
                    self.store.save('runtime-result', identifier, {'status': 'succeeded', 'completed_at': time.time()})
                except Exception:
                    self.store.save('runtime-result', identifier, {'status': 'failed', 'error': 'Не удалось безопасно изменить состояние runtime', 'completed_at': time.time()})
            await asyncio.sleep(.5)
