from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class GateClosed(RuntimeError):
    code = "publication_blocked"
    status = status_code = 503

    def __init__(self, message):
        self.message = message
        super().__init__(message)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def durable_json(path: Path, value: dict[str, Any]) -> None:
    """Replace a small control document only after flushing its complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class ControlStore:
    """Cross-process publication authority and migration journal.

    Opening a content copy never grants authority. Initial activation must be an
    explicit trusted setup command, or a verified handoff. A retired marker is
    intentionally independent of the movable content database.
    """

    def __init__(self, control_dir: str | Path):
        self.root = Path(control_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "control.sqlite"
        self.node_path = self.root / "node.json"
        self.retired_path = self.root / "retired.json"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS blocks (
                    reason TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS permits (
                    id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, job_version INTEGER NOT NULL,
                    channel_id INTEGER, generation INTEGER NOT NULL, node_id TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS permit_job ON permits(job_id);
                CREATE TABLE IF NOT EXISTS documents (
                    kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS control_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
                    stage TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            """)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextlib.contextmanager
    def transaction(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def initialize(self, *, system_id: str, data_root: str | Path,
                   role: str = "standby", node_id: str | None = None,
                   generation: int = 1, profile: str = "local",
                   release_id: str = "development", schema_version: int = 1) -> dict:
        if role not in {"active", "standby", "worker_only", "recovery_required"}:
            raise ValueError("Unsupported initial node role")
        with self.transaction():
            if self.retired_path.exists():
                raise GateClosed("Узел уже передал управление; обычная настройка запрещена")
            if self.node_path.exists():
                raise ValueError("Node identity already exists")
            value = {"node_id": node_id or str(uuid.uuid4()), "system_id": system_id,
                     "role": role, "generation": generation, "data_root": str(Path(data_root).resolve()),
                     "profile": profile, "release_id": release_id,
                     "schema_version": schema_version, "created_at": time.time()}
            durable_json(self.node_path, value)
        return value

    def identity(self) -> dict:
        if not self.node_path.exists():
            return {"node_id": None, "system_id": None, "role": "recovery_required", "generation": 0}
        try:
            value = json.loads(self.node_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise GateClosed("Не удалось проверить идентичность узла") from error
        if self.retired_path.exists():
            value["role"] = "retired"
            value["retirement"] = json.loads(self.retired_path.read_text(encoding="utf-8"))
        return value

    def status(self) -> dict:
        with self.connect() as connection:
            blocks = [dict(row) for row in connection.execute("SELECT * FROM blocks ORDER BY reason")]
            permits = [dict(row) for row in connection.execute("SELECT * FROM permits ORDER BY created")]
        node = self.identity()
        return {**node, "blocks": blocks, "active_sends": len(permits),
                "permits": permits, "publication_allowed": node["role"] == "active" and not blocks}

    def _assert(self, connection: sqlite3.Connection, *, mutation: bool = False) -> dict:
        node = self.identity()
        if node["role"] != "active":
            raise GateClosed("Этот узел не имеет права изменять данные или публиковать")
        blocks = list(connection.execute("SELECT reason FROM blocks"))
        prohibited = [row["reason"] for row in blocks if not mutation or row["reason"] != "user_pause"]
        if prohibited:
            raise GateClosed("Публикации остановлены: " + ", ".join(prohibited))
        return node

    def assert_publication_allowed(self) -> None:
        with self.transaction() as connection:
            self._assert(connection)

    def assert_mutation_allowed(self) -> None:
        with self.transaction() as connection:
            self._assert(connection, mutation=True)

    def block(self, reason: str, owner: str) -> None:
        if not reason or not owner:
            raise ValueError("Block reason and owner are required")
        with self.transaction() as connection:
            existing = connection.execute("SELECT owner FROM blocks WHERE reason=?", (reason,)).fetchone()
            if existing and existing["owner"] != owner:
                raise GateClosed("Остановкой управляет другая операция")
            connection.execute("INSERT OR IGNORE INTO blocks VALUES(?,?,?)", (reason, owner, time.time()))

    def unblock(self, reason: str, owner: str) -> None:
        with self.transaction() as connection:
            existing = connection.execute("SELECT owner FROM blocks WHERE reason=?", (reason,)).fetchone()
            if existing and existing["owner"] != owner:
                raise GateClosed("Нельзя снять остановку другой операции")
            connection.execute("DELETE FROM blocks WHERE reason=? AND owner=?", (reason, owner))

    def acquire_permit(self, job_id: int, job_version: int, channel_id: int | None = None) -> dict:
        with self.transaction() as connection:
            node = self._assert(connection)
            permit = {"id": str(uuid.uuid4()), "job_id": job_id, "job_version": job_version,
                      "channel_id": channel_id, "generation": node["generation"],
                      "node_id": node["node_id"], "created": time.time()}
            if channel_id is not None and connection.execute(
                "SELECT 1 FROM permits WHERE channel_id=?", (channel_id,)
            ).fetchone():
                raise GateClosed("В канале уже идёт отправка")
            connection.execute("INSERT INTO permits VALUES(:id,:job_id,:job_version,:channel_id,:generation,:node_id,:created)", permit)
        return permit

    def validate_permit(self, permit_id: str | dict) -> dict:
        key = permit_id["id"] if isinstance(permit_id, dict) else permit_id
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM permits WHERE id=?", (key,)).fetchone()
            node = self.identity()
            if (not row or node["role"] != "active" or row["generation"] != node["generation"]
                    or row["node_id"] != node["node_id"]):
                raise GateClosed("Разрешение отправки отозвано")
            # Previously admitted multipart sends finish during drain.
            return dict(row)

    def release_permit(self, permit_id: str | dict) -> None:
        key = permit_id["id"] if isinstance(permit_id, dict) else permit_id
        with self.transaction() as connection:
            connection.execute("DELETE FROM permits WHERE id=?", (key,))

    def retire(self, migration_id: str, *, target_node_id: str, generation: int, snapshot_hash: str,
               media_manifest_hash: str | None = None) -> dict:
        with self.transaction() as connection:
            node = self.identity()
            if connection.execute("SELECT 1 FROM permits LIMIT 1").fetchone():
                raise GateClosed("Отправки ещё не завершены")
            if self.retired_path.exists():
                old = json.loads(self.retired_path.read_text(encoding="utf-8"))
                if (old["migration_id"] != migration_id or old["target_node_id"] != target_node_id
                        or old["generation"] != generation or old["snapshot_hash"] != snapshot_hash
                        or media_manifest_hash is not None and old.get("media_manifest_hash") != media_manifest_hash):
                    raise GateClosed("Узел уже передал управление другой операции")
                return old
            if node["role"] != "active" or generation != node["generation"] + 1:
                raise GateClosed("Неверная роль или поколение источника")
            receipt = {"migration_id": migration_id, "source_node_id": node["node_id"],
                       "target_node_id": target_node_id, "system_id": node["system_id"],
                       "generation": generation, "snapshot_hash": snapshot_hash,
                       "retired_at": time.time()}
            if media_manifest_hash is not None:
                receipt["media_manifest_hash"] = media_manifest_hash
            durable_json(self.retired_path, receipt)
            return receipt

    def save(self, kind: str, identifier: str, payload: dict) -> None:
        with self.transaction() as connection:
            connection.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated",
                               (kind, identifier, json.dumps(payload, ensure_ascii=False, sort_keys=True), time.time()))

    def accept_command(self, kind: str, key: str, digest: str, record: dict, *, exclusive: bool = False) -> dict:
        """Accept a durable operation and its idempotency receipt atomically."""
        with self.transaction() as connection:
            previous = connection.execute("SELECT payload FROM documents WHERE kind=? AND id=?", (kind + "-command", key)).fetchone()
            if previous:
                receipt = json.loads(previous["payload"])
                if receipt["digest"] != digest:
                    raise ValueError("Idempotency key reused with another payload")
                row = connection.execute("SELECT payload FROM documents WHERE kind=? AND id=?", (kind, receipt["id"])).fetchone()
                return json.loads(row["payload"])
            if exclusive:
                for row in connection.execute("SELECT payload FROM documents WHERE kind=?", (kind,)):
                    if json.loads(row["payload"])["status"] not in {"succeeded", "cancelled", "failed"}:
                        raise GateClosed("Другая операция ещё не завершена")
            now = time.time()
            for document_kind, identifier, value in ((kind, record["id"], record),
                    (kind + "-command", key, {"id": record["id"], "digest": digest})):
                connection.execute("INSERT INTO documents VALUES(?,?,?,?)", (document_kind, identifier, json.dumps(value, ensure_ascii=False, sort_keys=True), now))
        return record

    def load(self, kind: str, identifier: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM documents WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list(self, kind: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM documents WHERE kind=? ORDER BY updated DESC", (kind,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def event(self, operation_id: str, stage: str, payload: dict) -> int:
        with self.transaction() as connection:
            cursor = connection.execute("INSERT INTO control_events(operation_id,stage,payload,created) VALUES(?,?,?,?)",
                                        (operation_id, stage, json.dumps(payload, ensure_ascii=False), time.time()))
            return int(cursor.lastrowid)

    def events(self, operation_id: str, after_seq: int = 0) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM control_events WHERE operation_id=? AND seq>? ORDER BY seq LIMIT 1000", (operation_id, after_seq)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]


class NodeGate:
    """Async adapter for async application services; all state remains durable."""
    def __init__(self, store: ControlStore):
        self.store = store

    async def assert_publish_allowed(self):
        self.store.assert_publication_allowed()

    async def assert_mutation_allowed(self):
        self.store.assert_mutation_allowed()

    async def acquire(self, job_id: int, job_version: int, channel_id: int | None = None):
        return self.store.acquire_permit(job_id, job_version, channel_id)

    async def before_call(self, permit):
        return self.store.validate_permit(permit)

    async def release(self, permit):
        self.store.release_permit(permit)
