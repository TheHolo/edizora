"""Fault fixtures for SSH policy/provision orchestration; no external server is used."""
import io
import json
import shlex
import uuid
from pathlib import Path

import paramiko
import pytest

from app.deployment.controller import DeploymentService, LocalNodeTarget
from app.deployment.paramiko_transport import ParamikoNodeTarget
from app.deployment.release import build_release
from app.deployment.snapshot import VerificationError
from app.host.control import ControlStore, GateClosed, durable_json


def installation(root, system, role="active"):
    control = ControlStore(root / "control")
    data = root / "data"
    data.mkdir(parents=True)
    control.initialize(system_id=system, data_root=data, role=role, release_id="test-release")
    return control, data


def artifact(tmp_path):
    source = tmp_path / "source"
    for folder in ("app/db", "deploy", "frontend/dist"):
        (source / folder).mkdir(parents=True)
    (source / "app/db/migrations.py").write_text("SCHEMA_VERSION = 1\n")
    (source / "deploy/install-web-ubuntu.sh").write_text("#!/bin/bash\n# fixed test installer fixture\n")
    (source / "frontend/dist/index.html").write_text("<main>fixture</main>")
    result = build_release(source, tmp_path / "archives")
    return Path(result["path"]), result["release_id"]


def profile():
    return {"id": str(uuid.uuid4()), "pending_node_id": str(uuid.uuid4()), "host": "fixture.example",
            "port": 22, "user": "root", "https_url": "https://publisher.fixture.example"}


class PreflightTarget(ParamikoNodeTarget):
    report = {"os_id": "ubuntu", "os_version": "24.04", "architecture": "x86_64", "free_bytes": 10**12,
              "hostname": "fixture-vps", "identity": None, "foreign_data": False, "unit_conflict": False, "root_preflight": True}
    def _invoke(self, *args, **kwargs):
        raise VerificationError("Agent is intentionally absent in this preflight fixture")
    def _execute(self, command, *args, **kwargs):
        assert kwargs.get("privileged") is True
        assert command.startswith("python3 -c ")
        return 0, json.dumps(self.report).encode()


def test_remote_preflight_requires_supported_os_and_does_not_install(tmp_path):
    release, release_id = artifact(tmp_path)
    selected = profile()
    target = PreflightTarget(selected, tmp_path / "known_hosts", lambda: {"password": "fixture-password"},
        {"system_id": "system", "release_id": release_id, "schema_version": 1}, lambda: release)
    result = target.info()
    assert result["role"] == "unprovisioned"
    assert result["provisioning_required"] is True
    assert result["node_id"] == selected["pending_node_id"]
    assert result["installed_release_id"] is None
    target.report = {**target.report, "architecture": "aarch64"}
    with pytest.raises(VerificationError):
        target.info()
    target.report = {**PreflightTarget.report, "foreign_data": True}
    with pytest.raises(VerificationError):
        target.info()


def test_profile_credentials_never_enter_control_documents_and_are_lost_on_restart(tmp_path):
    store, data = installation(tmp_path, str(uuid.uuid4()))
    service = DeploymentService(store, data, data / "publisher.sqlite")
    selected = service.create_profile({"host": "fixture.example", "user": "root"})
    result = service.set_profile_credentials(selected["id"], {"password": "fixture-ssh-password", "sudo_password": "fixture-sudo-password"})
    assert result == {"profile_id": selected["id"], "credentials_present": True, "expires_on_controller_restart": True}
    with store.connect() as connection:
        documents = "\n".join(row[0] for row in connection.execute("SELECT payload FROM documents"))
    assert "fixture-ssh-password" not in documents
    assert "fixture-sudo-password" not in documents
    restarted = DeploymentService(store, data, data / "publisher.sqlite")
    assert restarted._ssh_credentials == {}


