"""Windows crash/reconnect smoke against an installed, disposable prototype.

The test creates a new profile, pauses publication, and terminates only processes
whose executable paths and host identity match that profile's test installation.
It never accesses a user's installation or a Telegram credential.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.desktop.headless import attach_existing, stop_managed
from app.config import Settings
from app.host.control import ControlStore
from app.host.secrets import ALLOWED_SECRETS, SecretStore
from app.runtime import InstanceLock
from measure_prototype import process_tree_private_bytes


def terminate_verified(pid: int, expected: Path) -> None:
    """Terminate the already-open test process handle, never a recycled PID."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                   wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0001 | 0x1000, False, pid)
    if not handle:
        raise OSError('Cannot inspect the disposable test process')
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            raise OSError('Cannot verify the test process image')
        if Path(buffer.value).resolve() != expected.resolve():
            raise AssertionError('Refusing to terminate a process outside the test installation')
        if not kernel.TerminateProcess(handle, 1):
            raise OSError('Cannot terminate the verified test process')
    finally:
        kernel.CloseHandle(handle)


def main() -> None:
    if os.name != 'nt':
        raise SystemExit('Windows is required')
    executable = Path(os.environ['PUBLISHER_TEST_APPLICATION']).resolve(strict=True)
    backend = (executable.parent / 'backend/edizora-backend.exe').resolve(strict=True)
    edge = Path(os.environ['PUBLISHER_TEST_EDGE_DRIVER']).resolve(strict=True)
    driver_binary = Path(os.environ['PUBLISHER_TEST_TAURI_DRIVER']).resolve(strict=True)
    root = Path(os.environ['PUBLISHER_NATIVE_FIXTURE'])
    if not root.is_absolute() or root.exists():
        raise SystemExit('PUBLISHER_NATIVE_FIXTURE must name a new absolute directory')
    root.mkdir(parents=True)
    root = root.resolve()
    (root / '.edizora-prototype').write_text('isolated test profile', encoding='utf-8')
    window_test = os.environ.get('PUBLISHER_NATIVE_WINDOW_STATE') == '1'
    if window_test:
        # A disconnected monitor should never strand the title bar off-screen.
        (root / 'window-state.json').write_text(json.dumps({
            'version': 1, 'x': 100000, 'y': 100000, 'width': 1100, 'height': 700,
        }), encoding='utf-8')
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
            'script': 'const [name,args,done]=arguments; window.__TAURI_INTERNALS__.invoke(name,args).then(value=>done({value}),error=>done({error:String(error)}));',
            'args': [command, arguments or {}],
        })
        if 'error' in result:
            raise RuntimeError(f'{command}: {result["error"]}')
        return result['value']

    def new_session():
        nonlocal session
        value = call('POST', '/session', {'capabilities': {'alwaysMatch': {
            'browserName': 'wry', 'tauri:options': {'application': str(executable)},
        }}})
        session = value['sessionId']
        call('POST', f'/session/{session}/timeouts', {'script': 120000})
        wait(lambda: call('POST', f'/session/{session}/execute/sync', {
            'script': 'return !!window.__TAURI_INTERNALS__', 'args': []}), 'Tauri bridge')

    def wait(predicate, description, timeout=90):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (httpx.HTTPError, RuntimeError, OSError, ValueError) as error:
                last = str(error)
            time.sleep(.2)
        raise AssertionError(f'Timed out waiting for {description}: {last}')

    def api(path, method='GET', body=None):
        return invoke('desktop_request', {'method': method, 'path': '/api/v1' + path,
                      'body': body, 'key': str(uuid4()) if method != 'GET' else None})

    def login():
        wait(lambda: call('POST', f'/session/{session}/execute/sync', {
            'script': "return !!document.querySelector('input[autocomplete=current-password]')", 'args': []}), 'login form')
        call('POST', f'/session/{session}/execute/sync', {'script': """
          const form = document.querySelector('input[autocomplete=current-password]').form;
          form.elements.namedItem('admin').value = '1234567';
          form.elements.namedItem('password').value = 'isolated-password-123';
          form.requestSubmit();
          return true;
        """, 'args': []})
        wait(lambda: call('POST', f'/session/{session}/execute/sync', {
            'script': "return !!document.querySelector('input[name=alias]')", 'args': []}), 'signed-in workspace')
        assert invoke('desktop_status')['signed_in']

    def wait_for_hidden_workspace():
        wait(lambda: call('POST', f'/session/{session}/execute/sync', {
            'script': "return !document.querySelector('input[name=alias]')", 'args': []}), 'cleared signed-in workspace', timeout=45)

    def assert_safe_fixture():
        store = ControlStore(root / 'control')
        if store.status()['publication_allowed'] or store.status()['active_sends']:
            raise AssertionError('Fixture must be paused with no active sends')
        if SecretStore(root / 'control').read('BOT_TOKEN'):
            raise AssertionError('Fixture must not contain a Telegram token')
        return store

    try:
        wait(lambda: call('GET', '/status'), 'driver', timeout=30)
        new_session()
        wait(lambda: invoke('desktop_status')['phase'] == 'setup', 'setup')
        setup = invoke('desktop_setup', {'input': {'admin_id': 1234567,
            'password': 'isolated-password-123', 'password_repeat': 'isolated-password-123',
            'mode': 'new', 'checkout': '', 'database': '', 'storage': '', 'confirmed_stopped': False,
            'backup': '', 'recovery_password': ''}})
        assert setup['status'] == 200, setup
        wait(lambda: invoke('desktop_status')['phase'] == 'ready', 'initial host')
        desired_rect = None
        if window_test:
            def visible_rect():
                rect = call('GET', f'/session/{session}/window/rect')
                user32 = ctypes.WinDLL('user32')
                left, top, width, height = (user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))
                return rect if (left <= rect['x'] < left + width and top <= rect['y'] < top + height) else None
            visible_rect_value = wait(visible_rect, 'off-screen window recovery')
            def initial_geometry():
                state = json.loads((root / 'window-state.json').read_text(encoding='utf-8'))
                return state if abs(state['x']) < 10000 and abs(state['y']) < 10000 else None
            original_geometry = wait(initial_geometry, 'fallback window geometry')
            print(f'WINDOW_MOVE_READY initial={visible_rect_value}', flush=True)
            def saved_geometry():
                state = json.loads((root / 'window-state.json').read_text(encoding='utf-8'))
                return state if (abs(state['x'] - original_geometry['x']) > 30
                                 and abs(state['y'] - original_geometry['y']) > 30) else None
            desired_rect = wait(saved_geometry, 'debounced window geometry', timeout=90)
        store = ControlStore(root / 'control')
        if not any(block['reason'] == 'user_pause' for block in store.status()['blocks']):
            store.block('user_pause', 'crash-test')
        assert_safe_fixture()
        login()
        channel = api('/channels', 'POST', {'alias': 'crash-fixture', 'title': 'Survives restart',
                                          'telegram_chat_id': '-1001234567'})
        assert channel['status'] == 201, channel
        initial = attach_existing(root)
        runtime_pid = store.load('runtime-status', 'runtime')['pid']
        _, processes = process_tree_private_bytes(driver.pid)
        shells = [item['pid'] for item in processes if item['name'].lower() == executable.name.lower()]
        if len(shells) != 1:
            raise AssertionError(f'Expected one disposable shell, got {len(shells)}')
        terminate_verified(shells[0], executable)
        wait(lambda: attach_existing(root)['pid'] == initial['pid'], 'host surviving the shell crash')
        assert store.load('runtime-status', 'runtime')['pid'] == runtime_pid
        try:
            call('DELETE', f'/session/{session}')
        except (httpx.HTTPError, RuntimeError):
            pass
        session = None
        new_session()
        wait(lambda: invoke('desktop_status')['phase'] == 'ready', 'reattached shell')
        if desired_rect:
            def restored_rect():
                actual = call('GET', f'/session/{session}/window/rect')
                # WebDriver reports the WebView client origin; the stored
                # position includes the native title bar and window borders.
                return actual if (abs(actual['x'] - desired_rect['x']) < 40
                                  and abs(actual['y'] - desired_rect['y']) < 80
                                  and abs(actual['width'] - desired_rect['width']) < 40
                                  and abs(actual['height'] - desired_rect['height']) < 40) else None
            wait(restored_rect, 'restored window geometry')
        assert attach_existing(root)['pid'] == initial['pid']
        assert store.load('runtime-status', 'runtime')['pid'] == runtime_pid
        login()
        assert_safe_fixture()
        terminate_verified(initial['pid'], backend)
        wait_for_hidden_workspace()

        def recovered():
            current = attach_existing(root)
            status = invoke('desktop_status')
            return current if current['pid'] != initial['pid'] and status['phase'] == 'ready' and not status['signed_in'] else None

        first_recovery = wait(recovered, 'host recovery and session reset', timeout=120)
        login()
        listing = api('/channels')
        assert listing['status'] == 200
        assert any(item['title'] == 'Survives restart' for item in listing['body']['items'])
        assert_safe_fixture()
        # The first replacement is now owned by this shell rather than
        # attached; exercise the pipe-supervised crash path as well.
        initial = first_recovery
        terminate_verified(initial['pid'], backend)
        wait_for_hidden_workspace()
        wait(recovered, 'owned host recovery and session reset', timeout=120)
        login()
        listing = api('/channels')
        assert listing['status'] == 200
        assert any(item['title'] == 'Survives restart' for item in listing['body']['items'])
        assert_safe_fixture()
        invoke('desktop_shutdown')
        def stopped():
            try:
                with InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
                    return True
            except RuntimeError:
                return False
        wait(stopped, 'managed shutdown', timeout=90)
        print('PASS: shell crash reattaches; attached and owned host crashes drain, reconnect, reset session and preserve data', flush=True)
    finally:
        if (root / 'control/node.json').exists():
            try:
                store = ControlStore(root / 'control')
                if store.status()['active_sends'] or SecretStore(root / 'control').read('BOT_TOKEN'):
                    raise RuntimeError('Unsafe fixture state; leaving processes untouched')
                if not any(block['reason'] == 'user_pause' for block in store.status()['blocks']):
                    store.block('user_pause', 'crash-test')
                # The test owns these shells; closing them first prevents a
                # recovery attempt racing the authenticated fixture drain.
                _, processes = process_tree_private_bytes(driver.pid)
                for item in processes:
                    if item['name'].lower() == executable.name.lower():
                        terminate_verified(item['pid'], executable)
                stop_managed(root, lambda _: None)
            except Exception:
                pass  # Preserve evidence for an unconfirmed stop; never kill a sender here.
        client.close()
        driver.terminate()
        driver.wait(timeout=15)


if __name__ == '__main__':
    main()
