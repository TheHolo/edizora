"""Measure time to the rendered login form in an installed prototype on Windows.

The WebDriver session handshake is included, so the resulting elapsed times
are conservative end-to-end measurements, not exact shell-process timings.
Use only with the generated, paused performance fixture.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import time
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.desktop.launcher import stop_host
from app.config import Settings
from app.host.control import ControlStore
from app.host.secrets import ALLOWED_SECRETS, SecretStore
from app.runtime import InstanceLock
from measure_prototype import FileTime, process_tree_private_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--tauri-driver', type=Path, required=True)
    parser.add_argument('--edge-driver', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=3, choices=range(1, 21))
    parser.add_argument('--resume', action='store_true', help='Continue a partial report for the same fixture and executable')
    parser.add_argument('--managed-shutdown', action='store_true',
                        help='Use the shell shutdown command for builds that close to the tray')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('Windows is required')
    root = args.fixture.resolve(strict=True)
    if (root / '.edizora-prototype').read_text(encoding='utf-8') != 'disposable performance fixture':
        raise SystemExit('A generated disposable performance fixture is required')
    store = ControlStore(root / 'control')
    if store.status()['publication_allowed'] or SecretStore(root / 'control').read('BOT_TOKEN'):
        raise SystemExit('Fixture must remain paused and have no Telegram token')
    with InstanceLock(root / 'control/host.lock'):
        pass
    executable = args.application.resolve(strict=True)
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise SystemExit('Output path already exists')
    if args.resume:
        if not output.is_file():
            raise SystemExit('The partial report does not exist')
        previous = json.loads(output.read_text(encoding='utf-8'))
        if (previous.get('kind') != 'exploratory-not-acceptance'
                or previous.get('application') != str(executable)
                or previous.get('application_sha256') != executable_hash
                or previous.get('fixture') != str(root)):
            raise SystemExit('The partial report belongs to a different application or fixture')
        results = previous.get('samples')
        if (not isinstance(results, list) or len(results) > args.samples
                or any(not isinstance(value, dict) or value.get('sample') != index + 1
                       or not isinstance(value.get('visible_login_ms'), (int, float))
                       for index, value in enumerate(results))):
            raise SystemExit('The partial report has invalid samples')
    else:
        results = []
    output.parent.mkdir(parents=True, exist_ok=True)

    def save_report():
        ordered = sorted(result['visible_login_ms'] for result in results)
        process_times = sorted(result['visible_login_from_process_ms'] for result in results
                               if result.get('visible_login_from_process_ms') is not None)
        report = {'kind': 'exploratory-not-acceptance', 'application': str(executable),
                  'application_sha256': executable_hash, 'fixture': str(root),
                  'target_samples': args.samples, 'complete': len(results) == args.samples,
                  'samples': results, 'median_ms': statistics.median(ordered) if ordered else None,
                  'p95_nearest_rank_ms': ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)] if len(ordered) >= 20 else None,
                  'process_median_ms': statistics.median(process_times) if len(process_times) == len(results) and process_times else None,
                  'process_p95_nearest_rank_ms': process_times[max(0, (95 * len(process_times) + 99) // 100 - 1)] if len(process_times) == len(results) and len(process_times) >= 20 else None,
                  'limitations': ['driver elapsed includes WebDriver handshake overhead', 'process timing requires a visible descendant',
                                  'no cold boots', 'not necessarily the reference machine']}
        temporary = output.with_name(output.name + '.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(report, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

    save_report()
    if len(results) == args.samples:
        return
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    blocked = ALLOWED_SECRETS | {name.upper() for name in Settings.model_fields}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in blocked and not key.upper().startswith('PUBLISHER_')}
    environment.update(EDIZORA_PROTOTYPE_ROOT=str(root), MSEDGEDRIVER_TELEMETRY_OPTOUT='1')
    driver = subprocess.Popen([str(args.tauri_driver.resolve(strict=True)), '--port', str(port),
                               '--native-driver', str(args.edge_driver.resolve(strict=True))],
                              env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=120, trust_env=False)

    def call(method, path, body=None):
        response = client.request(method, path, json=body)
        data = response.json().get('value')
        if response.status_code >= 400:
            raise RuntimeError(f'WebDriver {path}: {data}')
        return data

    def drain_fixture():
        try:
            with InstanceLock(root / 'control/host.lock'):
                return
        except RuntimeError:
            pass
        stop_host(root)
        deadline = time.monotonic() + 60
        while True:
            try:
                with InstanceLock(root / 'control/host.lock'):
                    return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Fixture host did not finish its normal drain') from None
                time.sleep(.2)

    try:
        deadline = time.monotonic() + 30
        while True:
            if driver.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('WebDriver did not become ready')
            try:
                call('GET', '/status')
                break
            except (httpx.HTTPError, ValueError):
                time.sleep(.1)
        for index in range(len(results), args.samples):
            session = None
            try:
                started = time.perf_counter()
                filetime = FileTime()
                ctypes.windll.kernel32.GetSystemTimeAsFileTime(ctypes.byref(filetime))
                started_ticks = filetime.ticks()
                response = call('POST', '/session', {'capabilities': {'alwaysMatch': {
                    'browserName': 'wry', 'tauri:options': {'application': str(executable)},
                }}})
                session = response['sessionId']
                handshake_ms = (time.perf_counter() - started) * 1000
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    ready = call('POST', f'/session/{session}/execute/sync', {'script': """
                      const field = document.querySelector('input[autocomplete=current-password]');
                      return !!field && field.getClientRects().length > 0 && document.visibilityState === 'visible';
                    """, 'args': []})
                    if ready:
                        break
                    time.sleep(.1)
                else:
                    raise TimeoutError('The login form did not become visible')
                ready_ms = (time.perf_counter() - started) * 1000
                process_offset_ms = None
                try:
                    _, processes = process_tree_private_bytes(driver.pid)
                    shells = [item for item in processes if item['name'].lower() == executable.name.lower()]
                    if len(shells) == 1:
                        offset = (shells[0]['created_ticks'] - started_ticks) / 10_000
                        if 0 <= offset <= ready_ms:
                            process_offset_ms = offset
                except OSError:
                    pass  # A transient WebView child may exit during enumeration.
                from_process = ready_ms - process_offset_ms if process_offset_ms is not None else None
                results.append({'sample': index + 1, 'driver_handshake_ms': round(handshake_ms, 1),
                                'visible_login_ms': round(ready_ms, 1),
                                'process_start_offset_ms': round(process_offset_ms, 1) if process_offset_ms is not None else None,
                                'visible_login_from_process_ms': round(from_process, 1) if from_process is not None else None})
                save_report()
                process_detail = f', from process {from_process:.1f} ms' if from_process is not None else ', process timestamp unavailable'
                print(f'sample {index + 1}: visible login {ready_ms:.1f} ms (driver handshake {handshake_ms:.1f} ms{process_detail})', flush=True)
            finally:
                if session and args.managed_shutdown:
                    call('POST', f'/session/{session}/execute/sync', {'script': """
                      window.__TAURI_INTERNALS__.invoke('desktop_shutdown'); return true;
                    """, 'args': []})
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        try:
                            call('GET', f'/session/{session}/url')
                        except (httpx.HTTPError, RuntimeError):
                            break
                        time.sleep(.2)
                    else:
                        raise RuntimeError('The application did not complete its managed shutdown')
                    drain_fixture()
                    try:
                        call('DELETE', f'/session/{session}')
                    except (httpx.HTTPError, RuntimeError):
                        pass  # WebDriver may already have discarded the closed session.
                else:
                    drain_fixture()
                    if session:
                        call('DELETE', f'/session/{session}')
    finally:
        client.close()
        driver.terminate()
        driver.wait(timeout=15)


if __name__ == '__main__':
    main()
