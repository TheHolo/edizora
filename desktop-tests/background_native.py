"""Exercise background startup using only explicitly disposable profiles on Windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.desktop.headless import attach_existing, stop_managed
from app.config import Settings
from app.host.control import ControlStore
from app.host.secrets import ALLOWED_SECRETS, SecretStore
from app.runtime import InstanceLock


def window_visible(pid: int) -> bool | None:
    """Observe the named top-level window of the process started by this test."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    @callback_type
    def visit(handle, _):
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid:
            title = ctypes.create_unicode_buffer(256)
            user.GetWindowTextW(handle, title, len(title))
            if title.value == 'Edizora — prototype':
                found.append(bool(user.IsWindowVisible(handle)))
        return True

    if not user.EnumWindows(visit, 0):
        raise OSError('Could not inspect the disposable shell window')
    if len(found) > 1:
        raise AssertionError('More than one test window exists')
    return found[0] if found else None


def wait(predicate, description, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (httpx.HTTPError, OSError, ValueError, RuntimeError):
            pass
        time.sleep(.2)
    raise AssertionError(f'Timed out waiting for {description}')


def main() -> None:
    if os.name != 'nt':
        raise SystemExit('Windows is required')
    executable = Path(os.environ['PUBLISHER_TEST_APPLICATION']).resolve(strict=True)
    root = Path(os.environ['PUBLISHER_BACKGROUND_FIXTURE']).resolve(strict=True)
    fresh = Path(os.environ['PUBLISHER_BACKGROUND_FRESH'])
    if not fresh.is_absolute() or fresh.exists():
        raise SystemExit('PUBLISHER_BACKGROUND_FRESH must name a new absolute directory')
    if (root / '.edizora-prototype').read_text(encoding='utf-8') != 'disposable performance fixture':
        raise SystemExit('A generated disposable performance fixture is required')
    store = ControlStore(root / 'control')
    if store.status()['publication_allowed'] or store.status()['active_sends'] or SecretStore(root / 'control').read('BOT_TOKEN'):
        raise SystemExit('Fixture must be paused with no bot token or active sends')
    with InstanceLock(root / 'control/host.lock'), InstanceLock(root / 'control/runtime.lock'):
        pass
    blocked = ALLOWED_SECRETS | {name.upper() for name in Settings.model_fields}
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() not in blocked and not key.upper().startswith('PUBLISHER_')}
    environment['EDIZORA_PROTOTYPE_ROOT'] = str(root)
    shell = subprocess.Popen([str(executable), '--background'], env=environment,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        initial = wait(lambda: attach_existing(root), 'background host')
        wait(lambda: window_visible(shell.pid) is False, 'hidden window')
        second = subprocess.run([str(executable), '--background'], env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert second.returncode == 0
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            assert window_visible(shell.pid) is False
            time.sleep(.1)
        assert attach_existing(root)['pid'] == initial['pid']

        # A different profile gets its own shell; its first setup is visible
        # even if it was launched in background mode.
        fresh.mkdir(parents=True)
        fresh = fresh.resolve()
        (fresh / '.edizora-prototype').write_text('isolated test profile', encoding='utf-8')
        fresh_environment = {**environment, 'EDIZORA_PROTOTYPE_ROOT': str(fresh)}
        fresh_shell = subprocess.Popen([str(executable), '--background'], env=fresh_environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            wait(lambda: window_visible(fresh_shell.pid) is True, 'first setup window on a separate profile')
            def setup_is_running():
                try:
                    with InstanceLock(fresh / 'desktop-setup.lock'):
                        return False
                except RuntimeError:
                    return True
            wait(setup_is_running, 'separate setup process')
            assert window_visible(shell.pid) is False
            assert attach_existing(root)['pid'] == initial['pid']
        finally:
            if fresh_shell.poll() is None:
                fresh_shell.terminate()
                fresh_shell.wait(timeout=15)
            stop_managed(fresh, lambda _: None)

        reopened = subprocess.run([str(executable)], env=environment,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        assert reopened.returncode == 0
        wait(lambda: window_visible(shell.pid) is True, 'normal launch reopening the window')
        assert attach_existing(root)['pid'] == initial['pid']
    finally:
        # This handle belongs to the test-only shell. The verified host is
        # subsequently drained, with publication paused and no credentials.
        if shell.poll() is None:
            shell.terminate()
            shell.wait(timeout=15)
        stop_managed(root, lambda _: None)

    print('PASS: background start stays hidden; ordinary launch reopens it; a separate first setup stays visible', flush=True)


if __name__ == '__main__':
    main()
