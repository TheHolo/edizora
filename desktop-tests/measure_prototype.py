"""Exploratory host-ready and private-bytes samples for a disposable fixture.

These are not the performance acceptance run: they do not measure first visible
window, cold boots, the complete UI, or the required sample counts. An optional
idle window records per-second process-tree memory and CPU samples.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.desktop.headless import attach_existing
from app.desktop.launcher import stop_host
from app.config import Settings
from app.host.control import ControlStore
from app.host.secrets import ALLOWED_SECRETS, SecretStore
from app.runtime import InstanceLock


class ProcessEntry(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                ('parent', wintypes.DWORD), ('priority', ctypes.c_long), ('flags', wintypes.DWORD),
                ('exe', wintypes.WCHAR * 260)]


class MemoryCounters(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('faults', wintypes.DWORD),
                ('peak_working_set', ctypes.c_size_t), ('working_set', ctypes.c_size_t),
                ('quota_peak_paged', ctypes.c_size_t), ('quota_paged', ctypes.c_size_t),
                ('quota_peak_nonpaged', ctypes.c_size_t), ('quota_nonpaged', ctypes.c_size_t),
                ('pagefile', ctypes.c_size_t), ('peak_pagefile', ctypes.c_size_t),
                ('private_bytes', ctypes.c_size_t)]


class FileTime(ctypes.Structure):
    _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]

    def ticks(self) -> int:
        return (self.high << 32) | self.low


def process_tree_private_bytes(root_pid: int) -> tuple[int, list[dict]]:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileTime), ctypes.POINTER(FileTime),
                                       ctypes.POINTER(FileTime), ctypes.POINTER(FileTime)]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError('Could not enumerate process tree')
    parents, names = {}, {}
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        result = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while result:
            parents[int(entry.pid)] = int(entry.parent)
            names[int(entry.pid)] = entry.exe
            result = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    descendants = {root_pid}
    while True:
        new = {pid for pid, parent in parents.items() if parent in descendants} - descendants
        if not new:
            break
        descendants.update(new)
    total = 0
    processes = []
    for pid in descendants:
        handle = kernel.OpenProcess(0x1000 | 0x0010, False, pid)
        if not handle:
            raise OSError(f'Could not inspect process {pid}')
        try:
            counters = MemoryCounters()
            counters.size = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.size):
                raise OSError(f'Could not read memory for process {pid}')
            created, exited, kernel_time, user_time = FileTime(), FileTime(), FileTime(), FileTime()
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise OSError(f'Could not read CPU time for process {pid}')
            total += counters.private_bytes
            processes.append({'name': names.get(pid, 'unknown'), 'private_bytes': counters.private_bytes,
                              'pid': pid, 'created_ticks': created.ticks(),
                              'cpu_ticks': kernel_time.ticks() + user_time.ticks()})
        finally:
            kernel.CloseHandle(handle)
    return total, sorted(processes, key=lambda value: value['private_bytes'], reverse=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--application', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=3, choices=range(1, 21))
    parser.add_argument('--settle-seconds', type=int, default=5, choices=range(1, 121),
                        help='Delay after host readiness before each exploratory memory sample')
    parser.add_argument('--idle-window-seconds', type=int, default=0, choices=range(0, 121),
                        help='Optionally sample process-tree CPU and memory each second after settling')
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
    results = []
    for index in range(args.samples):
        blocked = ALLOWED_SECRETS | {name.upper() for name in Settings.model_fields}
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in blocked and not key.upper().startswith('PUBLISHER_')}
        env['EDIZORA_PROTOTYPE_ROOT'] = str(root)
        started = time.perf_counter()
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen([str(executable)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup)
        ready = False
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Desktop shell exited before host readiness')
                try:
                    attach_existing(root)
                    ready = True
                    break
                except Exception:
                    time.sleep(.1)
            if not ready:
                raise TimeoutError('Host readiness was not confirmed')
            ready_ms = (time.perf_counter() - started) * 1000
            time.sleep(args.settle_seconds)
            private_bytes, processes = process_tree_private_bytes(process.pid)
            webviews = sum(item['name'].lower() == 'msedgewebview2.exe' for item in processes)
            result = {'sample': index + 1, 'host_ready_ms': round(ready_ms, 1),
                      f'private_bytes_after_{args.settle_seconds}s': private_bytes,
                      'webview_process_count': webviews, 'processes': processes}
            if args.idle_window_seconds:
                previous = {(item['pid'], item['created_ticks']): item['cpu_ticks'] for item in processes}
                cpu_ticks = 0
                by_process = {}
                peak_private = private_bytes
                window_start = time.monotonic()
                deadline = window_start + args.idle_window_seconds
                count = 0
                timeline = []
                while time.monotonic() < deadline:
                    time.sleep(max(0, min(1, deadline - time.monotonic())))
                    current_private, current_processes = process_tree_private_bytes(process.pid)
                    current = {(item['pid'], item['created_ticks']): item['cpu_ticks'] for item in current_processes}
                    interval_ticks = 0
                    for item in current_processes:
                        key = (item['pid'], item['created_ticks'])
                        delta = max(0, item['cpu_ticks'] - previous.get(key, 0))
                        cpu_ticks += delta
                        interval_ticks += delta
                        entry = by_process.setdefault(key, {'name': item['name'], 'pid': item['pid'], 'cpu_ticks': 0})
                        entry['cpu_ticks'] += delta
                    previous = current
                    peak_private = max(peak_private, current_private)
                    count += 1
                    timeline.append({'elapsed_seconds': round(time.monotonic() - window_start, 4),
                                     'private_bytes': current_private, 'cpu_ticks_delta': interval_ticks,
                                     'processes': current_processes})
                elapsed = time.monotonic() - window_start
                result['idle_window'] = {'seconds': round(elapsed, 2), 'samples': count,
                    'cpu_percent_of_one_core': round(cpu_ticks / 10_000_000 / elapsed * 100, 3),
                    'cpu_percent_of_machine': round(cpu_ticks / 10_000_000 / elapsed * 100 / (os.cpu_count() or 1), 3),
                    'sampled_peak_private_bytes': peak_private,
                    'final_private_bytes': current_private,
                    'timeline': timeline,
                    'process_cpu': sorted(by_process.values(), key=lambda item: item['cpu_ticks'], reverse=True)}
            results.append(result)
            print(f"sample {index + 1}: host={ready_ms:.1f} ms, private={private_bytes / 1048576:.1f} MiB, processes={len(processes)}", flush=True)
            if webviews < 3:
                print('WebView processes may still be starting; this is not a settled idle-memory sample.', flush=True)
            if args.idle_window_seconds:
                print(f"idle {elapsed:.1f}s: CPU={result['idle_window']['cpu_percent_of_one_core']:.2f}% of one core, "
                      f"private final={current_private / 1048576:.1f} MiB, sampled peak={peak_private / 1048576:.1f} MiB", flush=True)
        finally:
            if ready:
                stop_host(root)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        with InstanceLock(root / 'control/host.lock'):
                            break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Fixture host did not finish its normal drain') from None
                        time.sleep(.2)
                if process.poll() is None:
                    process.terminate()  # Only the empty shell, after host lock release.
                process.wait(timeout=15)
    output = args.output.resolve()
    if output.exists():
        raise SystemExit('Output path already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({'kind': 'exploratory-not-acceptance', 'application': str(executable),
        'platform': platform.platform(), 'logical_cpus': os.cpu_count(), 'samples': results,
        'limitations': ['host readiness only; UI is on the pre-login prototype screen',
                        'a memory sample can precede startup of some WebView child processes',
                        'one-second interval samples can miss brief process or memory peaks',
                        'no cold boots', 'too few samples for p95']}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
