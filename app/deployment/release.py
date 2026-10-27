from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

from app.deployment.snapshot import VerificationError, safe_path, sha256


def build_release(source: Path, output_dir: Path) -> dict:
    """Package the actual reviewed working files with deterministic timestamps.

    Content data, credentials, caches, virtual environments and Git state are
    intentionally absent. The release ID describes these bytes, not Git HEAD.
    """
    source = source.resolve()
    files = []
    for folder in ("app", "deploy", "docs", "migrations", "frontend/dist", "frontend/src", "frontend/public", "frontend/scripts", "tests"):
        directory = source / folder
        if directory.exists():
            files.extend(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
                         and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"})
    for name in ("pyproject.toml", "uv.lock", "README.md", ".env.example", "bot-settings.toml", "frontend/package.json", "frontend/package-lock.json",
                 "frontend/index.html", "frontend/tsconfig.json", "frontend/tsconfig.app.json", "frontend/tsconfig.node.json", "frontend/vite.config.ts"):
        if (source / name).is_file():
            files.append(source / name)
    if not (source / "frontend/dist/index.html").is_file():
        raise ValueError("Сначала соберите проверенный frontend: frontend/dist/index.html отсутствует")
    records = [{"key": p.relative_to(source).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)}
               for p in sorted(set(files))]
    encoded = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    release_id = "publisher-" + hashlib.sha256(encoded).hexdigest()[:16]
    migration_source = source / "app/db/migrations.py"
    schema_match = re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", migration_source.read_text(encoding="utf-8"), re.MULTILINE) if migration_source.exists() else None
    manifest = {"format": "publisher-release-v1", "release_id": release_id,
                "schema_version": int(schema_match.group(1)) if schema_match else 1, "files": records}
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (release_id + ".zip")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        contents = [(record["key"], (source / record["key"]).read_bytes()) for record in records]
        contents.append(("release-manifest.json", json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()))
        for key, content in sorted(contents):
            info = zipfile.ZipInfo(key, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = 0o755 if key.endswith(".sh") else 0o644
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, content)
    return {"release_id": release_id, "path": str(destination.resolve()), "sha256": sha256(destination), "files": len(records)}


def verify_release(archive_path: Path) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "release-manifest.json" not in names:
            raise VerificationError("Недопустимый набор файлов релиза")
        manifest = json.loads(archive.read("release-manifest.json"))
        if manifest.get("format") != "publisher-release-v1":
            raise VerificationError("Неизвестный формат релиза")
        expected = {record["key"] for record in manifest["files"]} | {"release-manifest.json"}
        if set(names) != expected:
            raise VerificationError("Набор файлов релиза отличается от manifest")
        for record in manifest["files"]:
            safe_path(Path.cwd(), record["key"])
            data = archive.read(record["key"])
            if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise VerificationError("Повреждён файл релиза")
        encoded = json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()
        if manifest["release_id"] != "publisher-" + hashlib.sha256(encoded).hexdigest()[:16]:
            raise VerificationError("ID релиза не соответствует содержимому")
    return manifest
