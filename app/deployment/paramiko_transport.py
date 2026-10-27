"""Memory-only SSH authentication, pinned host keys and fixed passive provisioning."""
from __future__ import annotations

import io
import json
import shlex
import time
import uuid
import zipfile
from urllib.parse import urlsplit
from pathlib import Path

import paramiko

from app.deployment.release import verify_release
from app.deployment.snapshot import VerificationError, sha256, verify_snapshot
from app.host.control import GateClosed


PREFLIGHT = """import json,os,pathlib,platform,shutil
v={}
for line in pathlib.Path('/etc/os-release').read_text().splitlines():
 if '=' in line:
  k,x=line.split('=',1);v[k]=x.strip(chr(34))
identity=pathlib.Path('/var/lib/publisher/control/node.json')
node=json.loads(identity.read_text()) if identity.exists() else None
retired=pathlib.Path('/var/lib/publisher/control/retired.json')
if node and retired.exists():node['role']='retired'
data=pathlib.Path('/var/lib/publisher/data')
foreign=bool(data.exists() and any(data.iterdir()) and not node)
unit=pathlib.Path('/etc/systemd/system/publisher-host.service')
unit_conflict=unit.exists() and not any(name in unit.read_text() for name in ('Description=Edizora managed host and runtime', 'Description=Publisher managed host and runtime'))
unsafe_paths=any(pathlib.Path(p).is_symlink() for p in ['/var/lib/publisher','/var/lib/publisher/data','/var/lib/publisher/control','/opt/publisher','/opt/publisher/releases','/etc/publisher'])
print(json.dumps({'os_id':v.get('ID'),'os_version':v.get('VERSION_ID'),'architecture':platform.machine(),
 'free_bytes':min(shutil.disk_usage('/var/lib').free,shutil.disk_usage('/tmp').free),'hostname':platform.node(),
 'identity':node,'foreign_data':foreign,'unit_conflict':unit_conflict,'unsafe_paths':unsafe_paths,'root_preflight':os.geteuid()==0}))
"""


