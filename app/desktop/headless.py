"""Private launcher for the desktop shell; it never opens a browser or tray.

The bootstrap values are exchanged over inherited pipes. The HTTP listener is
bound before its address is reported, and its transport credential does not
replace the ordinary user session, origin and CSRF checks.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx

from app.desktop.launcher import (configure_environment, configure_logs,
                                  recover_initial_moves, root_path, setup_app)
from app.runtime import InstanceLock


PROTOCOL = 1
_HEX = frozenset('0123456789abcdef')


def _valid_secret(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _bind_loopback() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt':
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def _origin(listener: socket.socket) -> str:
    return f'http://127.0.0.1:{listener.getsockname()[1]}'


def identity_proof(token: str, challenge: str, pid: int, origin: str, identity: dict) -> dict:
    fields = {'protocol': PROTOCOL, 'challenge': challenge, 'pid': pid, 'origin': origin,
              'node_id': identity.get('node_id'), 'system_id': identity.get('system_id')}
    encoded = json.dumps(fields, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {**fields, 'proof': hmac.new(token.encode('ascii'), encoded, hashlib.sha256).hexdigest()}


def write_bridge_token(control: Path, token: str) -> None:
    """Protect the reconnect credential for the current OS user, never as plaintext on Windows."""
    if not _valid_secret(token):
        raise ValueError('Invalid desktop credential')
    directory = control / 'private'
    directory.mkdir(parents=True, exist_ok=True)
    value = token.encode('ascii')
    if os.name == 'nt':
        from app.host.secrets import _dpapi
        value = _dpapi(value)
    temporary = directory / ('desktop-bridge-' + secrets.token_hex(16) + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / 'desktop-bridge.key')
    finally:
        temporary.unlink(missing_ok=True)


def _read_bridge_token(control: Path) -> str:
    path = control / 'private' / 'desktop-bridge.key'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError('Desktop credential unavailable')
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('Desktop credential permissions are not private')
    value = path.read_bytes()
    if os.name == 'nt':
        from app.host.secrets import _dpapi
        value = _dpapi(value, decrypt=True)
    token = value.decode('ascii')
    if not _valid_secret(token):
        raise ValueError('Invalid desktop credential')
    return token


def _loopback_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('Invalid host address')
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or value != f'http://127.0.0.1:{parsed.port}'):
        raise ValueError('Invalid host address')
    return value


def attach_existing(root: Path) -> dict:
    """Prove a running host owns this installation before sending it credentials."""
    control = root / 'control'
    record = json.loads((control / 'host.json').read_text(encoding='utf-8'))
    identity = json.loads((control / 'node.json').read_text(encoding='utf-8'))
    origin = _loopback_origin(record['local_url'])
    if record.get('origin') != origin or type(record.get('pid')) is not int or record['pid'] <= 0:
        raise ValueError('Invalid host record')
    if not all(isinstance(identity.get(key), str) and identity[key] for key in ('node_id', 'system_id')):
        raise ValueError('Invalid installation identity')
    token = _read_bridge_token(control)
    challenge = secrets.token_hex(32)
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
        response = client.post(origin + '/_host/desktop-identity', json={'challenge': challenge})
        response.raise_for_status()
        actual = response.json()
    expected = identity_proof(token, challenge, record['pid'], origin, identity)
    if not isinstance(actual, dict) or set(actual) != set(expected):
        raise ValueError('Invalid host proof')
    for key, value in expected.items():
        if key == 'proof':
            if not isinstance(actual[key], str) or not hmac.compare_digest(actual[key], value):
                raise ValueError('Invalid host proof')
        elif actual[key] != value:
            raise ValueError('Host identity mismatch')
    return {'origin': origin, 'pid': record['pid'], 'node_id': identity['node_id'],
            'system_id': identity['system_id'], 'token': token}


def stop_managed(root: Path, report) -> None:
    """Ask a verified host to drain, then prove both process locks are free.

    A helper process can outlive a window while a send finishes. It never
    terminates the host or runtime and never treats a dead host alone as proof
    that the runtime or an uncertain publication is safe.
    """
    from app.host.control import ControlStore

    control = root / 'control'
    node = control / 'node.json'

    def confirm_stopped() -> bool:
        from contextlib import ExitStack
        locks = ExitStack()
        try:
            for path in (root / 'desktop-setup.lock', control / 'host.lock', control / 'runtime.lock'):
                locks.enter_context(InstanceLock(path))
        except RuntimeError:
            locks.close()
            return False
        with locks:
            if node.is_file() and ControlStore(control).status()['active_sends']:
                raise ValueError('Unresolved publication requires review')
            report('stopped')
            return True

    if confirm_stopped():
        return
    # A live setup or a runtime without its host cannot be stopped by guessing
    # a PID. The shell must first close its setup pipe or report the failure.
    waiting_for_setup = False
    while True:
        try:
            with InstanceLock(root / 'desktop-setup.lock'):
                break
        except RuntimeError:
            if not waiting_for_setup:
                report('stopping')
                waiting_for_setup = True
            time.sleep(.2)
    if waiting_for_setup and confirm_stopped():
        return
    try:
        with InstanceLock(control / 'host.lock'):
            raise ValueError('Runtime is running without its host')
    except RuntimeError:
        pass
    attached = attach_existing(root)
    key = control / 'host-control.key'
    if key.is_symlink() or not key.is_file() or key.stat().st_size > 1024:
        raise ValueError('Host control credential unavailable')
    token = key.read_text(encoding='utf-8')
    if not token or len(token) > 512 or any(c.isspace() for c in token):
        raise ValueError('Invalid host control credential')
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as client:
        response = client.post(attached['origin'] + '/_host/stop', headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
    if not waiting_for_setup:
        report('stopping')
    while not confirm_stopped():
        time.sleep(.2)


def _send(nonce: str, phase: str, **values) -> None:
    print(json.dumps({'protocol': PROTOCOL, 'nonce': nonce, 'phase': phase, **values},
                     separators=(',', ':')), flush=True)


def _setup(root: Path, nonce: str, token: str) -> bool:
    import uvicorn
    with InstanceLock(root / 'desktop-setup.lock'), _bind_loopback() as listener:
        origin = _origin(listener)
        completed = threading.Event()
        app = setup_app(root, token, origin, completed.set)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=listener.getsockname()[1],
                                               access_log=False, log_config=None))

        def supervise():
            while not server.started and not server.should_exit:
                time.sleep(.05)
            if server.started:
                _send(nonce, 'setup', pid=os.getpid(), origin=origin)
            while not completed.wait(.1):
                if server.should_exit:
                    return
            # Let the initialization response be delivered before the host starts.
            time.sleep(.2)
            server.should_exit = True

        def parent_closed():
            # A crash during the wizard must release its lock without killing an
            # in-flight setup request; Uvicorn waits for active requests to finish.
            while sys.stdin.read(1024):
                pass
            server.should_exit = True

        threading.Thread(target=supervise, name='desktop-setup', daemon=True).start()
        threading.Thread(target=parent_closed, name='desktop-parent', daemon=True).start()
        server.run(sockets=[listener])
        return completed.is_set()


def main() -> None:
    # Bound input and never echo malformed input: it may contain credentials.
    try:
        line = sys.stdin.readline(4097)
        if len(line) > 4096 or not line.endswith('\n'):
            raise ValueError('Invalid bootstrap')
        bootstrap = json.loads(line)
        if (not isinstance(bootstrap, dict) or set(bootstrap) != {'protocol', 'nonce', 'token'}
                or bootstrap['protocol'] != PROTOCOL or not _valid_secret(bootstrap['nonce'])
                or not _valid_secret(bootstrap['token'])):
            raise ValueError('Invalid bootstrap')
        nonce, token = bootstrap['nonce'], bootstrap['token']
    except (ValueError, TypeError, KeyError):
        raise SystemExit('Invalid desktop bootstrap') from None

    root = root_path()
    configure_environment(root)
    configure_logs(root)
    # This shell is restricted to disposable profiles until the lifecycle and
    # installer gates are complete. Never let it alter the user's Run entries.
    os.environ['PUBLISHER_TAURI_PROTOTYPE'] = '1'
    if sys.argv[1:] == ['--stop']:
        try:
            stop_managed(root, lambda phase: _send(nonce, phase))
        except Exception:
            logging.exception('Desktop shutdown could not be confirmed')
            _send(nonce, 'stop_failed')
            raise SystemExit('Desktop shutdown could not be confirmed') from None
        return
    if sys.argv[1:]:
        raise SystemExit('Unsupported desktop operation')
    # A desktop shell must not inherit an externally configured web origin.
    os.environ.pop('PUBLISHER_ORIGIN', None)
    os.environ.pop('PUBLISHER_PORT', None)
    recover_initial_moves(root)
    if not (root / 'control/node.json').is_file() and not _setup(root, nonce, token):
        return
    from app.host.secrets import SecretStore
    from app.web import DesktopHostAlreadyRunning, run
    bot_enabled = bool(SecretStore(root / 'control').read('BOT_TOKEN'))
    with _bind_loopback() as listener:
        origin = _origin(listener)
        arguments = ['--host', '127.0.0.1', '--port', str(listener.getsockname()[1]),
                     '--upgrade', '--no-browser', *(['--enable-bot'] if bot_enabled else [])]

        def ready(identity):
            _send(nonce, 'ready', origin=origin, pid=os.getpid(), node_id=identity.get('node_id'),
                  system_id=identity.get('system_id'), token=token, attached=False)

        try:
            run(argv=arguments, desktop_socket=listener, desktop_token=token, desktop_ready=ready)
        except DesktopHostAlreadyRunning:
            # The lock is authoritative. An old or unverifiable host is never
            # contacted with a session or replaced by another scheduler.
            attached = attach_existing(root)
            _send(nonce, 'ready', **attached, attached=True)


def run() -> None:
    try:
        main()
    except Exception:
        logging.exception('Desktop backend could not start')
        # Keep credentials and user paths out of the pipe and the UI error.
        raise SystemExit('Desktop backend could not start; see diagnostics') from None


if __name__ == '__main__':
    run()