def test_paramiko_rejects_changed_host_key_without_exposing_password(tmp_path, monkeypatch):
    key = paramiko.RSAKey.generate(1024)
    state = {}
    class Client:
        def load_host_keys(self, path): state["known_hosts"] = path
        def set_missing_host_key_policy(self, policy): state["policy"] = policy
        def connect(self, **kwargs):
            state["password"] = kwargs["password"]
            raise paramiko.BadHostKeyException("fixture.example", key, key)
        def close(self): state["closed"] = True
    monkeypatch.setattr(paramiko, "SSHClient", Client)
    target = ParamikoNodeTarget(profile(), tmp_path / "pinned", lambda: {"password": "fixture-password"}, {}, lambda: None)
    with pytest.raises(VerificationError) as error:
        target._client()
    assert "fixture-password" not in str(error.value)
    assert isinstance(state["policy"], paramiko.RejectPolicy)
    assert state["known_hosts"] == str(tmp_path / "pinned")
    assert state["closed"]


def test_provision_uploads_verified_release_and_executes_only_fixed_installer(tmp_path):
    release, release_id = artifact(tmp_path)
    selected = profile()
    expected = {"system_id": str(uuid.uuid4()), "release_id": release_id, "schema_version": 1}
    recorded, uploaded = [], {}
    class Buffer(io.BytesIO):
        def __init__(self, path): super().__init__(); self.path = path
        def close(self): uploaded[self.path] = self.getvalue(); super().close()
    class SFTP:
        def mkdir(self, path, mode): recorded.append(("mkdir", path, mode))
        def put(self, local, remote, callback=None):
            uploaded[remote] = Path(local).read_bytes()
            if callback: callback(len(uploaded[remote]), len(uploaded[remote]))
        def file(self, path, mode): return Buffer(path)
        def chmod(self, *args): pass
        def close(self): pass
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def open_sftp(self): return SFTP()
    class ProvisionTarget(ParamikoNodeTarget):
        def info(self): return {"provisioning_required": True}
        def _client(self): return Client()
        def _execute(self, command, *args, **kwargs):
            recorded.append(("command", command, kwargs.get("privileged")))
            return 0, b""
        def _invoke(self, *args, **kwargs):
            return {**expected, "node_id": selected["pending_node_id"], "role": "standby", "host_ready": True}
    target = ProvisionTarget(selected, tmp_path / "known_hosts", lambda: {"password": "fixture-password"}, expected, lambda: release)
    identifier = str(uuid.uuid4())
    result = target.provision(migration_id=identifier, node_id=selected["pending_node_id"], https_url=selected["https_url"])
    assert result["role"] == "standby"
    commands = [entry for entry in recorded if entry[0] == "command"]
    installer = shlex.split(commands[0][1])
    assert installer[0] == "bash"
    assert installer[1] == "/tmp/publisher-bootstrap-" + identifier + "/install.sh"
    assert installer[-1] == selected["pending_node_id"]
    assert installer[-2] == "publisher.fixture.example"
    assert commands[1][1] == "systemctl enable --now publisher-host"
    assert all(item[2] is True and "fixture-password" not in item[1] for item in commands)
    assert any(path.endswith("release.zip") for path in uploaded)


