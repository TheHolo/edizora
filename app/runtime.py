from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any


def managed_checkout_marker() -> Path:
    return Path(__file__).resolve().parent.parent / ".publisher-managed.json"


def mark_managed_checkout(control_dir: Path) -> None:
    from app.host.control import durable_json
    durable_json(managed_checkout_marker(),{"format":"publisher-managed-checkout-v1","control_dir":str(Path(control_dir).resolve())})


def assert_legacy_entrypoint_allowed() -> None:
    default_control=Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local'/'share'))/'Publisher'/'control'/'node.json'
    if os.environ.get("PUBLISHER_CONTROL_DIR") or managed_checkout_marker().is_file() or default_control.is_file():
        raise RuntimeError("Эта установка управляется веб-панелью Publisher. Старый запуск бота заблокирован, чтобы не создать второго отправителя и не оживить перенесённый узел. Откройте python -m app.web или штатный launcher веб-панели.")


async def supervise(*services: Coroutine[Any, Any, None]) -> None:
    """Stop sibling services when polling exits or any service fails."""
    tasks = [asyncio.create_task(service) for service in services]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class InstanceLock:
    """An OS-held lock: stale files do not prevent restarting after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        # Windows locks one existing byte; never truncate an existing lock file.
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            raise RuntimeError(
                "Другой экземпляр бота уже использует эту папку storage. "
                "Остановите его перед повторным запуском."
            ) from error
        self._file = stream
        return self

    def __exit__(self, *exc: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
