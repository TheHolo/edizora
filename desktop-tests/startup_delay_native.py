"""Verify the 30-second startup check with a non-publishing fake sidecar."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.host.secrets import ALLOWED_SECRETS
from app.runtime import InstanceLock
from measure_prototype import process_tree_private_bytes
from recovery_native import terminate_verified


def main() -> None:
    if os.name != 'nt':
        raise SystemExit('Windows is required')
    shell_source = Path(os.environ['PUBLISHER_TEST_APPLICATION']).resolve(strict=True)
    fake_source = Path(os.environ['PUBLISHER_TEST_FAKE_BACKEND']).resolve(strict=True)
    if (shell_source.name.lower() != 'edizora-prototype.exe'
            or b'EDIZORA_PROTOTYPE_ROOT' not in shell_source.read_bytes()):
        raise SystemExit('An isolated prototype shell is required')
    if b'EDIZORA_FAKE_SLOW_BACKEND_V1' not in fake_source.read_bytes():
        raise SystemExit('The dedicated non-publishing fake backend is required')
    driver_binary = Path(os.environ['PUBLISHER_TEST_TAURI_DRIVER']).resolve(strict=True)
    edge = Path(os.environ['PUBLISHER_TEST_EDGE_DRIVER']).resolve(strict=True)
    root = Path(os.environ['PUBLISHER_NATIVE_FIXTURE'])
    payload = Path(os.environ['PUBLISHER_TEST_STUB_PAYLOAD'])
    if not root.is_absolute() or root.exists() or not payload.is_absolute() or payload.exists():
        raise SystemExit('The test profile and payload must be new absolute directories')
    root.mkdir(parents=True)
    payload.mkdir(parents=True)
    root, payload = root.resolve(), payload.resolve()
    (root / '.edizora-prototype').write_text('isolated test profile', encoding='utf-8')
    (payload / 'backend').mkdir()
    shell = payload / 'edizora-prototype.exe'
    fake = payload / 'backend/edizora-backend.exe'
    shutil.copy2(shell_source, shell)
    shutil.copy2(fake_source, fake)
    blocked = ALLOWED_SECRETS | {name.upper() for name in Settings.model_fields}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in blocked and not key.upper().startswith('PUBLISHER_')}
    environment.update(EDIZORA_PROTOTYPE_ROOT=str(root), MSEDGEDRIVER_TELEMETRY_OPTOUT='1')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
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

    def script(source, args=None):
        return call('POST', f'/session/{session}/execute/sync', {'script': source, 'args': args or []})

    def invoke(command):
        result = call('POST', f'/session/{session}/execute/async', {
            'script': 'const [name,done]=arguments;window.__TAURI_INTERNALS__.invoke(name).then(value=>done({value}),error=>done({error:String(error)}));',
            'args': [command]})
        if 'error' in result:
            raise RuntimeError(f'{command}: {result["error"]}')
        return result['value']

    def wait(predicate, description, timeout=60):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as error:
                last = str(error)
            time.sleep(.2)
        raise AssertionError(f'Timed out waiting for {description}: {last}')

    def backend_pids():
        _, processes = process_tree_private_bytes(driver.pid)
        return [item['pid'] for item in processes if item['name'].lower() == fake.name.lower()]

    def one_backend():
        values = backend_pids()
        return values[0] if len(values) == 1 else None

    try:
        wait(lambda: call('GET', '/status'), 'driver', timeout=30)
        created = call('POST', '/session', {'capabilities': {'alwaysMatch': {
            'browserName': 'wry', 'tauri:options': {'application': str(shell)}}}})
        session = created['sessionId']
        call('POST', f'/session/{session}/timeouts', {'script': 120000})
        wait(lambda: script('return !!window.__TAURI_INTERNALS__'), 'Tauri bridge')
        wait(lambda: invoke('desktop_status')['phase'] == 'starting', 'slow startup')
        original = wait(one_backend, 'one fake backend')
        wait(lambda: script("return [...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Проверить снова')"),
             '30-second check', timeout=55)
        script("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Проверить снова').click();return true")
        wait(lambda: script("return [...document.querySelectorAll('[role=alert]')].some(x=>x.textContent.includes('ещё запускается'))"),
             'still-starting explanation', timeout=5)
        assert invoke('desktop_status')['phase'] == 'starting'
        assert backend_pids() == [original]
        wait(lambda: invoke('desktop_status')['phase'] == 'failed', 'first fake process exit', timeout=20)
        wait(lambda: script("return [...document.querySelectorAll('button')].some(x=>x.textContent.trim()==='Проверить снова')"),
             'retry after exit')
        script("[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Проверить снова').click();return true")
        wait(lambda: (pid := one_backend()) is not None and pid != original, 'one retry process', timeout=15)
        invoke('desktop_shutdown')
        def exited():
            try:
                call('GET', f'/session/{session}/url')
                return False
            except (httpx.HTTPError, RuntimeError):
                return True
        wait(exited, 'safe shutdown during retry', timeout=30)
        with InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
            pass
        assert not (root / 'control/node.json').exists()
        print('PASS: slow startup check keeps one backend, retries only after exit and shuts down safely', flush=True)
    finally:
        try:
            _, processes = process_tree_private_bytes(driver.pid)
            for item in processes:
                try:
                    if item['name'].lower() == fake.name.lower():
                        terminate_verified(item['pid'], fake)
                    elif item['name'].lower() == shell.name.lower():
                        terminate_verified(item['pid'], shell)
                except OSError:
                    pass  # A test process may have exited between enumeration and opening it.
        except OSError:
            pass
        client.close()
        driver.terminate()
        driver.wait(timeout=15)


if __name__ == '__main__':
    main()