async def test_provision_is_only_called_after_shared_gate_and_runtime_drain(tmp_path):
    import sqlite3
    system = str(uuid.uuid4())
    source, data = installation(tmp_path / "source", system)
    destination, _ = installation(tmp_path / "destination", system, "standby")
    database = data / "publisher.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO records VALUES(4)")
    connection.commit()
    connection.close()
    calls = []
    class Hooks:
        async def deployment_readiness(self): return {"ready": True, "issues": []}
        async def accept_maintenance(self, identifier): calls.append("accept")
        async def close_and_drain(self, identifier): calls.append("drain")
        async def prepare_handoff(self, *args, **kwargs): return {}
        async def assert_quiesced(self): assert "drain" in calls
        async def stop(self): calls.append("stop")
    class Target(LocalNodeTarget):
        provisioned = False
        def info(self):
            value = super().info()
            return {**value, "role": "standby" if self.provisioned else "unprovisioned", "provisioning_required": not self.provisioned}
        def provision(self, **kwargs):
            assert calls[:2] == ["accept", "drain"]
            assert any(block["reason"] == "migration" for block in source.status()["blocks"])
            assert source.status()["active_sends"] == 0
            self.provisioned = True
            calls.append("provision")
            return self.info()
    target = Target(destination)
    service = DeploymentService(source, data, database, Hooks())
    service.register_target(target)
    plan = await service.create_plan({"target_node_id": destination.identity()["node_id"]})
    assert not target.provisioned
    operation = await service.start_migration(plan["id"], 1, True, "provision-order")
    await service.tasks[operation["id"]]
    assert service.migration(operation["id"])["status"] == "succeeded"
    assert calls.index("provision") < calls.index("stop")


def test_source_artifact_must_match_current_release_before_provision(tmp_path):
    release, release_id = artifact(tmp_path / "artifact")
    store, data = installation(tmp_path / "installation", str(uuid.uuid4()))
    store.save("installed-release", "current", {"archive": str(release)})
    service = DeploymentService(store, data, data / "publisher.sqlite")
    with pytest.raises(VerificationError):
        service.release_archive()
    assert store.status()["publication_allowed"]
    identity = store.identity()
    durable_json(store.node_path, {**identity, "release_id": release_id})
    assert service.release_archive() == release


def test_minimal_ubuntu_preflight_uses_only_reads_before_python_install(tmp_path):
    selected = profile()
    fixture = """
__PUB_OS__
ID=ubuntu
VERSION_ID="24.04"
__PUB_ARCH__
x86_64
__PUB_UID__
0
__PUB_HOST__
minimal-ubuntu
__PUB_DISK__
Filesystem 1024-blocks Used Available Capacity Mounted
/dev/vda1 10000000 1000000 9000000 10% /
__PUB_NODE__
__PUB_RETIRED__
__PUB_FOREIGN__
__PUB_UNSAFE__
__PUB_UNIT__
__PUB_END__
"""
    commands = []
    class Minimal(ParamikoNodeTarget):
        def _execute(self, command, *args, **kwargs):
            commands.append(command)
            assert kwargs.get("privileged") is True
            assert command.startswith("sh -c ")
            assert "apt-get" not in command
            return 0, fixture.encode()
    target = Minimal(selected, tmp_path / "known_hosts", lambda: {}, {}, lambda: None)
    report = target._preflight_without_python()
    assert report["os_version"] == "24.04"
    assert report["architecture"] == "x86_64"
    assert report["free_bytes"] == 9000000 * 1024
    assert report["root_preflight"]
    assert not report["foreign_data"]


def test_loaded_temporary_ssh_profile_requires_credentials_again(tmp_path):
    store, data = installation(tmp_path, str(uuid.uuid4()))
    service = DeploymentService(store, data, data / "publisher.sqlite")
    selected = service.create_profile({"host": "fixture.example", "user": "root"})
    selected.update(node_id=selected["pending_node_id"], trusted=True)
    store.save("profile", selected["id"], selected)
    known = store.root / "known_hosts"
    known.write_text("fixture.example ssh-rsa " + paramiko.RSAKey.generate(1024).get_base64())
    store.save("ssh-transport", selected["node_id"], {"mode": "paramiko", "profile_id": selected["id"],
        "node_id": selected["node_id"], "known_hosts": str(known)})
    service.set_profile_credentials(selected["id"], {"password": "fixture-only-in-memory"})
    restarted = DeploymentService(store, data, data / "publisher.sqlite")
    endpoint = restarted.targets[selected["node_id"]]
    with pytest.raises(GateClosed) as error:
        endpoint._client()
    assert "Повторно введите" in str(error.value)
    assert "fixture-only-in-memory" not in str(error.value)
