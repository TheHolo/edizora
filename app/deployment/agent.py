"""Restricted receiving-side operations, invoked only by a trusted SSH account.

No listener and no public first-owner setup. Installation creates the identity;
SSH authentication and pinned host keys protect the controlling channel.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

from app.deployment.controller import LocalNodeTarget
from app.deployment.snapshot import VerificationError, safe_path, verify_snapshot
from app.host.control import ControlStore


def receive(target: LocalNodeTarget, migration_id: str, incoming) -> dict:
    root = target.staging(migration_id)
    root.mkdir(parents=True, exist_ok=True)
    limit = int(os.environ.get("PUBLISHER_MAX_TRANSFER_BYTES", str(100 * 1024**3)))
    # Never interpret user filenames as filesystem paths; check both archive and
    # expanded byte totals before extracting. Existing verified files can resume.
    with tempfile.TemporaryFile() as package:
        copied = 0
        while chunk := incoming.read(1024 * 1024):
            copied += len(chunk)
            if copied > limit:
                raise VerificationError("Пакет превышает лимит передачи")
            package.write(chunk)
        package.seek(0)
        with zipfile.ZipFile(package) as bundle:
            entries = bundle.infolist()
            if len(entries) > 1_000_000 or sum(x.file_size for x in entries) > limit:
                raise VerificationError("Распакованный пакет превышает лимит")
            seen = set()
            for entry in entries:
                mode = entry.external_attr >> 16
                if entry.is_dir() or stat.S_ISLNK(mode) or entry.filename.casefold() in seen:
                    raise VerificationError("Недопустимый элемент архива")
                seen.add(entry.filename.casefold())
                path = safe_path(root, entry.filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + ".part")
                with bundle.open(entry) as source, temporary.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
    return verify_snapshot(root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Edizora authenticated node operations")
    parser.add_argument("action", choices=["info", "receive", "verify", "activate", "query", "finalize", "abort", "blocks",
                                           "bridge-register", "bridge-poll", "bridge-reply", "export", "route-probe"])
    parser.add_argument("migration_id", nargs="?")
    options = parser.parse_args()
    if options.migration_id:
        options.migration_id = str(uuid.UUID(options.migration_id))
    control_dir = os.environ.get("PUBLISHER_CONTROL_DIR")
    if not control_dir:
        raise ValueError("PUBLISHER_CONTROL_DIR is required")
    target = LocalNodeTarget(ControlStore(control_dir))
    action, identifier = options.action, options.migration_id
    if action == "info":
        result = target.info()
        version = {}
        os_release = Path("/etc/os-release")
        if os_release.exists():
            for line in os_release.read_text().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    version[key] = value.strip('"')
        result["installation_profile"] = ("ubuntu-24.04-x86_64" if version.get("ID") == "ubuntu" and version.get("VERSION_ID") == "24.04" and platform.machine() == "x86_64" else "unsupported")
    elif action.startswith("bridge-"):
        from app.deployment.pull import register_pull_node, poll_pull_commands, reply_pull_command
        if action == "bridge-register":
            result = register_pull_node(target.store, json.load(sys.stdin))
        elif action == "bridge-poll":
            result = poll_pull_commands(target.store, identifier)
        else:
            result = reply_pull_command(target.store, identifier, json.load(sys.stdin))
    elif action == "export":
        snapshot = target.store.root / "snapshots" / identifier
        record = target.store.load("migration", identifier)
        if not record or record["plan"]["source_node_id"] != target.store.identity()["node_id"]:
            raise ValueError("Unknown source snapshot")
        manifest = verify_snapshot(snapshot)
        with zipfile.ZipFile(sys.stdout.buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as bundle:
            for entry in manifest["files"]:
                bundle.write(snapshot / entry["key"], entry["key"])
            bundle.write(snapshot / "manifest.json", "manifest.json")
        return 0
    elif action == "route-probe":
        value = json.load(sys.stdin)
        result = target.route_probe(identifier, value["challenge"])
    elif action == "receive":
        result = receive(target, identifier, sys.stdin.buffer)
    elif action == "verify":
        result = target.verify(identifier)
    elif action == "activate":
        grant = json.load(sys.stdin)
        if grant.get("migration_id") != identifier:
            raise ValueError("Migration mismatch")
        result = target.activate(grant)
    elif action == "query":
        result = target.query(identifier)
    elif action == "finalize":
        record = json.load(sys.stdin)
        if record.get("id") != identifier:
            raise ValueError("Migration mismatch")
        result = target.finalize(record)
    elif action == "blocks":
        value = json.load(sys.stdin)
        target.apply_blocks(value["blocks"], value.get("migration_id"))
        result = {"ok": True}
    else:
        target.abort(identifier)
        result = {"ok": True}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Remote stderr is never a transport for arbitrary exception contents.
        print(json.dumps({"error": {"code": "node_operation_failed", "message": "Узел отклонил операцию; проверьте локальный журнал"}}))
        raise SystemExit(1)
