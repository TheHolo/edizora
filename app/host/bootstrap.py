"""Trusted local first activation of an empty installer-created standby node."""
from __future__ import annotations

from pathlib import Path

from app.host.control import ControlStore, GateClosed, durable_json
from app.runtime import InstanceLock


def _assert_host_lock(store: ControlStore, lock: InstanceLock) -> None:
    if (lock.path.resolve() != store.root / "host.lock" or
            lock._file is None or lock._file.closed):
        raise GateClosed("Первичная активация требует удерживаемой блокировки host этой установки")


def _assert_unused_control(store: ControlStore, connection) -> None:
    if store.retired_path.exists():
        raise GateClosed("Узел уже передавал управление; первичная активация запрещена")
    for table in ("permits", "blocks", "control_events"):
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            raise GateClosed("Установка уже использовалась; требуется проверенный перенос/восстановление")
    if connection.execute("SELECT 1 FROM documents WHERE kind != 'installed-release' LIMIT 1").fetchone():
        raise GateClosed("В установке есть управляющий журнал; первичная активация запрещена")


def prepare_initial_activation(store: ControlStore, data: Path, database: Path, *, host_lock: InstanceLock) -> dict:
    """Validate before schema creation; never permits an existing content copy."""
    _assert_host_lock(store, host_lock)
    with store.transaction() as connection:
        node = store.identity()
        if node.get("role") != "standby" or node.get("generation") != 1:
            raise GateClosed("Первичная активация доступна только новой пассивной установке")
        if Path(node["data_root"]).resolve() != data.resolve() or not database.resolve().is_relative_to(data.resolve()):
            raise GateClosed("Пути первичной установки не совпадают с идентичностью узла")
        if database.exists() or (data.exists() and any(data.iterdir())):
            raise GateClosed("Первичная активация требует пустую папку данных; существующая БД не получает права запуска")
        _assert_unused_control(store, connection)
        return node


def commit_initial_activation(store: ControlStore, intent: dict, *, host_lock: InstanceLock) -> None:
    """Commit only after successful local schema/admin/password setup."""
    _assert_host_lock(store, host_lock)
    with store.transaction() as connection:
        if store.identity() != intent:
            raise GateClosed("Идентичность изменилась во время первичной настройки")
        _assert_unused_control(store, connection)
        durable_json(store.node_path, {**intent, "role": "active", "initial_activation": True})