class ParamikoNodeTarget:
    def __init__(self, profile: dict, known_hosts: Path, credential_provider, expected: dict, release_provider):
        self.profile, self.known_hosts = dict(profile), Path(known_hosts)
        self.credentials = credential_provider
        self.expected, self.release_provider = dict(expected), release_provider
        self.provisioning_required = False
        self._agent_available = False

    def _credential(self) -> dict:
        value = self.credentials()
        if value is None:
            raise GateClosed("Повторно введите SSH-доступ: временные ключи и пароли не сохраняются после перезапуска controller")
        return value

    def _client(self):
        value = self._credential()
        client = paramiko.SSHClient()
        client.load_host_keys(str(self.known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        private = None
        if value.get("private_key"):
            errors = []
            for key_type in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    private = key_type.from_private_key(io.StringIO(value["private_key"]), password=value.get("private_key_passphrase"))
                    break
                except (paramiko.SSHException, ValueError) as error:
                    errors.append(type(error).__name__)
            if private is None:
                raise GateClosed("Не удалось открыть SSH-ключ. Проверьте формат OpenSSH и пароль ключа")
        try:
            client.connect(hostname=self.profile["host"], port=self.profile["port"], username=self.profile["user"],
                password=value.get("password"), pkey=private, timeout=15, banner_timeout=15, auth_timeout=20,
                disabled_algorithms={"keys": ["ssh-rsa"], "pubkeys": ["ssh-rsa"]},
                allow_agent=private is None and not value.get("password"), look_for_keys=private is None and not value.get("password"))
            return client
        except paramiko.BadHostKeyException:
            client.close()
            raise VerificationError("SSH host key изменился; соединение остановлено для проверки") from None
        except Exception:
            client.close()
            raise GateClosed("SSH не подтвердил доступ. Проверьте пользователя, ключ или пароль; публикации не возобновляются автоматически") from None

    def _prefix(self, *, as_publisher=False) -> tuple[str, bytes]:
        credentials = self._credential()
        if self.profile["user"] == "root":
            return ("runuser -u publisher -- " if as_publisher else ""), b""
        if as_publisher and self.profile["user"] == "publisher":
            return "", b""
        password = credentials.get("sudo_password")
        if password:
            return "sudo -S -p '' " + ("-u publisher " if as_publisher else ""), (password + "\n").encode()
        return "sudo -n " + ("-u publisher " if as_publisher else ""), b""

    def _execute(self, command: str, payload: bytes = b"", *, as_publisher=False, privileged=False,
                 timeout=120, allow_failure=False) -> tuple[int, bytes]:
        prefix, password = self._prefix(as_publisher=as_publisher) if privileged or as_publisher else ("", b"")
        with self._client() as client:
            incoming, outgoing, errors = client.exec_command(prefix + command, timeout=timeout)
            if password:
                incoming.write(password)
            if payload:
                incoming.write(payload)
            incoming.flush()
            incoming.channel.shutdown_write()
            # Read channels together: never deadlock when apt/systemd writes
            # substantial stderr. Raw remote output never enters progress/logs.
            channel = outgoing.channel
            output = bytearray()
            deadline = time.monotonic() + timeout
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                if channel.recv_ready():
                    chunk = channel.recv(65536)
                    if len(output) < 8 * 1024 * 1024:
                        output.extend(chunk[:8 * 1024 * 1024 - len(output)])
                if channel.recv_stderr_ready():
                    channel.recv_stderr(65536)
                if time.monotonic() >= deadline:
                    channel.close()
                    raise GateClosed("SSH-этап превысил время ожидания. Проверьте удалённую установку перед повтором")
                time.sleep(.01)
            status = channel.recv_exit_status()
        if status and not allow_failure:
            raise VerificationError("Удалённая фиксированная операция не выполнена. Проверьте SSH/sudo и журнал назначения")
        return status, bytes(output)

    def _invoke(self, action: str, identifier: str | None = None, value=None) -> dict:
        if action not in {"info", "verify", "activate", "query", "finalize", "abort", "blocks", "route-probe"}:
            raise ValueError("Unsupported node action")
        command = "/usr/local/bin/publisher-node-agent " + action
        if identifier:
            command += " " + str(uuid.UUID(identifier))
        _, output = self._execute(command, json.dumps(value).encode() if value is not None else b"", as_publisher=True)
        try:
            result = json.loads(output)
        except ValueError:
            raise VerificationError("Назначение вернуло неверный ответ управляющего протокола") from None
        if not isinstance(result, dict) or result.get("error"):
            raise VerificationError("Назначение отклонило управляющую операцию")
        return result

    def info(self):
        try:
            result = self._invoke("info")
            if result.get("installation_profile") != "ubuntu-24.04-x86_64":
                raise VerificationError("Поддержан SSH target Ubuntu 24.04 LTS x86-64")
            self._agent_available = True
            self.provisioning_required = False
            return result
        except VerificationError:
            pass
        try:
            _, output = self._execute("python3 -c " + shlex.quote(PREFLIGHT), privileged=True)
            report = json.loads(output)
        except (VerificationError, ValueError):
            # Minimal Ubuntu images may not have Python yet. These fixed shell
            # reads allow preflight before the post-drain installer adds Python.
            report = self._preflight_without_python()
        if report.get("os_id") != "ubuntu" or report.get("os_version") != "24.04" or report.get("architecture") != "x86_64":
            raise VerificationError("Для автоматической установки поддержана Ubuntu 24.04 LTS x86-64")
        if report.get("foreign_data") or report.get("unit_conflict") or report.get("unsafe_paths") or not report.get("root_preflight"):
            raise VerificationError("Назначение содержит чужие данные/службу либо не подтвердило необходимые sudo-права")
        self.release_provider()  # Verify same reviewed release before stopping source.
        existing = report.get("identity")
        if existing:
            if existing.get("system_id") != self.expected["system_id"] or existing.get("role") not in {"standby", "retired"}:
                raise VerificationError("На назначении уже зарегистрирован другой или активный Edizora")
            node = existing
        else:
            node = {"node_id": self.profile["pending_node_id"], "system_id": self.expected["system_id"],
                    "role": "unprovisioned", "generation": 1, "release_id": self.expected["release_id"],
                    "schema_version": self.expected["schema_version"], "data_root": "/var/lib/publisher/data"}
        self.provisioning_required = True
        return {**node, "profile": "ubuntu-24.04-x86_64", "operating_system": "Ubuntu", "os_version": "24.04",
                "architecture": "x86_64", "hostname": report["hostname"], "free_bytes": report["free_bytes"],
                "provisioning_required": True, "connected": True, "installed_release_id": existing.get("release_id") if existing else None}

    def _preflight_without_python(self):
        script = """printf '\n__PUB_OS__\n'; cat /etc/os-release
printf '\n__PUB_ARCH__\n'; uname -m
printf '\n__PUB_UID__\n'; id -u
printf '\n__PUB_HOST__\n'; hostname
printf '\n__PUB_DISK__\n'; df -Pk /var/lib /tmp
printf '\n__PUB_NODE__\n'; if test -f /var/lib/publisher/control/node.json; then cat /var/lib/publisher/control/node.json; fi
printf '\n__PUB_RETIRED__\n'; test ! -f /var/lib/publisher/control/retired.json || printf yes
printf '\n__PUB_FOREIGN__\n'; if test ! -f /var/lib/publisher/control/node.json; then find /var/lib/publisher/data -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null; fi
printf '\n__PUB_UNSAFE__\n'; for p in /var/lib/publisher /var/lib/publisher/data /var/lib/publisher/control /opt/publisher /opt/publisher/releases /etc/publisher; do test ! -L "$p" || printf yes; done
printf '\n__PUB_UNIT__\n'; if test -f /etc/systemd/system/publisher-host.service; then cat /etc/systemd/system/publisher-host.service; fi
printf '\n__PUB_END__\n'
"""
        _, output = self._execute("sh -c " + shlex.quote(script), privileged=True)
        values = {}
        current = None
        for line in output.decode("utf-8", errors="replace").splitlines():
            if line.startswith("__PUB_") and line.endswith("__"):
                current = line
                values[current] = []
            elif current:
                values[current].append(line)
        text = lambda name: "\n".join(values.get("__PUB_" + name + "__", [])).strip()
        version = {}
        for line in text("OS").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                version[key] = value.strip('"')
        free = []
        for line in text("DISK").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4 and parts[3].isdigit():
                free.append(int(parts[3]) * 1024)
        identity = json.loads(text("NODE")) if text("NODE") else None
        if identity and text("RETIRED"):
            identity["role"] = "retired"
        unit = text("UNIT")
        if not free:
            raise VerificationError("Назначение не подтвердило свободное место")
        return {"os_id": version.get("ID"), "os_version": version.get("VERSION_ID"), "architecture": text("ARCH"),
                "hostname": text("HOST"), "root_preflight": text("UID") == "0", "free_bytes": min(free),
                "identity": identity, "foreign_data": bool(text("FOREIGN")), "unsafe_paths": bool(text("UNSAFE")),
                "unit_conflict": bool(unit and not any(name in unit for name in ("Description=Edizora managed host and runtime", "Description=Publisher managed host and runtime")))}

    def provision(self, *, migration_id: str, node_id: str, https_url: str | None = None, progress=None):
        info = self.info()
        if not info.get("provisioning_required"):
            if any(info.get(key) != expected for key, expected in (("node_id", node_id), ("system_id", self.expected["system_id"]),
                    ("release_id", self.expected["release_id"]), ("schema_version", self.expected["schema_version"]))):
                raise VerificationError("Назначение изменило идентичность после проверки плана")
            if not info.get("host_ready"):
                self._execute("systemctl enable --now publisher-host", privileged=True, timeout=120)
                info = self._wait_host()
            return info
        archive = self.release_provider()
        manifest = verify_release(archive)
        if manifest["release_id"] != self.expected["release_id"] or manifest.get("schema_version") != self.expected["schema_version"]:
            raise VerificationError("Для переноса нужен тот же проверенный релиз и схема")
        remote_dir = "/tmp/publisher-bootstrap-" + str(uuid.UUID(migration_id))
        remote_archive, remote_script = remote_dir + "/release.zip", remote_dir + "/install.sh"
        with self._client() as client:
            sftp = client.open_sftp()
            try:
                try:
                    sftp.mkdir(remote_dir, mode=0o700)
                except OSError:
                    attributes = sftp.stat(remote_dir)
                    import stat
                    if not stat.S_ISDIR(attributes.st_mode) or stat.S_IMODE(attributes.st_mode) & 0o077:
                        raise VerificationError("Небезопасный staging-каталог удалённой установки")
                sftp.put(str(archive), remote_archive, callback=(lambda sent, total: progress(sent, total)) if progress else None)
                with zipfile.ZipFile(archive) as release:
                    installer = release.read("deploy/install-web-ubuntu.sh")
                with sftp.file(remote_script, "wb") as stream:
                    stream.write(installer)
                sftp.chmod(remote_script, 0o700)
            finally:
                sftp.close()
        if progress:
            progress(None, None)
        origin = urlsplit(https_url or self.profile.get("https_url") or "")
        if origin.scheme != "https" or not origin.hostname or origin.port not in {None, 443}:
            raise VerificationError("Автоматическая Ubuntu-установка требует HTTPS-домен на порту 443")
        arguments = ["bash", remote_script, remote_archive, sha256(archive), self.expected["system_id"], origin.hostname, node_id]
        self._execute(" ".join(shlex.quote(item) for item in arguments), privileged=True, timeout=1800)
        self._execute("systemctl enable --now publisher-host", privileged=True, timeout=120)
        # Caddy/routing is an explicit later step. Provisioning never starts a
        # sender or claims success merely because the installer exited zero.
        result = self._wait_host()
        if any(result.get(key) != expected for key, expected in (("node_id", node_id), ("system_id", self.expected["system_id"]),
                ("release_id", self.expected["release_id"]), ("schema_version", self.expected["schema_version"]))):
            raise VerificationError("После установки идентичность/релиз назначения не совпали с планом")
        if result["role"] not in {"standby", "retired"}:
            raise GateClosed("Установленное назначение не находится в пассивной роли")
        self.provisioning_required = False
        self._agent_available = True
        return result

    def _wait_host(self):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            value = self._invoke("info")
            if value.get("host_ready"):
                return value
            time.sleep(.5)
        raise GateClosed("Назначение не подтвердило готовую управляющую службу после установки")

    def transfer(self, migration_id: str, snapshot: Path, progress=None):
        manifest = verify_snapshot(snapshot)
        with self._client() as client:
            prefix, password = self._prefix(as_publisher=True)
            incoming, outgoing, errors = client.exec_command(prefix + "/usr/local/bin/publisher-node-agent receive " + str(uuid.UUID(migration_id)), timeout=1800)
            if password:
                incoming.write(password)
            # Zip streams directly into the authenticated SSH channel. No
            # plaintext credential or content archive is saved on the client.
            class Sink:
                def __init__(self): self.position = 0
                def write(inner, data):
                    incoming.write(data); inner.position += len(data); return len(data)
                def tell(inner): return inner.position
                def flush(inner): incoming.flush()
            sink = Sink()
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as package:
                completed = 0
                for entry in manifest["files"]:
                    with (snapshot / entry["key"]).open("rb") as source, package.open(entry["key"], "w") as output:
                        while chunk := source.read(1024 * 1024):
                            output.write(chunk)
                            completed += len(chunk)
                            if progress:
                                progress(completed, manifest["total_bytes"])
                package.write(snapshot / "manifest.json", "manifest.json")
            incoming.flush()
            incoming.channel.shutdown_write()
            response = outgoing.read(8 * 1024 * 1024)
            if outgoing.channel.recv_exit_status():
                raise VerificationError("Защищённая передача прервана; проверенные checkpoints сохранены")
        result = json.loads(response)
        if result.get("error"):
            raise VerificationError("Назначение отклонило переданный снимок")
        return result

    def verify(self, identifier): return self._invoke("verify", identifier)
    def activate(self, grant): return self._invoke("activate", grant["migration_id"], grant)
    def query(self, identifier): return self._invoke("query", identifier)
    def finalize(self, record): return self._invoke("finalize", record["id"], record)
    def abort(self, identifier): return self._invoke("abort", identifier)
    def route_probe(self, nonce, challenge): return self._invoke("route-probe", nonce, {"challenge": challenge})
    def apply_blocks(self, blocks, migration_id=None): return self._invoke("blocks", value={"blocks": blocks, "migration_id": migration_id})
