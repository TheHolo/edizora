from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import time
import uuid
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import UTC, datetime
from typing import Protocol

from app.host.control import ControlStore, GateClosed, durable_json
from app.host.secrets import SecretStore
from app.deployment.snapshot import VerificationError, copy_verified, make_snapshot, safe_path, sha256, verify_snapshot


STAGES = ("stopping", "quiesced", "preparing_target", "snapshotting", "transferring",
          "verifying", "retiring_source", "activation_committed", "activating_target", "routing", "completed")
PAUSE_WARNING = ("На время переноса публикация всех постов во всех каналах будет остановлена. "
                 "Начатые отправки завершатся; новые отправки, включая ручные, будут заблокированы. "
                 "Очередь и настройки сохранятся. Публикации, запланированные на время переноса, "
                 "могут задержаться. Работа возобновится после проверки новой установки по выбранным правилам.")


def operation_dto(record: dict) -> dict:
    result = dict(record)
    for key in ("created_at", "started_at", "updated_at", "heartbeat_at"):
        value = result.get(key, result.get("updated_at") if key == "heartbeat_at" else None)
        result[key] = datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z") if isinstance(value, (int, float)) else value
    for key, default in (("version", 1), ("message", ""), ("progress", None), ("result", None), ("error", None), ("last_event_seq", 0), ("allowed_actions", []), ("stages", [])):
        result.setdefault(key, default)
    return result


class RuntimeHooks(Protocol):
    async def close_and_drain(self, migration_id: str) -> None: ...
    async def assert_quiesced(self) -> None: ...
    async def stop(self) -> None: ...


class LocalNodeTarget:
    """A registered host endpoint on an accessible local disk, also used for fault tests.

    This is not presented as an SSH migration or an Ubuntu installation test.
    The same receiving-side verification is used by the SSH agent.
    """
    def __init__(self, store: ControlStore):
        self.store = store

    def info(self) -> dict:
        from app.runtime import InstanceLock
        node = self.store.status()
        data = Path(node["data_root"])
        data.parent.mkdir(parents=True, exist_ok=True)
        probe = InstanceLock(self.store.root / "host.lock")
        try:
            probe.__enter__()
            host_running = False
            probe.__exit__()
        except RuntimeError:
            host_running = True
        host_ready = False
        host_record = self.store.root / "host.json"
        if host_running and host_record.is_file():
            try:
                local = urllib.parse.urlsplit(json.loads(host_record.read_text(encoding="utf-8"))["local_url"])
                if local.scheme == "http" and local.hostname in {"127.0.0.1", "::1", "localhost"} and local.port:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(urllib.parse.urlunsplit((local.scheme, local.netloc, "/api/v1/health/live", "", "")), timeout=1) as response:
                        host_ready = response.status == 200
            except Exception:
                pass
        return {**node, "free_bytes": shutil.disk_usage(data.parent).free, "connected": True,
                "host_running": host_running, "host_ready": host_ready,
                "secret_public_key": SecretStore(self.store.root).public_key()}

    def staging(self, migration_id: str) -> Path:
        return self.store.root / "incoming" / str(uuid.UUID(migration_id))

    def transfer(self, migration_id: str, snapshot: Path, progress=None):
        return copy_verified(snapshot, self.staging(migration_id), progress=progress)

    def verify(self, migration_id: str) -> dict:
        return verify_snapshot(self.staging(migration_id))

    def activate(self, grant: dict) -> dict:
        from app.runtime import InstanceLock
        with InstanceLock(self.store.root / "activation.lock"):
            return self._activate_locked(grant)

    def _activate_locked(self, grant: dict) -> dict:
        identifier = grant["migration_id"]
        if self.store.load("revoked", identifier):
            raise GateClosed("Разрешение этой миграции отозвано")
        previous = self.store.load("activation", identifier)
        if previous and previous.get("status") == "accepted":
            if previous["grant"] != grant:
                raise GateClosed("Параметры уже принятого разрешения изменены")
            return previous
        node = self.store.identity()
        recovering_install = (previous and previous.get("status") == "installing" and previous.get("grant") == grant
            and node.get("node_id") == grant["target_node_id"] and node.get("system_id") == grant["system_id"]
            and node.get("generation") == grant["generation"] and node.get("authorized_snapshot") == grant["snapshot_hash"]
            and node.get("migration_id") == identifier)
        if recovering_install:
            manifest = verify_snapshot(Path(node["data_root"]))
            if manifest["database_sha256"] != grant["snapshot_hash"]:
                raise VerificationError("Активируемый набор данных изменился после прерывания")
            if grant.get("media_manifest_hash") and sha256(Path(node["data_root"]) / "manifest.json") != grant["media_manifest_hash"]:
                raise VerificationError("Манифест файлов изменился после разрешения на активацию")
            self._import_secrets(Path(node["data_root"]), grant)
            if self.store.retired_path.exists():
                os.replace(self.store.retired_path, self.store.root / ("retired-before-" + identifier + ".json"))
            receipt = {"status": "accepted", "grant": grant, "target_node_id": node["node_id"],
                       "generation": grant["generation"], "snapshot_hash": grant["snapshot_hash"], "accepted_at": time.time()}
            self.store.save("activation", identifier, receipt)
            return receipt
        if (node["node_id"] != grant["target_node_id"] or node["system_id"] != grant["system_id"]
                or grant["generation"] <= node["generation"]):
            raise GateClosed("Разрешение предназначено другому узлу или поколению")
        if node["role"] not in {"standby", "retired", "recovery_required"}:
            raise GateClosed("Назначение не находится в пассивной роли")
        retirement = grant.get("retired_receipt", {})
        if any(retirement.get(key) != grant.get(key) for key in ("migration_id", "target_node_id", "system_id", "generation", "snapshot_hash", "media_manifest_hash")):
            raise GateClosed("Нет согласованной квитанции остановленного источника")
        self._stop_managed_runtime()
        old_blocks = self.store.status()["blocks"]
        if node["role"] == "retired":
            retired = node.get("retirement", {})
            for block in old_blocks:
                if block["reason"] == "migration" and block["owner"] == retired.get("migration_id"):
                    self.store.unblock("migration", block["owner"])
        self.store.block("migration", identifier)
        stage = self.staging(identifier)
        data_root = Path(node["data_root"])
        # Recovery after root rename uses the already installed manifest.
        installed = data_root.exists() and (data_root / "manifest.json").exists()
        if installed:
            old_manifest = json.loads((data_root / "manifest.json").read_text(encoding="utf-8"))
            installed = old_manifest.get("migration_id") == identifier and old_manifest.get("database_sha256") == grant["snapshot_hash"]
            if installed:
                verify_snapshot(data_root)
                if grant.get("media_manifest_hash") and sha256(data_root / "manifest.json") != grant["media_manifest_hash"]:
                    raise VerificationError("Установленный манифест не соответствует разрешению")
        if not installed:
            manifest = verify_snapshot(stage)
            if manifest["database_sha256"] != grant["snapshot_hash"]:
                raise VerificationError("Разрешение не соответствует проверенному снимку")
            if grant.get("media_manifest_hash") and sha256(stage / "manifest.json") != grant["media_manifest_hash"]:
                raise VerificationError("Разрешение не соответствует проверенному каталогу файлов")
            if manifest["system_id"] != node["system_id"]:
                raise VerificationError("Чужая система в снимке")
            if (manifest["release_id"] != node["release_id"] or manifest["schema_version"] != node["schema_version"]):
                raise VerificationError("Перенос требует тот же релиз и схему")
            receipt = {"status": "installing", "grant": grant, "target_node_id": node["node_id"]}
            self.store.save("activation", identifier, receipt)
            archive = data_root.with_name(data_root.name + ".before-" + identifier)
            if data_root.exists():
                if archive.exists():
                    raise VerificationError("Архив прежнего набора уже существует; нужна сверка")
                os.replace(data_root, archive)
            data_root.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, data_root)
        installed_manifest = json.loads((data_root / "manifest.json").read_text(encoding="utf-8"))
        updated = {**node, "role": "active", "generation": grant["generation"],
                   "authorized_snapshot": grant["snapshot_hash"], "authorized_manifest": grant.get("media_manifest_hash"),
                   "database_key": installed_manifest["database_key"], "migration_id": identifier}
        updated.pop("retirement", None)
        self._import_secrets(data_root, grant)
        # A returning node can supersede its old tombstone only through this
        # authenticated activation, never through initialize/ordinary startup.
        durable_json(self.store.node_path, updated)
        if self.store.retired_path.exists():
            os.replace(self.store.retired_path, self.store.root / ("retired-before-" + identifier + ".json"))
        receipt = {"status": "accepted", "grant": grant, "target_node_id": node["node_id"],
                   "generation": grant["generation"], "snapshot_hash": grant["snapshot_hash"],
                   "accepted_at": time.time()}
        self.store.save("activation", identifier, receipt)
        return receipt

    def _import_secrets(self, data_root: Path, grant: dict) -> None:
        envelope = data_root / "secrets-envelope.json"
        if envelope.exists():
            context = {key: grant[key] for key in ("system_id", "migration_id", "target_node_id", "generation")}
            SecretStore(self.store.root).import_for_target(json.loads(envelope.read_text(encoding="utf-8")), context=context)

    def query(self, migration_id: str) -> dict:
        return {"node": self.store.status(), "receipt": self.store.load("activation", migration_id),
                "finalization": self.store.load("finalization", migration_id)}

    def finalize(self, record: dict) -> dict:
        completed = self.store.load("finalization", record["id"])
        if completed and completed.get("status") == "completed":
            return completed
        receipt = self.store.load("activation", record["id"])
        node = self.store.identity()
        if not receipt or receipt["status"] != "accepted" or node["generation"] != record["generation_after"]:
            raise GateClosed("Нет подтверждения принятия управления")
        report = {**record, "status": "succeeded", "stage_key": "completed", "migration_state": "completed", "allowed_actions": []}
        report["checkpoints"] = {**report.get("checkpoints", {}), "completed": time.time()}
        self.store.save("migration", record["id"], report)
        self.store.save("finalization", record["id"], {"status": "committed", "generation": node["generation"], "committed_at": time.time()})
        self.store.unblock("migration", record["id"])
        command_id = self._runtime_command("resume", record["id"])
        if self.store.load("runtime-status", "runtime") is not None:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                result = self.store.load("runtime-result", command_id)
                if result:
                    if result.get("status") != "succeeded":
                        raise GateClosed("Назначение приняло данные, но runtime не подтвердил безопасный запуск")
                    break
                time.sleep(.1)
            else:
                raise GateClosed("Нет подтверждения запуска назначения; источник остаётся перенесённым")
        result = {"status": "completed", "generation": node["generation"], "node_id": node["node_id"],
                  "migration_id": record["id"], "completed_at": time.time()}
        self.store.save("finalization", record["id"], result)
        return result

    def _runtime_command(self, action: str, migration_id: str | None = None) -> str:
        identifier = str(uuid.uuid4())
        node = self.store.identity()
        self.store.save("runtime-command", identifier, {"id": identifier, "action": action,
            "node_id": node["node_id"], "generation": node["generation"],
            "migration_id": migration_id, "created_at": time.time()})
        return identifier

    def _stop_managed_runtime(self) -> None:
        status = self.store.load("runtime-status", "runtime")
        if not status or not status.get("running"):
            return
        identifier = self._runtime_command("stop")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = self.store.load("runtime-result", identifier)
            status = self.store.load("runtime-status", "runtime")
            if result and result.get("status") == "succeeded" and status and not status.get("running"):
                return
            time.sleep(0.1)
        raise GateClosed("Назначение не подтвердило остановку runtime; замена данных запрещена")

    def apply_blocks(self, blocks: list[dict], migration_id: str | None = None) -> None:
        if migration_id:
            if self.store.load("finalization", migration_id):
                return
            if self.store.identity().get("migration_id") != migration_id:
                raise GateClosed("Переданные паузы относятся другому поколению данных")
        # user_pause is transferable system state; a returning host must not
        # resurrect a stale pause that was explicitly cleared on the active node.
        wanted = {block["reason"]: block for block in blocks if block["reason"] != "migration"}
        for previous in self.store.status()["blocks"]:
            if previous["reason"] == "user_pause" and "user_pause" not in wanted:
                self.store.unblock("user_pause", previous["owner"])
        for block in blocks:
            if block["reason"] != "migration":
                for previous in self.store.status()["blocks"]:
                    if previous["reason"] == block["reason"] and previous["owner"] != block["owner"]:
                        self.store.unblock(previous["reason"], previous["owner"])
                self.store.block(block["reason"], block["owner"])

    def route_probe(self, nonce: str, challenge: str) -> dict:
        nonce = str(uuid.UUID(nonce))
        node = self.store.identity()
        value = {"node_id": node["node_id"], "system_id": node["system_id"],
                 "generation": node["generation"], "challenge": challenge, "expires_at": time.time() + 60}
        self.store.save("route-probe", nonce, value)
        return {key: value[key] for key in ("node_id", "system_id", "generation", "challenge")}

    def abort(self, migration_id: str) -> None:
        if self.store.load("activation", migration_id):
            raise GateClosed("Назначение уже получило разрешение; отмена запрещена")
        self.store.save("revoked", migration_id, {"revoked": True, "at": time.time()})


