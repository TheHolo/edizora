from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from app.deployment.snapshot import VerificationError, verify_snapshot


def fingerprint(public_key: str) -> str:
    parts = public_key.strip().split()
    if len(parts) < 2:
        raise ValueError("Неверный публичный SSH ключ")
    raw = base64.b64decode(parts[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


class SSHNodeTarget:
    """Fixed remote operations over authenticated OpenSSH with pinned host keys.

    Credentials are paths to a trusted OS SSH agent/key, never request payloads or
    source code. Password continuation after reboot is deliberately unsupported.
    """
    def __init__(self, *, host: str, user: str, port: int, known_hosts: Path,
                 identity_file: Path | None = None, ssh_binary: str = "ssh"):
        if not re.fullmatch(r"[A-Za-z0-9_.:@\[\]-]+", host) or host.startswith("-"):
            raise ValueError("Неверный SSH host")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", user) or user.startswith("-"):
            raise ValueError("Неверный SSH user")
        if not 1 <= port <= 65535 or not known_hosts.is_file():
            raise ValueError("Нужны проверенный known_hosts и корректный порт")
        executable = shutil.which(ssh_binary)
        if not executable:
            raise ValueError("OpenSSH client не установлен")
        self.arguments = [executable, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                          "-o", "UserKnownHostsFile=" + str(known_hosts.resolve()),
                          "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-p", str(port)]
        if identity_file:
            if not identity_file.is_file():
                raise ValueError("SSH key недоступен")
            self.arguments += ["-i", str(identity_file.resolve()), "-o", "IdentitiesOnly=yes"]
        self.arguments += [user + "@" + host]

    def _command(self, action: str, identifier: str | None = None) -> list[str]:
        if action not in {"info", "receive", "verify", "activate", "query", "finalize", "abort", "blocks",
                          "bridge-register", "bridge-poll", "bridge-reply", "export", "route-probe"}:
            raise ValueError("Unsupported remote operation")
        command = "publisher-node-agent " + action
        if identifier:
            command += " " + str(uuid.UUID(identifier))
        return [*self.arguments, command]

    def _invoke(self, action: str, identifier: str | None = None, value: dict | list | None = None) -> dict:
        result = subprocess.run(self._command(action, identifier), input=json.dumps(value).encode() if value is not None else b"",
                                capture_output=True, timeout=120, check=False)
        if result.returncode:
            # stderr may contain remote configuration or credentials; retain no raw output.
            raise VerificationError("Защищённая операция SSH не выполнена. Проверьте доступ, host key и журнал удалённого host")
        try:
            response = json.loads(result.stdout)
        except ValueError as error:
            raise VerificationError("Удалённый host вернул неверный ответ") from error
        if not isinstance(response, dict) or response.get("error"):
            raise VerificationError("Удалённый host отклонил операцию")
        return response

    def info(self) -> dict:
        info = self._invoke("info")
        if info.get("installation_profile") != "ubuntu-24.04-x86_64":
            raise VerificationError("Поддержан SSH target Ubuntu 24.04 LTS x86-64 с установленным пассивным host")
        return info

    def transfer(self, migration_id: str, snapshot: Path, progress=None) -> dict:
        manifest = verify_snapshot(snapshot)
        with tempfile.TemporaryDirectory(prefix="publisher-transfer-") as directory:
            archive = Path(directory) / "snapshot.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as bundle:
                for entry in manifest["files"]:
                    bundle.write(snapshot / entry["key"], entry["key"])
                bundle.write(snapshot / "manifest.json", "manifest.json")
            total = archive.stat().st_size
            process = subprocess.Popen(self._command("receive", migration_id), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                sent = 0
                with archive.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        process.stdin.write(chunk)
                        process.stdin.flush()
                        sent += len(chunk)
                        if progress:
                            progress(sent, total)
                process.stdin.close()
                response = process.stdout.read(1024 * 1024)
                code = process.wait(timeout=120)
                if code:
                    raise VerificationError("Передача SSH прервана; проверенные файлы сохранены на назначении")
                value = json.loads(response)
                if value.get("error"):
                    raise VerificationError("Назначение отклонило переданный пакет")
                return value
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def verify(self, migration_id: str) -> dict:
        return self._invoke("verify", migration_id)

    def activate(self, grant: dict) -> dict:
        return self._invoke("activate", grant["migration_id"], grant)

    def query(self, migration_id: str) -> dict:
        return self._invoke("query", migration_id)

    def finalize(self, record: dict) -> dict:
        return self._invoke("finalize", record["id"], record)

    def apply_blocks(self, blocks: list[dict], migration_id: str | None = None) -> None:
        self._invoke("blocks", value={"blocks": blocks, "migration_id": migration_id})

    def route_probe(self, nonce: str, challenge: str) -> dict:
        return self._invoke("route-probe", nonce, {"challenge": challenge})

    def abort(self, migration_id: str) -> None:
        self._invoke("abort", migration_id)
