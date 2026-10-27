from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
import shutil
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.host.control import durable_json


class VerificationError(ValueError):
    code = "snapshot_verification_failed"
    status = status_code = 409

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(root: Path, key: str) -> Path:
    path = PurePosixPath(key)
    if (not key or "\\" in key or path.is_absolute() or PureWindowsPath(key).drive
            or any(part in {"..", ".", ""} for part in key.split("/"))):
        raise VerificationError("Недопустимый относительный путь")
    result = root.joinpath(*path.parts)
    if not result.resolve().is_relative_to(root.resolve()):
        raise VerificationError("Путь выходит за каталог данных")
    cursor = result
    while cursor != root:
        if cursor.is_symlink():
            raise VerificationError("Символические ссылки в пакете запрещены")
        cursor = cursor.parent
    return result


def database_report(path: Path) -> dict:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if integrity != ["ok"] or foreign_keys:
            raise VerificationError("SQLite не прошла проверку целостности или связей")
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        summaries = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            # Row digests preserve IDs, relationships, JSON and UTC strings without
            # exposing any actual user content or secrets in a manifest.
            rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
            encoded = sorted(json.dumps(row, ensure_ascii=False, default=lambda x: x.hex() if isinstance(x, bytes) else str(x), separators=(",", ":")) for row in rows)
            summaries[table] = {"rows": len(rows), "digest": hashlib.sha256("\n".join(encoded).encode()).hexdigest()}
        return {"integrity": "ok", "foreign_keys": "ok", "tables": summaries,
                "sqlite_version": sqlite3.sqlite_version}


def make_snapshot(data_root: Path, database: Path, destination: Path, *, metadata: dict, extras: dict | None = None,
                  progress=None) -> dict:
    """Caller holds a runtime write barrier until this immutable set is pinned."""
    root, database = data_root.resolve(), database.resolve()
    if not database.is_relative_to(root) or not database.is_file():
        raise VerificationError("Рабочая SQLite должна находиться внутри data root")
    if destination.exists():
        raise VerificationError("Каталог снимка уже существует")
    if destination.resolve().is_relative_to(root):
        raise VerificationError("Снимок нельзя создавать внутри рабочих данных")
    destination.mkdir(parents=True)
    key = database.relative_to(root).as_posix()
    target_db = safe_path(destination, key)
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(target_db)) as target:
        source.backup(target)
    report = database_report(target_db)
    files = []
    seen = set()
    candidates = sorted(root.rglob("*"))
    originals = []
    node_local_files = []
    for path in candidates:
        if path.is_symlink():
            raise VerificationError("В рабочих данных найдена символическая ссылка")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "lmstudio-model.json" or (path.parent == root and path.suffix in {".lock", ".pid"}):
            node_local_files.append(relative)
            continue
        if relative in {key, key + "-wal", key + "-shm", key + "-journal", "manifest.json", "secrets-envelope.json"}:
            continue
        if relative.casefold() in seen:
            raise VerificationError("Имена файлов конфликтуют при переносе на Windows")
        seen.add(relative.casefold())
        originals.append((path, relative))
    completed = target_db.stat().st_size
    total = completed + sum(path.stat().st_size for path, _ in originals)
    if progress:
        progress(completed, total)
    for path, relative in originals:
        target = safe_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source_stream, target.open("wb") as target_stream:
            while chunk := source_stream.read(1024 * 1024):
                target_stream.write(chunk)
                completed += len(chunk)
                if progress:
                    progress(completed, total)
            target_stream.flush()
            os.fsync(target_stream.fileno())
    for extra_key, value in (extras or {}).items():
        target = safe_path(destination, extra_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        durable_json(target, value)
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            relative = path.relative_to(destination).as_posix()
            entry = {"key": relative, "size": path.stat().st_size, "sha256": sha256(path), "required": True}
            files.append(entry)
    manifest = {"format": "publisher-snapshot-v1", **metadata, "database_key": key,
                "database_sha256": sha256(target_db), "database_report": report,
                "files": files, "total_bytes": sum(item["size"] for item in files), "excluded_node_local_files": node_local_files}
    durable_json(destination / "manifest.json", manifest)
    verify_snapshot(destination)
    return manifest


def verify_snapshot(directory: Path) -> dict:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise VerificationError("Манифест снимка недоступен") from error
    if manifest.get("format") != "publisher-snapshot-v1":
        raise VerificationError("Неизвестный формат снимка")
    keys = set()
    for entry in manifest["files"]:
        key = entry["key"]
        if key.casefold() in keys or key == "manifest.json":
            raise VerificationError("Повторяющийся путь в манифесте")
        keys.add(key.casefold())
        path = safe_path(directory, key)
        if not path.is_file() or path.stat().st_size != entry["size"] or sha256(path) != entry["sha256"]:
            raise VerificationError("Файл снимка отсутствует или повреждён: " + key)
    actual = {p.relative_to(directory).as_posix().casefold() for p in directory.rglob("*") if p.is_file() and p != directory / "manifest.json"}
    if actual != keys:
        raise VerificationError("Набор файлов отличается от манифеста")
    database = safe_path(directory, manifest["database_key"])
    if sha256(database) != manifest["database_sha256"]:
        raise VerificationError("Хеш SQLite отличается от манифеста")
    report = database_report(database)
    if report["tables"] != manifest["database_report"]["tables"]:
        raise VerificationError("Семантическая сверка SQLite не пройдена")
    return manifest


def copy_verified(source: Path, destination: Path, *, progress=None) -> dict:
    manifest = verify_snapshot(source)
    destination.mkdir(parents=True, exist_ok=True)
    completed = 0
    for entry in manifest["files"]:
        target = safe_path(destination, entry["key"])
        if not (target.is_file() and target.stat().st_size == entry["size"] and sha256(target) == entry["sha256"]):
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            with safe_path(source, entry["key"]).open("rb") as incoming, temporary.open("wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    outgoing.write(chunk)
                    if progress:
                        progress(completed + outgoing.tell(), manifest["total_bytes"])
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if sha256(temporary) != entry["sha256"]:
                raise VerificationError("Контрольная сумма передачи не совпала")
            os.replace(temporary, target)
        completed += entry["size"]
        if progress:
            progress(completed, manifest["total_bytes"])
    durable_json(destination / "manifest.json", manifest)
    return verify_snapshot(destination)