class DeploymentService:
    def __init__(self, store: ControlStore, data_root: str | Path, db_path: str | Path,
                 runtime_hooks: RuntimeHooks | None = None):
        self.store = store
        self.data_root = Path(data_root).resolve()
        self.db_path = Path(db_path).resolve()
        self.runtime = runtime_hooks
        self.secrets = SecretStore(store.root)
        self._ssh_credentials: dict[str, dict] = {}
        self.targets: dict[str, LocalNodeTarget] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.reload_targets()

    def reload_targets(self) -> None:
        from app.deployment.pull import PullNodeTarget
        from app.deployment.ssh import SSHNodeTarget
        for record in self.store.list("pull-node"):
            self.targets[record["info"]["node_id"]] = PullNodeTarget(self.store, record["info"]["node_id"])
        for record in self.store.list("ssh-transport"):
            try:
                if record.get("mode") == "paramiko":
                    profile = self.store.load("profile", record["profile_id"])
                    if not profile or not Path(record["known_hosts"]).is_file():
                        continue
                    self.targets[record["node_id"]] = self._paramiko_target(profile, Path(record["known_hosts"]))
                    continue
                self.targets[record["node_id"]] = SSHNodeTarget(host=record["host"], user=record["user"],
                    port=record["port"], known_hosts=Path(record["known_hosts"]),
                    identity_file=Path(record["identity_file"]) if record.get("identity_file") else None)
            except (ValueError, OSError):
                # Missing machine credentials never disable the controller UI or
                # silently relax host-key checking after a reboot.
                self.targets.pop(record["node_id"], None)

    def _paramiko_target(self, profile: dict, known_hosts: Path):
        from app.deployment.paramiko_transport import ParamikoNodeTarget
        identifier = profile["id"]
        return ParamikoNodeTarget(profile, known_hosts, lambda: self._ssh_credentials.get(identifier),
                                  self.store.identity(), self.release_archive)

    def release_archive(self) -> Path:
        from app.deployment.release import verify_release, build_release
        configured = os.environ.get("PUBLISHER_RELEASE_ARCHIVE")
        installed = self.store.load("installed-release", "current")
        archive = Path(configured or installed["archive"]) if configured or installed and installed.get("archive") else None
        if archive is None:
            source = Path(__file__).resolve().parents[2]
            if not (source / "release-manifest.json").is_file():
                raise GateClosed("Сначала установите проверенный релиз источника; перенос не совмещается с обновлением программы")
            artifact = build_release(source, self.store.root / "release-artifacts")
            archive = Path(artifact["path"])
        manifest = verify_release(archive)
        node = self.store.identity()
        if manifest["release_id"] != node["release_id"] or manifest.get("schema_version") != node["schema_version"]:
            raise VerificationError("Артефакт источника имеет другой release/schema; сначала выполните отдельное проверенное обновление")
        self.store.save("installed-release", "current", {"archive": str(archive.resolve()), "release_id": manifest["release_id"], "schema_version": manifest["schema_version"], "sha256": sha256(archive)})
        return archive

    def set_profile_credentials(self, identifier: str, value: dict) -> dict:
        profile = self.store.load("profile", identifier)
        if not profile or profile["type"] != "ssh":
            raise ValueError("SSH-профиль не найден")
        allowed = {"password", "private_key", "private_key_passphrase", "sudo_password"}
        if set(value) - allowed - {"use_agent"}:
            raise ValueError("Недопустимые поля SSH-доступа")
        credentials = {}
        for name in allowed:
            entry = value.get(name)
            if entry is not None:
                if not isinstance(entry, str) or len(entry) > (65536 if name == "private_key" else 4096):
                    raise ValueError("Проверьте размер и формат SSH-доступа")
                if name == "sudo_password" and any(character in entry for character in "\r\n\x00"):
                    raise ValueError("Sudo-пароль не должен содержать управляющие переводы строк")
                if entry:
                    credentials[name] = entry
        if not credentials and value.get("use_agent") is not True:
            raise ValueError("Укажите ключ, SSH-пароль либо явно выберите настроенный SSH agent")
        self._ssh_credentials[identifier] = credentials
        return {"profile_id": identifier, "credentials_present": True, "expires_on_controller_restart": True}

    def register_ssh_target(self, profile_id: str, *, known_hosts: Path, identity_file: Path | None = None) -> dict:
        """Trusted local/SSH CLI only; path arguments never come from the public API."""
        from app.deployment.ssh import SSHNodeTarget
        profile = self.store.load("profile", profile_id)
        if not profile or profile["type"] != "ssh":
            raise ValueError("SSH профиль не найден")
        endpoint = SSHNodeTarget(host=profile["host"], user=profile["user"], port=profile["port"],
                                 known_hosts=known_hosts, identity_file=identity_file)
        info = endpoint.info()
        source = self.store.identity()
        if info["system_id"] != source["system_id"] or info["role"] not in {"standby", "retired"}:
            raise GateClosed("Назначение не является пассивным host этой системы")
        record = {"node_id": info["node_id"], "host": profile["host"], "user": profile["user"],
                  "port": profile["port"], "known_hosts": str(known_hosts.resolve()),
                  "identity_file": str(identity_file.resolve()) if identity_file else None}
        self.store.save("ssh-transport", info["node_id"], record)
        self.targets[info["node_id"]] = endpoint
        profile.update(node_id=info["node_id"], status="checked", trusted=True)
        self.store.save("profile", profile_id, profile)
        return profile

    def register_target(self, target: LocalNodeTarget, *, name: str | None = None) -> dict:
        info = target.info()
        self.targets[info["node_id"]] = target
        self.store.save("node-info", info["node_id"], {**info, "last_checked_at": time.time()})
        profile = {"id": info["node_id"], "name": name or info["node_id"], "type": "registered_host",
                   "node_id": info["node_id"], "trusted": True, "status": "checked"}
        self.store.save("profile", profile["id"], profile)
        return profile

    def current(self) -> dict:
        node = self.store.status()
        size = sum(p.stat().st_size for p in self.data_root.rglob("*") if p.is_file()) if self.data_root.exists() else 0
        return {**node, "operating_system": platform.system(), "os_version": platform.release(),
                "architecture": platform.machine(), "hostname": platform.node(),
                "database_path": str(self.db_path), "media_root": str(self.data_root),
                "data_bytes": size, "ai_location": "independent_worker",
                "last_verified_backup": next((x for x in self.list_backups() if x["status"] == "succeeded"), None)}

    def nodes(self) -> list[dict]:
        result = [self.current()]
        result.extend(self.store.list("node-info"))
        return result

    def create_profile(self, value: dict) -> dict:
        # A web request cannot register arbitrary local paths as trusted hosts.
        if value.get("type", "ssh") != "ssh":
            raise ValueError("Локальный host сначала регистрируется доверенной командой установки")
        host = str(value.get("host", ""))
        user = str(value.get("user", ""))
        if not host or not user or any(c in host + user for c in "\r\n\x00 ;|&`$<>\\/") or host.startswith("-") or user.startswith("-"):
            raise ValueError("Неверный SSH адрес или пользователь")
        port = int(value.get("port", 22))
        if not 1 <= port <= 65535:
            raise ValueError("Неверный SSH порт")
        profile = {"id": str(uuid.uuid4()), "pending_node_id": str(uuid.uuid4()), "type": "ssh", "name": str(value.get("name", host))[:100],
                   "host": host, "user": user, "port": port, "status": "awaiting_trust",
                   "https_url": value.get("https_url"), "trusted": False,
                   "message": "Подтвердите fingerprint через доверенный SSH-клиент; пароль панели провайдера не является SSH-паролем"}
        self.store.save("profile", profile["id"], profile)
        return profile

    async def check_profile(self, identifier: str) -> dict:
        profile = self.store.load("profile", identifier)
        if not profile:
            profile = next((p for p in self.store.list("profile") if p.get("node_id") == identifier), None)
        if not profile:
            raise KeyError(identifier)
        self.reload_targets()
        target = self.targets.get(profile.get("node_id"))
        if not target:
            if profile.get("type") == "ssh" and not profile.get("trusted"):
                return await asyncio.to_thread(self._discover_host_keys, profile)
            return {**profile, "status": "waiting_user", "message": "Назначение ещё не зарегистрировано доверенным host. Проверьте SSH host key и установите пассивный host по инструкции."}
        info = await asyncio.to_thread(target.info)
        if profile.get("node_id") and info["node_id"] != profile["node_id"]:
            raise VerificationError("Идентичность узла назначения изменилась после привязки; повторная проверка обязательна")
        self.store.save("node-info", info["node_id"], {**info, "last_checked_at": time.time()})
        node = self.store.identity()
        if info["system_id"] != node["system_id"] or info["release_id"] != node["release_id"] or info["schema_version"] != node["schema_version"]:
            raise VerificationError("Назначение имеет другую систему, релиз или схему")
        if info["role"] not in {"standby", "retired", "unprovisioned"}:
            raise GateClosed("Назначение не в пассивной роли")
        return {**profile, "status": "checked", "target": info}

    def _discover_host_keys(self, profile: dict) -> dict:
        from app.deployment.ssh import fingerprint
        executable = shutil.which("ssh-keyscan")
        if not executable:
            return {**profile, "status": "waiting_user", "message": "Установите OpenSSH client для проверки fingerprint назначения"}
        try:
            result = subprocess.run([executable, "-T", "10", "-p", str(profile["port"]), profile["host"]],
                                    capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {**profile, "status": "waiting_user", "message": "Не удалось проверить SSH-ключ. Проверьте сеть и повторите запрос"}
        lines = []
        for raw in result.stdout.decode("utf-8", errors="replace").splitlines():
            parts = raw.split()
            if len(parts) == 3 and parts[1] in {"ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"}:
                try:
                    lines.append({"algorithm": parts[1], "fingerprint": fingerprint(parts[1] + " " + parts[2]), "line": raw})
                except ValueError:
                    continue
        if not lines:
            return {**profile, "status": "waiting_user", "message": "SSH не ответил на запрос ключей. Проверьте адрес, порт и доступность назначения"}
        self.store.save("ssh-key-scan", profile["id"], {"keys": lines, "expires_at": time.time() + 300})
        return {**profile, "status": "awaiting_trust", "fingerprints": [{key: item[key] for key in ("algorithm", "fingerprint")} for item in lines],
                "message": "Сверьте fingerprint по независимому доверенному каналу и явно подтвердите нужный ключ. Сканирование не подтверждает доверие серверу"}

    async def trust_profile(self, identifier: str, confirmed_fingerprint: str) -> dict:
        self.store.assert_mutation_allowed()
        profile = self.store.load("profile", identifier)
        scanned = self.store.load("ssh-key-scan", identifier)
        if not profile or not scanned or scanned["expires_at"] < time.time():
            raise ValueError("Повторите проверку SSH-ключа перед подтверждением")
        selected = next((item for item in scanned["keys"] if item["fingerprint"] == confirmed_fingerprint), None)
        if not selected:
            raise ValueError("Подтверждённый fingerprint не совпадает с проверенным ключом")
        trust_root = self.store.root / "private" / "ssh-trust"
        trust_root.mkdir(parents=True, exist_ok=True)
        known_hosts = trust_root / (str(uuid.UUID(identifier)) + ".known_hosts")
        if known_hosts.exists() and known_hosts.read_text(encoding="utf-8").strip() != selected["line"]:
            raise VerificationError("SSH ключ изменился. Проверьте замену через доверенный локальный администраторский доступ")
        known_hosts.write_text(selected["line"] + "\n", encoding="utf-8")
        if os.name != "nt":
            known_hosts.chmod(0o600)
        profile.setdefault("pending_node_id", str(uuid.uuid4()))
        endpoint = self._paramiko_target(profile, known_hosts)
        profile.update(trusted=True, status="trusted", node_id=profile["pending_node_id"])
        self.store.save("profile", identifier, profile)
        self.store.save("ssh-transport", profile["node_id"], {"mode": "paramiko", "profile_id": identifier,
            "node_id": profile["node_id"], "known_hosts": str(known_hosts.resolve())})
        self.targets[profile["node_id"]] = endpoint
        info = await asyncio.to_thread(endpoint.info)
        source = self.store.identity()
        if any(info.get(name) != source[name] for name in ("system_id", "release_id", "schema_version")) or info.get("role") not in {"standby", "retired", "unprovisioned"}:
            raise VerificationError("Назначение содержит другую систему/релиз или уже имеет активную роль")
        old_identifier = profile["node_id"]
        profile.update(node_id=info["node_id"], status="checked")
        self.store.save("profile", identifier, profile)
        if old_identifier != info["node_id"]:
            self.targets.pop(old_identifier, None)
        self.store.save("ssh-transport", info["node_id"], {"mode": "paramiko", "profile_id": identifier,
            "node_id": info["node_id"], "known_hosts": str(known_hosts.resolve())})
        self.targets[info["node_id"]] = endpoint
        self.store.save("node-info", info["node_id"], {**info, "last_checked_at": time.time()})
        return {**profile, "target": info}

    async def create_plan(self, value: dict) -> dict:
        target_id = value.get("target_node_id") or value.get("profile_id")
        profile = self.store.load("profile", target_id)
        if not profile:
            profile = next((p for p in self.store.list("profile") if p.get("node_id") == target_id), None)
        checked = await self.check_profile(profile["id"] if profile else target_id)
        if checked["status"] != "checked":
            raise GateClosed(checked["message"])
        node, target = self.store.identity(), checked["target"]
        if node["role"] != "active" or target["node_id"] == node["node_id"]:
            raise GateClosed("Нужны активный источник и другой пассивный узел")
        size = self.current()["data_bytes"]
        reserve = 1024**3 if target.get("provisioning_required") else 64 * 1024 * 1024
        if target["free_bytes"] < size * 2 + reserve:
            raise VerificationError("Недостаточно места с учётом staging и резерва")
        policy = value.get("overdue_policy", "catch_up")
        if policy not in {"catch_up", "hold", "skip"}:
            raise ValueError("Неизвестная политика просрочки")
        readiness = await self.runtime.deployment_readiness() if self.runtime is not None and hasattr(self.runtime, "deployment_readiness") else {"ready": False, "issues": [], "message": "Runtime недоступен для проверки очереди"}
        routing = value.get("routing_plan", {"mode": "local_only"})
        if routing.get("mode") not in {"local_only", "https"}:
            raise ValueError("Выберите локальную панель или HTTPS-маршрут")
        if target.get("profile") == "ubuntu-24.04-x86_64" and routing.get("mode") != "https":
            raise ValueError("Для серверной установки укажите HTTPS-адрес панели")
        if routing.get("mode") == "https":
            address = urllib.parse.urlsplit(str(routing.get("url", "")))
            if address.scheme != "https" or not address.hostname or address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}:
                raise ValueError("Нужен HTTPS origin без пароля, query и fragment")
            hostname = address.hostname.encode("idna").decode("ascii")
            if target.get("provisioning_required") and address.port not in {None, 443}:
                raise ValueError("Автоматическая установка нового VPS требует HTTPS-домен на порту 443")
            authority = ("[" + hostname + "]") if ":" in hostname else hostname
            if address.port is not None:
                authority += ":" + str(address.port)
            routing = {"mode": "https", "url": "https://" + authority, "verified": False}
        plan = {"id": str(uuid.uuid4()), "revision": 1, "created_at": time.time(), "expires_at": time.time() + 900,
                "source_node_id": node["node_id"], "target_node_id": target["node_id"],
                "system_id": node["system_id"], "source_generation": node["generation"],
                "release_id": node["release_id"], "schema_version": node["schema_version"],
                "estimated_bytes": size, "overdue_policy": policy,
                "catch_up_interval_seconds": max(60, int(value.get("catch_up_interval_seconds", 60))),
                "warning": PAUSE_WARNING, "ai_plan": value.get("ai_plan", "keep_home_worker"),
                "routing_plan": routing,
                "initial_blocks": self.store.status()["blocks"], "transport": "registered_host"}
        plan["readiness"] = readiness
        plan["skip_candidate_job_ids"] = readiness.get("scheduled_job_ids", [])
        if policy == "skip" and "confirmed_skip_job_ids" in value:
            selected = value["confirmed_skip_job_ids"]
            if not isinstance(selected, list) or any(not isinstance(item, int) or isinstance(item, bool) for item in selected) or len(selected) != len(set(selected)) or not set(selected).issubset(plan["skip_candidate_job_ids"]):
                raise ValueError("Подтвердите конкретный список заданий для пропуска")
            plan["confirmed_skip_job_ids"] = selected
        self.store.save("plan", plan["id"], plan)
        return plan

    async def start_migration(self, plan_id: str, revision: int, accepted_pause: bool,
                              idempotency_key: str, confirmed_skip_job_ids: list[int] | None = None) -> dict:
        prior = self.store.load("migration-command", idempotency_key)
        digest = hashlib.sha256(json.dumps([plan_id, revision, accepted_pause, confirmed_skip_job_ids]).encode()).hexdigest()
        if prior:
            if prior["digest"] != digest:
                raise ValueError("Idempotency key reused with another payload")
            return self.migration(prior["id"])
        plan = self.store.load("plan", plan_id)
        if not plan or plan["revision"] != revision or plan["expires_at"] < time.time():
            raise ValueError("План устарел; выполните проверку заново")
        if not accepted_pause:
            raise ValueError("Требуется явное принятие общей остановки")
        if plan["overdue_policy"] == "skip":
            selected = confirmed_skip_job_ids if confirmed_skip_job_ids is not None else plan.get("confirmed_skip_job_ids")
            if not isinstance(selected, list) or any(not isinstance(item, int) or isinstance(item, bool) for item in selected) or len(selected) != len(set(selected)) or not set(selected).issubset(plan.get("skip_candidate_job_ids", [])):
                raise ValueError("Подтвердите ID и количество заданий для пропуска; остальные просроченные будут удержаны")
            plan["confirmed_skip_job_ids"] = selected
        if self.runtime is None:
            raise GateClosed("Runtime не подключён: невозможно подтвердить остановку всех записей")
        self.store.assert_mutation_allowed()
        if not hasattr(self.runtime, "deployment_readiness"):
            raise GateClosed("Невозможно проверить готовность подтверждённой очереди")
        readiness = await self.runtime.deployment_readiness()
        if not readiness.get("ready"):
            issues = readiness.get("issues", [])
            ids = ", ".join(str(item["job_id"]) for item in issues[:20])
            raise GateClosed("Сначала подготовьте обязательные файлы и перевод публикаций" + (": " + ids if ids else ""))
        node = self.store.identity()
        if node["generation"] != plan["source_generation"] or node["node_id"] != plan["source_node_id"]:
            raise GateClosed("Роль источника изменилась после проверки плана")
        await self.check_profile(plan["target_node_id"])
        if any(x["status"] not in {"succeeded", "cancelled"} for x in self.store.list("migration")):
            raise GateClosed("Другая миграция ещё не завершена")
        identifier = str(uuid.uuid4())
        record = {"id": identifier, "kind": "migration", "status": "running", "stage_key": "stopping",
                  "migration_state": "stopping", "plan": plan, "generation_after": plan["source_generation"] + 1,
                  "created_at": time.time(), "started_at": time.time(), "updated_at": time.time(), "version": 1,
                  "progress": None, "message": "Останавливаем публикации и изменения", "last_event_seq": 0,
                  "allowed_actions": ["cancel"], "checkpoints": {}, "activation_committed": False}
        # Intent is persisted before the separate runtime/content transaction.
        accepted = self.store.accept_command("migration", idempotency_key, digest, record, exclusive=True)
        if accepted["id"] != identifier:
            return self.migration(accepted["id"])
        self.store.block("migration", identifier)
        try:
            if hasattr(self.runtime, "accept_maintenance"):
                await self.runtime.accept_maintenance(identifier)
            else:
                raise GateClosed("Runtime не подтвердил принятие общей остановки")
        except Exception:
            record.update(status="waiting_user", message="Остановка сохранена, но runtime не подтвердил приём команды. Проверьте состояние приложения перед продолжением",
                          allowed_actions=["resume", "cancel", "resume-source"])
            self.store.save("migration", identifier, record)
            raise GateClosed("Остановка сохранена; нужно восстановить связь с runtime") from None
        self.tasks[identifier] = asyncio.create_task(self._run(identifier))
        return self.migration(identifier)

    def migration(self, identifier: str) -> dict:
        record = self.store.load("migration", identifier)
        if not record:
            raise KeyError(identifier)
        record["stages"] = [{"key": stage, "status": "succeeded" if stage in record["checkpoints"] else "running" if stage == record["stage_key"] else "pending"} for stage in STAGES]
        return operation_dto(record)

    def operation(self, identifier: str) -> dict | None:
        for kind in ("migration", "backup", "restore"):
            record = self.store.load(kind, identifier)
            if record:
                return self.migration(identifier) if kind == "migration" else operation_dto(record)
        return None

    def events(self, identifier: str, after_seq: int = 0) -> dict:
        return {"items": self.store.events(identifier, after_seq)}

    def _checkpoint(self, record: dict, stage: str, **values) -> None:
        record.update(values)
        record["stage_key"] = stage
        record["migration_state"] = stage
        record["updated_at"] = time.time()
        record["version"] += 1
        record["checkpoints"][stage] = time.time()
        record["last_event_seq"] = self.store.event(record["id"], stage, {"status": record["status"], "message": record.get("message", "")})
        self.store.save("migration", record["id"], record)

    async def _run(self, identifier: str) -> None:
        record = self.store.load("migration", identifier)
        plan = record["plan"]
        target = self.targets.get(plan["target_node_id"])
        try:
            if not target:
                raise GateClosed("Зарегистрированное назначение недоступно; восстановите доверенное соединение")
            if not record["activation_committed"]:
                if self.runtime is None:
                    raise GateClosed("Runtime недоступен для подтверждения остановки")
                await self.runtime.close_and_drain(identifier)
                # User pause may have changed after the preview plan. Capture
                # its real final state only after all admitted commands drain.
                plan["initial_blocks"] = [block for block in self.store.status()["blocks"] if block["reason"] != "migration"]
                record["plan"] = plan
                if not record["checkpoints"].get("handoff_prepared"):
                    if not hasattr(self.runtime, "prepare_handoff"):
                        raise GateClosed("Runtime не умеет отзывать старые lease перед переносом")
                    handoff_arguments = {}
                    if plan["overdue_policy"] == "skip":
                        handoff_arguments["confirmed_skip_job_ids"] = plan["confirmed_skip_job_ids"]
                    result = await self.runtime.prepare_handoff(identifier, plan["source_generation"],
                        plan["overdue_policy"], plan["catch_up_interval_seconds"], **handoff_arguments)
                    record["checkpoints"]["handoff_prepared"] = time.time()
                    record["handoff_preparation"] = result
                    self.store.save("migration", identifier, record)
                await self.runtime.assert_quiesced()
                if self.store.status()["active_sends"]:
                    raise GateClosed("Начатые отправки ещё не завершены")
                record["checkpoints"]["stopping"] = time.time()
                self._checkpoint(record, "quiesced", message="Все исполнители остановлены; активных отправок нет")
                await self.check_profile(plan["target_node_id"])
                self._checkpoint(record, "preparing_target", message="Пассивный host назначения проверен")
                if hasattr(target, "provision"):
                    def installation_progress(completed, total):
                        record.update(stage_key="preparing_target", message="Устанавливаем пассивный host назначения" if completed is None else "Передаём проверенный релиз назначения", updated_at=time.time())
                        record["progress"] = None if completed is None else {"unit": "bytes", "completed": completed, "total": total,
                            "percent": completed / total * 100 if total else None, "estimated_seconds_remaining": None}
                        self.store.save("migration", identifier, record)
                    installed = await asyncio.to_thread(target.provision, migration_id=identifier,
                        node_id=plan["target_node_id"], https_url=plan["routing_plan"].get("url"), progress=installation_progress)
                    self.store.save("node-info", installed["node_id"], {**installed, "last_checked_at": time.time()})
                snapshot = self.store.root / "snapshots" / identifier
                if snapshot.exists() and "snapshotting" not in record["checkpoints"]:
                    try:
                        verify_snapshot(snapshot)
                    except VerificationError:
                        archive = snapshot.with_name(snapshot.name + ".interrupted-" + uuid.uuid4().hex[:8])
                        os.replace(snapshot, archive)
                if not snapshot.exists():
                    target_info = await asyncio.to_thread(target.info)
                    secret_context = {"system_id": plan["system_id"], "migration_id": identifier,
                                      "target_node_id": plan["target_node_id"], "generation": record["generation_after"]}
                    secret_envelope = self.secrets.export_for_target(target_info["secret_public_key"], context=secret_context)
                    def snapshot_progress(completed, total):
                        record.update(stage_key="snapshotting", message="Создаём согласованный снимок данных", updated_at=time.time())
                        record["progress"] = {"unit": "bytes", "completed": completed, "total": total,
                            "percent": completed / total * 100 if total else None, "estimated_seconds_remaining": None}
                        record["version"] += 1
                        self.store.save("migration", identifier, record)
                    await asyncio.to_thread(make_snapshot, self.data_root, self.db_path, snapshot, metadata={
                        "system_id": plan["system_id"], "release_id": plan["release_id"],
                        "schema_version": plan["schema_version"], "migration_id": identifier},
                        extras={"secrets-envelope.json": secret_envelope}, progress=snapshot_progress)
                manifest = verify_snapshot(snapshot)
                self._checkpoint(record, "snapshotting", snapshot_hash=manifest["database_sha256"],
                                 media_manifest_hash=sha256(snapshot / "manifest.json"),
                                 message="SQLite Backup API и файлы проверены")
                def progress(completed, total):
                    record["progress"] = {"unit": "bytes", "completed": completed, "total": total,
                        "percent": completed / total * 100 if total else None, "estimated_seconds_remaining": None}
                    record["stage_key"] = "transferring"
                    record["updated_at"] = time.time()
                    record["version"] += 1
                    self.store.save("migration", identifier, record)
                await asyncio.to_thread(target.transfer, identifier, snapshot, progress)
                self._checkpoint(record, "transferring", message="Переданные файлы проверены по SHA-256")
                actual = await asyncio.to_thread(target.verify, identifier)
                if actual != manifest:
                    raise VerificationError("Манифест назначения отличается от источника")
                self._checkpoint(record, "verifying", message="Данные, связи и хеши совпали")
                await self.runtime.stop()
                await self.runtime.assert_quiesced()
                retired = self.store.retire(identifier, target_node_id=plan["target_node_id"],
                    generation=record["generation_after"], snapshot_hash=record["snapshot_hash"], media_manifest_hash=record["media_manifest_hash"])
                self._checkpoint(record, "retiring_source", retired_receipt=retired, message="Источник лишён права публикации")
                grant = {"migration_id": identifier, "source_node_id": plan["source_node_id"],
                    "target_node_id": plan["target_node_id"], "system_id": plan["system_id"],
                    "generation": record["generation_after"], "snapshot_hash": record["snapshot_hash"],
                    "media_manifest_hash": record["media_manifest_hash"], "retired_receipt": retired}
                # Durable commit precedes issuing the grant. From here source rollback is forbidden.
                self._checkpoint(record, "activation_committed", activation_committed=True, grant=grant,
                                 allowed_actions=["resolve"], message="Передача права зафиксирована")
            receipt = await asyncio.to_thread(target.activate, record["grant"])
            self._checkpoint(record, "activating_target", activation_receipt=receipt,
                             message="Назначение приняло управление; публикации ещё остановлены")
            target_state = await asyncio.to_thread(target.query, identifier)
            if target_state.get("finalization"):
                await asyncio.to_thread(target.finalize, record)
                record["status"] = "succeeded"
                self._checkpoint(record, "completed", message="Подтверждён прежний финальный результат переноса", allowed_actions=[], progress=None)
                return
            routing = plan["routing_plan"]
            if routing.get("mode") != "local_only":
                nonce, challenge = str(uuid.uuid4()), uuid.uuid4().hex + uuid.uuid4().hex
                expected = await asyncio.to_thread(target.route_probe, nonce, challenge)
                def check_route():
                    # Normal certificate/SNI validation is mandatory. No auth or
                    # secret is forwarded to the proposed public origin.
                    request = urllib.request.Request(routing["url"] + "/api/v1/deployment/probe/" + nonce,
                                                     headers={"Accept": "application/json"})
                    with urllib.request.urlopen(request, timeout=15) as response:
                        if response.geturl() != request.full_url:
                            raise VerificationError("HTTPS-маршрут перенаправляет на другой адрес")
                        observed = json.loads(response.read(4096))
                    if observed != expected:
                        raise VerificationError("HTTPS-адрес не подтверждает целевой узел")
                try:
                    await asyncio.to_thread(check_route)
                except Exception:
                    record["status"] = "waiting_user"
                    self._checkpoint(record, "routing", message="HTTPS-маршрут к целевому узлу ещё не подтверждён. Исправьте DNS/proxy и повторите проверку", allowed_actions=["resolve"])
                    return
            self._checkpoint(record, "routing", message="Выбранный маршрут подтверждён")
            # Replicate independent blocks before removing only this migration's block.
            await asyncio.to_thread(target.apply_blocks, plan["initial_blocks"], identifier)
            report = {**record, "status": "succeeded", "stage_key": "completed", "migration_state": "completed", "allowed_actions": []}
            await asyncio.to_thread(target.finalize, report)
            record["status"] = "succeeded"
            self._checkpoint(record, "completed", message="Перенос завершён; источник остаётся перенесённым", allowed_actions=[], progress=None)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            record["status"] = "waiting_user"
            record["migration_state"] = "recovery_required" if record["activation_committed"] else "waiting_user"
            record["error"] = {"code": "recovery_required" if record["activation_committed"] else getattr(error, "code", "migration_interrupted"),
                               "message": str(error)[:500] if isinstance(error, (GateClosed, VerificationError)) else "Этап прерван. Проверьте доступность узлов и повторите безопасную сверку", "retryable": True}
            record["allowed_actions"] = ["resolve"] if record["activation_committed"] else ["resume", "cancel", "resume-source"]
            record["updated_at"] = time.time()
            self.store.save("migration", identifier, record)

    async def resume(self, identifier: str) -> dict:
        record = self.migration(identifier)
        if record["status"] == "succeeded":
            return record
        if record["status"] == "cancelled":
            raise GateClosed("Отменённую миграцию нельзя повторно активировать; создайте новый план")
        if identifier not in self.tasks or self.tasks[identifier].done():
            record["status"] = "running"
            record.pop("error", None)
            self.store.save("migration", identifier, record)
            self.tasks[identifier] = asyncio.create_task(self._run(identifier))
        return self.migration(identifier)

    async def resolve(self, identifier: str) -> dict:
        record = self.migration(identifier)
        if not record["activation_committed"]:
            return await self.resume(identifier)
        target = self.targets.get(record["plan"]["target_node_id"])
        if not target:
            raise GateClosed("Нет связи с назначением; источник остаётся остановленным")
        state = await asyncio.to_thread(target.query, identifier)
        if state["receipt"] and state["receipt"].get("grant") != record["grant"]:
            raise GateClosed("Квитанция назначения не соответствует зафиксированной передаче")
        return await self.resume(identifier)

    async def cancel(self, identifier: str) -> dict:
        record = self.migration(identifier)
        if record["activation_committed"]:
            raise GateClosed("После передачи права автоматический возврат источника запрещён")
        task = self.tasks.get(identifier)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        target = self.targets.get(record["plan"]["target_node_id"])
        if not target:
            raise GateClosed("Для отмены сначала подтвердите отсутствие активации назначения")
        await asyncio.to_thread(target.abort, identifier)
        record["status"] = "cancelled"
        record["allowed_actions"] = ["resume-source"]
        record["message"] = "Перенос отменён; публикации остаются остановленными до явного возобновления"
        self.store.save("migration", identifier, record)
        return record

    async def resume_source(self, identifier: str) -> dict:
        record = self.migration(identifier)
        if record["activation_committed"]:
            raise GateClosed("Право уже передано: источник нельзя включить")
        target = self.targets.get(record["plan"]["target_node_id"])
        if not target:
            raise GateClosed("Назначение недоступно; безопасный возврат не доказан")
        await asyncio.to_thread(target.abort, identifier)
        if self.store.retired_path.exists():
            retirement = json.loads(self.store.retired_path.read_text(encoding="utf-8"))
            if retirement["migration_id"] != identifier:
                raise GateClosed("Источник передан другой операцией")
            os.replace(self.store.retired_path, self.store.root / ("retirement-revoked-" + identifier + ".json"))
        self.store.unblock("migration", identifier)
        if self.runtime is not None and hasattr(self.runtime, "resume"):
            await self.runtime.resume()
        record["status"] = "cancelled"
        record["allowed_actions"] = []
        record["message"] = "Источник разрешён к управляемому запуску; прежние пользовательские паузы сохранены"
        self.store.save("migration", identifier, record)
        return record

    def list_backups(self) -> list[dict]:
        return [operation_dto(record) for record in self.store.list("backup") if not record.get("pruned")]

    def recover_pending(self) -> dict:
        recovered = []
        for record in self.store.list("migration"):
            if record["status"] in {"running", "queued", "waiting_external"} and record["id"] not in self.tasks:
                self.store.block("migration", record["id"])
                record.update(status="waiting_user", migration_state="recovery_required" if record["activation_committed"] else "waiting_user",
                    error={"code": "recovery_required" if record["activation_committed"] else "controller_restarted",
                           "message": "Управляющая служба перезапущена. Этап сохранён; выполните сверку узлов перед продолжением", "retryable": True},
                    allowed_actions=["resolve"] if record["activation_committed"] else ["resume", "cancel", "resume-source"])
                self.store.save("migration", record["id"], record)
                recovered.append(record["id"])
        for record in self.store.list("backup"):
            if record["status"] in {"running", "queued"} and record["id"] not in self.tasks:
                record.update(status="waiting_user", message="Копирование прервано перезапуском. Снимок не считается проверенной копией", allowed_actions=["resume", "cancel"])
                self.store.save("backup", record["id"], record)
                # A backup barrier is not removed until a live runtime proves
                # there are no outstanding send permits. Recovery stays explicit.
                self.store.block("backup", record["id"])
                recovered.append(record["id"])
        for record in self.store.list("restore"):
            if record["status"] in {"running", "queued"}:
                self.store.block("recovery", record["id"])
                record.update(status="waiting_user", stage_key="recovery_required", message="Восстановление прервано; сверка сохранённых наборов обязательна", allowed_actions=["resume"])
                self.store.save("restore", record["id"], record)
                recovered.append(record["id"])
        return {"recovered_operation_ids": recovered}

    def backup_settings(self) -> dict:
        settings = self.store.load("backup-settings", "configuration") or {"enabled": False, "interval_hours": 24, "keep": 7, "next_due_at": None}
        return {**settings, "recovery_key_saved": bool(self.secrets.backup_passphrase()),
                "directory": str(self.store.root / "backups")}

    def configure_backups(self, value: dict) -> dict:
        self.store.assert_mutation_allowed()
        current = self.backup_settings()
        enabled = value.get("enabled", current["enabled"])
        interval = value.get("interval_hours", current["interval_hours"])
        keep = value.get("keep", current["keep"])
        if not isinstance(enabled, bool) or not isinstance(interval, int) or not 1 <= interval <= 24 * 30 or not isinstance(keep, int) or not 1 <= keep <= 365:
            raise ValueError("Проверьте период и количество резервных копий")
        if value.get("recovery_passphrase"):
            self.secrets.set_backup_passphrase(value["recovery_passphrase"])
        if enabled and self.secrets.has_portable_secrets() and not self.secrets.backup_passphrase():
            raise ValueError("Для автоматических полных копий сохраните пароль восстановления в защищённом хранилище")
        settings = {"enabled": enabled, "interval_hours": interval, "keep": keep,
                    "next_due_at": current.get("next_due_at") or time.time() + interval * 3600}
        self.store.save("backup-settings", "configuration", settings)
        return self.backup_settings()

    async def scheduler_loop(self):
        while True:
            settings = self.backup_settings()
            state = self.store.status()
            if settings["enabled"] and settings["next_due_at"] <= time.time() and state["role"] == "active" and all(b["reason"] == "user_pause" for b in state["blocks"]):
                key = str(uuid.uuid5(uuid.NAMESPACE_URL, self.store.identity()["system_id"] + "/backup/" + str(settings["next_due_at"])))
                try:
                    await self.start_backup(key, self.secrets.backup_passphrase())
                except (GateClosed, ValueError):
                    pass
                else:
                    saved = {key: settings[key] for key in ("enabled", "interval_hours", "keep")}
                    saved["next_due_at"] = time.time() + settings["interval_hours"] * 3600
                    self.store.save("backup-settings", "configuration", saved)
            await asyncio.sleep(30)

    def prune_backups(self) -> list[str]:
        """Remove only verified, excess UUID directories owned by this installation."""
        keep = self.backup_settings()["keep"]
        successful = sorted((record for record in self.store.list("backup") if record["status"] == "succeeded" and not record.get("pruned")), key=lambda record: record["created_at"], reverse=True)
        removed = []
        root = (self.store.root / "backups").resolve()
        for record in successful[keep:]:
            identifier = str(uuid.UUID(record["id"]))
            target = (root / identifier).resolve()
            if target.parent != root or target.name != identifier or target.is_symlink():
                raise VerificationError("Каталог retention выходит за разрешённый backup root")
            # A successful old record is not enough: verify before considering it
            # disposable. Missing/corrupt evidence remains for explicit review.
            verify_snapshot(target)
            shutil.rmtree(target)
            record["pruned"] = True
            record["pruned_at"] = time.time()
            self.store.save("backup", identifier, record)
            removed.append(identifier)
        return removed

    async def start_backup(self, idempotency_key: str, recovery_passphrase: str | None = None) -> dict:
        previous = self.store.load("backup-command", idempotency_key)
        if previous:
            return operation_dto(self.store.load("backup", previous["id"]))
        if not self.runtime:
            raise GateClosed("Runtime не подключён: невозможно получить согласованную копию")
        self.store.assert_mutation_allowed()
        identifier = str(uuid.uuid4())
        secret_context = {"system_id": self.store.identity()["system_id"], "backup_id": identifier}
        extras = {}
        if self.secrets.has_portable_secrets():
            if not recovery_passphrase:
                raise ValueError("Для полной резервной копии нужен отдельный пароль восстановления секретов")
            extras["secrets-envelope.json"] = self.secrets.export_backup(recovery_passphrase, context=secret_context)
        recovery_passphrase = None
        record = {"id": identifier, "kind": "backup", "status": "running", "stage_key": "stopping",
                  "created_at": time.time(), "started_at": time.time(), "updated_at": time.time(), "allowed_actions": [], "progress": None}
        accepted = self.store.accept_command("backup", idempotency_key, "full-backup-v1", record, exclusive=True)
        if accepted["id"] != identifier:
            return accepted
        self.store.block("backup", identifier)
        self.tasks[identifier] = asyncio.create_task(self._run_backup(identifier, extras))
        return operation_dto(record)

    async def _run_backup(self, identifier: str, extras: dict):
        record = self.store.load("backup", identifier)
        interrupted = False
        try:
            await self.runtime.close_and_drain(identifier)
            await self.runtime.assert_quiesced()
            node = self.store.identity()
            directory = self.store.root / "backups" / identifier
            if directory.exists():
                try:
                    manifest = verify_snapshot(directory)
                except VerificationError:
                    archive = directory.with_name(directory.name + ".interrupted-" + uuid.uuid4().hex[:8])
                    os.replace(directory, archive)
                    manifest = None
            else:
                manifest = None
            if manifest is None:
                def snapshot_progress(completed, total):
                    record.update(stage_key="snapshotting", message="Создаём согласованную резервную копию", updated_at=time.time())
                    record["progress"] = {"unit": "bytes", "completed": completed, "total": total,
                        "percent": completed / total * 100 if total else None, "estimated_seconds_remaining": None}
                    record["version"] = record.get("version", 1) + 1
                    self.store.save("backup", identifier, record)
                manifest = await asyncio.to_thread(make_snapshot, self.data_root, self.db_path, directory,
                    metadata={"system_id": node["system_id"], "release_id": node["release_id"], "schema_version": node["schema_version"], "backup_id": identifier}, extras=extras, progress=snapshot_progress)
            record.update(status="succeeded", stage_key="verified", snapshot_hash=manifest["database_sha256"],
                          message="Полная копия данных проверена; секреты защищены отдельным паролем восстановления" if extras else "Полная копия данных проверена",
                          encrypted_secrets=(directory / "secrets-envelope.json").exists(), allowed_actions=[])
        except asyncio.CancelledError:
            interrupted = True
            record.update(status="waiting_user", allowed_actions=["resume", "cancel"], message="Копирование прервано остановкой службы; публикации остаются остановленными до сверки")
        except Exception as error:
            record.update(status="failed", allowed_actions=["resume", "cancel"], error={"code": "backup_failed", "message": str(error)[:500] if isinstance(error, (GateClosed, VerificationError)) else "Не удалось создать проверенную копию"})
        finally:
            record["updated_at"] = time.time()
            self.store.save("backup", identifier, record)
            if interrupted:
                return
            try:
                await self.runtime.assert_quiesced()
                self.store.unblock("backup", identifier)
                if hasattr(self.runtime, "resume"):
                    await self.runtime.resume()
            except Exception:
                record.update(status="waiting_user", allowed_actions=["resume", "cancel"], message="Копирование завершено или прервано; проверьте остановленное приложение перед продолжением")
                self.store.save("backup", identifier, record)
            if record["status"] == "succeeded":
                self.prune_backups()

    async def backup_action(self, identifier: str, action: str, recovery_passphrase: str | None = None) -> dict:
        record = self.store.load("backup", identifier)
        if not record:
            raise KeyError(identifier)
        if identifier in self.tasks and not self.tasks[identifier].done():
            raise GateClosed("Копирование ещё выполняется")
        if action == "cancel":
            await self.runtime.close_and_drain(identifier)
            await self.runtime.assert_quiesced()
            self.store.unblock("backup", identifier)
            record.update(status="cancelled", allowed_actions=[])
            self.store.save("backup", identifier, record)
            if hasattr(self.runtime, "resume"):
                await self.runtime.resume()
        elif action == "resume":
            extras = {}
            if self.secrets.has_portable_secrets():
                password = recovery_passphrase or self.secrets.backup_passphrase()
                if not password:
                    raise ValueError("Повторно введите пароль восстановления секретов")
                extras["secrets-envelope.json"] = self.secrets.export_backup(password,
                    context={"system_id": self.store.identity()["system_id"], "backup_id": identifier})
            self.store.block("backup", identifier)
            record.update(status="running", allowed_actions=[])
            self.store.save("backup", identifier, record)
            self.tasks[identifier] = asyncio.create_task(self._run_backup(identifier, extras))
        else:
            raise ValueError("Недопустимое действие резервной копии")
        return operation_dto(record)

    def verify_backup(self, identifier: str) -> dict:
        record = self.store.load("backup", identifier)
        if not record:
            raise KeyError(identifier)
        manifest = verify_snapshot(self.store.root / "backups" / str(uuid.UUID(identifier)))
        record["verified_at"] = time.time()
        record["snapshot_hash"] = manifest["database_sha256"]
        self.store.save("backup", identifier, record)
        return operation_dto(record)

    def restore_plan(self, identifier: str, recovery_passphrase: str | None = None) -> dict:
        backup = self.verify_backup(identifier)
        node = self.store.identity()
        manifest = verify_snapshot(self.store.root / "backups" / identifier)
        if any(manifest[key] != node[key] for key in ("system_id", "release_id", "schema_version")):
            raise VerificationError("Резервная копия другой системы или версии")
        envelope_path = self.store.root / "backups" / identifier / "secrets-envelope.json"
        if envelope_path.exists():
            if not recovery_passphrase:
                raise ValueError("Введите пароль восстановления для проверки пакета секретов")
            self.secrets.validate_backup(json.loads(envelope_path.read_text(encoding="utf-8")), recovery_passphrase,
                                         context={"system_id": node["system_id"], "backup_id": identifier})
        plan = {"id": str(uuid.uuid4()), "backup_id": identifier, "snapshot_hash": backup["snapshot_hash"],
                "requires_recovery_passphrase": envelope_path.exists(),
                "created_at": time.time(), "expires_at": time.time() + 900,
                "warning": "Восстановленная очередь останется на проверке: после этой копии могли появиться новые публикации"}
        self.store.save("restore-plan", plan["id"], plan)
        return plan

    async def execute_restore(self, plan_id: str, confirmation: bool, recovery_passphrase: str | None = None) -> dict:
        previous = self.store.load("restore", plan_id)
        if previous:
            return operation_dto(previous)
        plan = self.store.load("restore-plan", plan_id)
        if not plan or plan["expires_at"] < time.time() or not confirmation:
            raise ValueError("Нужны актуальный план и подтверждение восстановления")
        if not self.runtime:
            raise GateClosed("Runtime недоступен")
        self.store.assert_mutation_allowed()
        envelope_path = self.store.root / "backups" / plan["backup_id"] / "secrets-envelope.json"
        envelope = json.loads(envelope_path.read_text(encoding="utf-8")) if envelope_path.exists() else None
        secret_context = {"system_id": self.store.identity()["system_id"], "backup_id": plan["backup_id"]}
        if envelope:
            if not recovery_passphrase:
                raise ValueError("Для восстановления нужен пароль пакета секретов")
            self.secrets.validate_backup(envelope, recovery_passphrase, context=secret_context)
        if self.secrets.has_portable_secrets() and not recovery_passphrase:
            raise ValueError("Для сохранения текущих секретов перед заменой нужен пароль восстановления")
        result = {"id": plan_id, "kind": "restore", "status": "running", "stage_key": "stopping", "plan": plan,
                  "created_at": time.time(), "started_at": time.time(), "updated_at": time.time(), "message": "Останавливаем приложение перед восстановлением", "allowed_actions": []}
        self.store.save("restore", plan_id, result)
        self.store.block("recovery", plan_id)
        self.tasks[plan_id] = asyncio.create_task(self._run_restore(result, recovery_passphrase))
        return operation_dto(result)

    async def _run_restore(self, result: dict, recovery_passphrase: str | None):
        identifier, plan = result["id"], result["plan"]
        try:
            await self.runtime.close_and_drain(identifier)
            await self.runtime.stop()
            await self.runtime.assert_quiesced()
            source = self.store.root / "backups" / plan["backup_id"]
            manifest = verify_snapshot(source)
            if manifest["database_sha256"] != plan["snapshot_hash"]:
                raise VerificationError("Резервная копия изменилась после подтверждения плана")
            node = self.store.identity()
            envelope_path = source / "secrets-envelope.json"
            envelope = json.loads(envelope_path.read_text(encoding="utf-8")) if envelope_path.exists() else None
            secret_context = {"system_id": node["system_id"], "backup_id": plan["backup_id"]}
            if envelope:
                self.secrets.validate_backup(envelope, recovery_passphrase, context=secret_context)
            installed = False
            install_intent = self.store.load("restore-install", identifier)
            if install_intent and (self.data_root / "manifest.json").exists():
                actual = json.loads((self.data_root / "manifest.json").read_text(encoding="utf-8"))
                installed = actual.get("backup_id") == plan["backup_id"] and actual.get("database_sha256") == plan["snapshot_hash"]
                if installed:
                    incoming_database = safe_path(self.data_root, manifest["database_key"])
                    installed = incoming_database.is_file() and sha256(incoming_database) == plan["snapshot_hash"]
                if installed:
                    verify_snapshot(self.data_root)
            pre_restore = self.store.root / "before-restore" / identifier
            if pre_restore.exists():
                try:
                    verify_snapshot(pre_restore)
                except VerificationError:
                    if installed or not self.data_root.exists():
                        raise VerificationError("Нет проверенной копии прежнего состояния после подмены")
                    archive = pre_restore.with_name(pre_restore.name + ".interrupted-" + uuid.uuid4().hex[:8])
                    os.replace(pre_restore, archive)
            if not pre_restore.exists() and not installed:
                extra = {}
                if self.secrets.has_portable_secrets():
                    extra["secrets-envelope.json"] = self.secrets.export_backup(recovery_passphrase,
                        context={"system_id": node["system_id"], "backup_id": "before-restore-" + identifier})
                await asyncio.to_thread(make_snapshot, self.data_root, self.db_path, pre_restore,
                    metadata={"system_id": node["system_id"], "release_id": node["release_id"], "schema_version": node["schema_version"]}, extras=extra)
            elif not pre_restore.exists():
                raise VerificationError("Нет проверенной копии прежнего состояния; требуется ручная сверка")
            if not installed:
                result.update(stage_key="replacing_data", updated_at=time.time())
                self.store.save("restore", identifier, result)
                staging = self.data_root.with_name(self.data_root.name + ".restore-" + identifier)
                await asyncio.to_thread(copy_verified, source, staging)
                archive = self.data_root.with_name(self.data_root.name + ".replaced-" + identifier)
                self.store.save("restore-install", identifier, {"snapshot_hash": plan["snapshot_hash"], "backup_id": plan["backup_id"], "created_at": time.time()})
                if self.data_root.exists():
                    if archive.exists():
                        raise VerificationError("Обнаружены два прежних набора; автоматическая подмена запрещена")
                    os.replace(self.data_root, archive)
                os.replace(staging, self.data_root)
            if envelope:
                self.secrets.import_backup(envelope, recovery_passphrase, context=secret_context)
            durable_json(self.store.node_path, {**self.store.identity(), "database_key": manifest["database_key"],
                "restored_snapshot": plan["snapshot_hash"], "restore_id": identifier})
            self.db_path = safe_path(self.data_root, manifest["database_key"])
            result.update(status="waiting_user", stage_key="review_queue",
                message="Данные восстановлены. Подтвердите перевод восстановленной очереди в черновики для проверки возможных повторов", allowed_actions=["resolve"])
        except asyncio.CancelledError:
            result.update(status="waiting_user", stage_key="recovery_required", message="Восстановление прервано остановкой службы; публикации остаются остановленными", allowed_actions=["resume"])
        except Exception:
            result.update(status="waiting_user", stage_key="recovery_required", message="Восстановление прервано. Сохранён прежний набор и резервная копия; повторите сверку с паролем восстановления", allowed_actions=["resume"])
        result["updated_at"] = time.time()
        self.store.save("restore", identifier, result)

    async def resume_restore(self, identifier: str, recovery_passphrase: str | None = None) -> dict:
        result = self.store.load("restore", identifier)
        if not result:
            raise KeyError(identifier)
        if result["stage_key"] == "review_queue" or result["status"] == "succeeded":
            return operation_dto(result)
        if identifier in self.tasks and not self.tasks[identifier].done():
            return operation_dto(result)
        node = self.store.identity()
        if node["role"] != "active" or not any(b["reason"] == "recovery" and b["owner"] == identifier for b in self.store.status()["blocks"]):
            raise GateClosed("Не подтверждено владение восстановлением на этом узле")
        source = self.store.root / "backups" / result["plan"]["backup_id"]
        verify_snapshot(source)
        envelope = source / "secrets-envelope.json"
        if envelope.exists():
            if not recovery_passphrase:
                raise ValueError("Повторно введите пароль восстановления секретов")
            self.secrets.validate_backup(json.loads(envelope.read_text(encoding="utf-8")), recovery_passphrase,
                context={"system_id": node["system_id"], "backup_id": result["plan"]["backup_id"]})
        result.update(status="running", message="Повторяем сверку сохранённых наборов", allowed_actions=[])
        self.store.save("restore", identifier, result)
        self.tasks[identifier] = asyncio.create_task(self._run_restore(result, recovery_passphrase))
        return operation_dto(result)

    async def resolve_restore(self, identifier: str, acknowledge_duplicate_risk: bool) -> dict:
        record = self.store.load("restore", identifier)
        if not record or record["stage_key"] != "review_queue" or not acknowledge_duplicate_risk:
            raise ValueError("Подтвердите перевод восстановленной очереди в черновики для проверки")
        await self.runtime.assert_quiesced()
        affected = []
        from contextlib import closing
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            rows = connection.execute("SELECT * FROM jobs WHERE status IN ('queued','scheduled')").fetchall()
            affected_channels = set()
            for row in rows:
                job_id, status = row["id"], row["status"]
                if "target_channel_id" in columns:
                    affected_channels.add(row["target_channel_id"])
                if "post_data" in columns:
                    payload = json.loads(row["post_data"] or "{}")
                    payload["restore_review"] = {"restore_id": identifier, "original_status": status,
                        "scheduled_at": row["scheduled_at"] if "scheduled_at" in columns else None,
                        "queue_position": row["queue_position"] if "queue_position" in columns else None,
                        "review_required": True}
                    connection.execute("UPDATE jobs SET post_data=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), job_id))
                updates = ["status='waiting_confirmation'"]
                for name, expression in (("force_publish", "0"), ("queue_position", "NULL"), ("scheduled_at", "NULL"), ("version", "version+1"), ("render_plan", "NULL")):
                    if name in columns:
                        updates.append(name + "=" + expression)
                connection.execute("UPDATE jobs SET " + ",".join(updates) + " WHERE id=?", (job_id,))
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='job_events'").fetchone():
                    connection.execute("INSERT INTO job_events(job_id,event_type,old_status,new_status,message,created_at) VALUES(?,?,?,?,?,?)",
                        (job_id,"restore_review",status,"waiting_confirmation","После восстановления нужна ручная сверка канала перед повторной постановкой",datetime.now(UTC).isoformat()))
                affected.append(job_id)
            if affected_channels and connection.execute("SELECT 1 FROM sqlite_master WHERE name='queue_revisions'").fetchone():
                for channel_id in affected_channels:
                    connection.execute("INSERT INTO queue_revisions(channel_id,revision) VALUES(?,2) ON CONFLICT(channel_id) DO UPDATE SET revision=revision+1", (channel_id,))
            connection.commit()
        self.store.unblock("recovery", identifier)
        record.update(status="succeeded", stage_key="completed", result={"review_draft_ids": affected}, allowed_actions=[],
                      message="Очередь переведена в черновики для проверки; автоматического повтора нет")
        self.store.save("restore", identifier, record)
        if hasattr(self.runtime, "resume"):
            await self.runtime.resume()
        return operation_dto(record)
