from __future__ import annotations

import asyncio
from contextlib import closing
import io
import json
import sqlite3
import uuid
import zipfile
from pathlib import Path

import pytest

from app.deployment.agent import receive
from app.deployment.controller import DeploymentService, LocalNodeTarget
from app.deployment.pull import PullNodeTarget, poll_pull_commands, register_pull_node, reply_pull_command
from app.deployment.release import build_release, verify_release
from app.deployment.snapshot import VerificationError, database_report, make_snapshot, safe_path, verify_snapshot
from app.host.control import ControlStore, GateClosed
from app.host.secrets import SecretStore


def node(root: Path, system: str, role="active", generation=1):
    store = ControlStore(root / "control")
    data = root / "data"
    data.mkdir(parents=True)
    store.initialize(system_id=system, data_root=data, role=role, generation=generation, release_id="test-release")
    return store, data


def content(root: Path):
    path = root / "publisher.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE jobs(id INTEGER PRIMARY KEY, status TEXT NOT NULL, text TEXT)")
        db.execute("INSERT INTO jobs VALUES(17,'completed','Опубликованный пост')")
        db.execute("INSERT INTO jobs VALUES(99,'queued','Будущий пост')")
        db.commit()
    (root / "медиа").mkdir()
    (root / "медиа" / "картина.bin").write_bytes(b"immutable-media")
    return path


class Hooks:
    def __init__(self):
        self.drained = False
        self.stopped = False

    async def close_and_drain(self, identifier):
        self.drained = True

    async def accept_maintenance(self, identifier):
        return {"accepted": True}

    async def deployment_readiness(self):
        return {"ready": True, "issues": [], "counts": {"queued": 1}}

    async def prepare_handoff(self, *args, **kwargs):
        assert self.drained
        return {"revoked_task_ids": [], "policy": args[2]}

    async def assert_quiesced(self):
        assert self.drained

    async def stop(self):
        self.stopped = True


async def finish(service, identifier):
    await service.tasks[identifier]
    return service.migration(identifier)


def test_missing_node_identity_cannot_publish_or_mutate(tmp_path):
    store = ControlStore(tmp_path)
    assert store.status()["role"] == "recovery_required"
    with pytest.raises(GateClosed):
        store.acquire_permit(1, 1)
    with pytest.raises(GateClosed):
        store.assert_mutation_allowed()


def test_gate_is_cross_process_durable_and_old_permit_can_drain(tmp_path):
    store, _ = node(tmp_path, str(uuid.uuid4()))
    permit = store.acquire_permit(2, 4, 8)
    second = ControlStore(store.root)
    second.block("migration", "transfer")
    with pytest.raises(GateClosed):
        store.acquire_permit(3, 1, 8)
    assert second.validate_permit(permit)["job_version"] == 4
    with pytest.raises(GateClosed):
        store.retire("transfer", target_node_id="other", generation=2, snapshot_hash="hash")
    second.release_permit(permit)
    assert store.status()["active_sends"] == 0
    with pytest.raises(GateClosed):
        second.unblock("migration", "another-owner")


def test_user_pause_does_not_prevent_draft_edits(tmp_path):
    store, _ = node(tmp_path, str(uuid.uuid4()))
    store.block("user_pause", "user")
    store.assert_mutation_allowed()
    with pytest.raises(GateClosed):
        store.assert_publication_allowed()


def test_retired_marker_survives_reopen_and_initializer(tmp_path):
    store, _ = node(tmp_path, str(uuid.uuid4()))
    store.retire("migration", target_node_id="target", generation=2, snapshot_hash="sha")
    reopened = ControlStore(store.root)
    assert reopened.identity()["role"] == "retired"
    with pytest.raises(GateClosed):
        reopened.initialize(system_id="different", data_root=tmp_path / "new", role="active")
    with pytest.raises(GateClosed):
        reopened.acquire_permit(1, 1)


def test_backup_api_keeps_uncheckpointed_wal_and_unicode(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = data / "source.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records VALUES(8,'данные в WAL')")
        connection.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        manifest = make_snapshot(data, database, tmp_path / "snapshot", metadata={"system_id": "fixture"})
    assert manifest["database_report"]["tables"]["records"]["rows"] == 1
    with closing(sqlite3.connect(tmp_path / "snapshot" / "source.sqlite")) as connection:
        assert connection.execute("SELECT value FROM records").fetchone()[0] == "данные в WAL"


def test_manifest_corruption_prevents_verified_copy(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database = content(data)
    snapshot = tmp_path / "snapshot"
    make_snapshot(data, database, snapshot, metadata={})
    (snapshot / "медиа" / "картина.bin").write_bytes(b"changed")
    with pytest.raises(VerificationError):
        verify_snapshot(snapshot)


@pytest.mark.parametrize("key", ["../escape", "C:/escape", "C:\\escape", "//server/share", "safe/../../escape", "safe\\evil", "/absolute", "safe/./evil"])
def test_paths_cannot_escape_snapshot(tmp_path, key):
    with pytest.raises(VerificationError):
        safe_path(tmp_path, key)


def test_zip_slip_refused_without_outside_write(tmp_path):
    store, _ = node(tmp_path / "node", "system", "standby")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../outside", b"bad")
    payload.seek(0)
    with pytest.raises(VerificationError):
        receive(LocalNodeTarget(store), str(uuid.uuid4()), payload)
    assert not (store.root / "outside").exists()


async def test_migration_preserves_rows_media_and_pause_and_retires_source(tmp_path):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, target_data = node(tmp_path / "target", system, "standby")
    database = content(data)
    source.block("user_pause", "owner")
    hooks = Hooks()
    service = DeploymentService(source, data, database, hooks)
    service.register_target(LocalNodeTarget(target))
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    assert not any(b["reason"] == "migration" for b in source.status()["blocks"])
    result = await service.start_migration(plan["id"], 1, True, str(uuid.uuid4()))
    result = await finish(service, result["id"])
    assert result["status"] == "succeeded", result
    assert source.identity()["role"] == "retired"
    assert target.identity()["role"] == "active"
    assert hooks.stopped
    assert target.status()["blocks"] == [{"reason": "user_pause", "owner": "owner", "created": target.status()["blocks"][0]["created"]}]
    assert database_report(target_data / "publisher.sqlite")["tables"] == database_report(database)["tables"]
    assert (target_data / "медиа" / "картина.bin").read_bytes() == b"immutable-media"
    assert target.load("migration", result["id"])["status"] == "succeeded"
    with pytest.raises(GateClosed):
        source.assert_publication_allowed()


async def test_lost_activation_ack_never_resumes_source_and_resolves_same_grant(tmp_path):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "target", system, "standby")
    database = content(data)
    class LostAck(LocalNodeTarget):
        lost = False
        def activate(self, grant):
            result = super().activate(grant)
            if not self.lost:
                self.lost = True
                raise ConnectionError("test connection lost after durable activation")
            return result
    endpoint = LostAck(target)
    service = DeploymentService(source, data, database, Hooks())
    service.register_target(endpoint)
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    initial = await service.start_migration(plan["id"], 1, True, "request")
    failed = await finish(service, initial["id"])
    assert failed["migration_state"] == "recovery_required"
    assert target.identity()["role"] == "active"
    assert not target.status()["publication_allowed"]
    assert source.identity()["role"] == "retired"
    with pytest.raises(GateClosed):
        await service.resume_source(initial["id"])
    # Controller restart reads durable grant. Target returns the same receipt.
    restarted = DeploymentService(source, data, database, Hooks())
    restarted.register_target(endpoint)
    await restarted.resolve(initial["id"])
    completed = await finish(restarted, initial["id"])
    assert completed["status"] == "succeeded"
    assert completed["grant"] == failed["grant"]
    assert target.status()["publication_allowed"]


async def test_return_transfer_uses_current_active_database(tmp_path):
    system = str(uuid.uuid4())
    windows, home_data = node(tmp_path / "home", system)
    cloud, cloud_data = node(tmp_path / "cloud", system, "standby")
    home_database = content(home_data)
    outbound = DeploymentService(windows, home_data, home_database, Hooks())
    outbound.register_target(LocalNodeTarget(cloud))
    plan = await outbound.create_plan({"target_node_id": cloud.identity()["node_id"]})
    operation = await outbound.start_migration(plan["id"], 1, True, "out")
    assert (await finish(outbound, operation["id"]))["status"] == "succeeded"
    with closing(sqlite3.connect(cloud_data / "publisher.sqlite")) as database:
        database.execute("INSERT INTO jobs VALUES(105,'completed','Новая облачная публикация')")
        database.commit()
    returning = DeploymentService(cloud, cloud_data, cloud_data / "publisher.sqlite", Hooks())
    returning.register_target(LocalNodeTarget(windows))
    plan = await returning.create_plan({"target_node_id": windows.identity()["node_id"]})
    operation = await returning.start_migration(plan["id"], 1, True, "back")
    result = await finish(returning, operation["id"])
    assert result["status"] == "succeeded", result
    with closing(sqlite3.connect(home_database)) as database:
        assert database.execute("SELECT text FROM jobs WHERE id=105").fetchone()[0] == "Новая облачная публикация"
    assert windows.identity()["generation"] == 3
    assert cloud.identity()["role"] == "retired"
    assert windows.status()["publication_allowed"]


async def test_start_requires_freeze_hooks_and_fresh_explicit_plan(tmp_path):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "target", system, "standby")
    database = content(data)
    service = DeploymentService(source, data, database)
    service.register_target(LocalNodeTarget(target))
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    with pytest.raises(ValueError):
        await service.start_migration(plan["id"], 1, False, "not-accepted")
    with pytest.raises(GateClosed):
        await service.start_migration(plan["id"], 1, True, "no-runtime")
    assert source.status()["publication_allowed"]


async def test_backup_verify_restore_keeps_recovery_gate(tmp_path):
    store, data = node(tmp_path, str(uuid.uuid4()))
    database = content(data)
    service = DeploymentService(store, data, database, Hooks())
    backup = await service.start_backup("backup-key")
    await service.tasks[backup["id"]]
    assert service.verify_backup(backup["id"])["status"] == "succeeded"
    assert store.status()["publication_allowed"]
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("INSERT INTO jobs VALUES(130,'completed','After backup')")
        connection.commit()
    plan = service.restore_plan(backup["id"])
    result = await service.execute_restore(plan["id"], True)
    await service.tasks[result["id"]]
    result = service.operation(result["id"])
    assert result["status"] == "waiting_user"
    assert not store.status()["publication_allowed"]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE id=130").fetchone()[0] == 0
    completed = await service.resolve_restore(result["id"], True)
    assert completed["result"]["review_draft_ids"] == [99]
    assert store.status()["publication_allowed"]


def test_pull_bridge_rejects_unknown_node_and_preserves_command_identity(tmp_path):
    system = str(uuid.uuid4())
    source, _ = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "home", system, "standby")
    info = LocalNodeTarget(target).info()
    register_pull_node(source, info)
    request_id = str(uuid.uuid4())
    request = {"id": request_id, "node_id": info["node_id"], "action": "query", "created_at": 0, "expires_at": 9999999999}
    source.save("pull-command", request_id, request)
    assert poll_pull_commands(source, info["node_id"])["commands"] == [request]
    response = {"id": request_id, "result": {"ok": True}}
    reply_pull_command(source, info["node_id"], response)
    assert poll_pull_commands(source, info["node_id"])["commands"] == []
    with pytest.raises(GateClosed):
        reply_pull_command(source, "different", response)
    with pytest.raises(GateClosed):
        reply_pull_command(source, info["node_id"], {"id": request_id, "result": {"ok": False}})


def test_release_is_deterministic_and_excludes_secrets(tmp_path):
    source = tmp_path / "source"
    (source / "app").mkdir(parents=True)
    (source / "frontend" / "dist").mkdir(parents=True)
    (source / "app" / "main.py").write_text("print('working tree')", encoding="utf-8")
    (source / "frontend" / "dist" / "index.html").write_text("<main>Publisher</main>", encoding="utf-8")
    (source / "frontend" / "scripts").mkdir()
    (source / "frontend" / "scripts" / "check-api.mjs").write_text("// schema check", encoding="utf-8")
    (source / "bot-settings.toml").write_text('[bot]\ntimezone = "UTC"\n', encoding="utf-8")
    (source / ".env").write_text("BOT_TOKEN=secret", encoding="utf-8")
    first = build_release(source, tmp_path / "release1")
    second = build_release(source, tmp_path / "release2")
    assert first["sha256"] == second["sha256"]
    manifest = verify_release(Path(first["path"]))
    assert ".env" not in {record["key"] for record in manifest["files"]}
    assert {"frontend/scripts/check-api.mjs", "bot-settings.toml"} <= {record["key"] for record in manifest["files"]}


def test_machine_secret_store_does_not_write_plaintext(tmp_path):
    store = SecretStore(tmp_path)
    store.write("BOT_TOKEN", "fixture-token-no-real-access")
    store.write("HOME_WORKER_LMSTUDIO_API_KEY", "home-only-key")
    assert store.status()["BOT_TOKEN"] is True
    assert SecretStore(tmp_path).read("BOT_TOKEN") == "fixture-token-no-real-access"
    assert b"fixture-token-no-real-access" not in store.path.read_bytes()
    assert b"home-only-key" not in store.path.read_bytes()
    with pytest.raises(ValueError):
        store.write("ARBITRARY_ENV", "value")


def test_secret_handoff_is_bound_to_target_and_generation(tmp_path):
    source, destination, impostor = (SecretStore(tmp_path / name) for name in ("source", "destination", "impostor"))
    source.write("BOT_TOKEN", "fixture-token")
    source.write("HOME_WORKER_LMSTUDIO_API_KEY", "home-only-key")
    context = {"system_id": "system", "target_node_id": "target", "generation": 2, "migration_id": "migration"}
    envelope = source.export_for_target(destination.public_key(), context=context)
    assert "fixture-token" not in json.dumps(envelope)
    with pytest.raises(ValueError):
        impostor.import_for_target(envelope, context=context)
    with pytest.raises(ValueError):
        destination.import_for_target(envelope, context={**context, "generation": 3})
    destination.import_for_target(envelope, context=context)
    assert destination.read("BOT_TOKEN") == "fixture-token"
    assert destination.read("HOME_WORKER_LMSTUDIO_API_KEY") is None


async def test_migration_transfers_secrets_encrypted_for_destination(tmp_path):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, target_data = node(tmp_path / "target", system, "standby")
    database = content(data)
    secrets = SecretStore(source.root)
    secrets.write("BOT_TOKEN", "fixture-no-real-token")
    service = DeploymentService(source, data, database, Hooks())
    service.register_target(LocalNodeTarget(target))
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    result = await service.start_migration(plan["id"], 1, True, "encrypted-transfer")
    assert (await finish(service, result["id"]))["status"] == "succeeded"
    assert SecretStore(target.root).read("BOT_TOKEN") == "fixture-no-real-token"
    assert "fixture-no-real-token" not in (target_data / "secrets-envelope.json").read_text()


async def test_backup_requires_recovery_key_and_wrong_key_cannot_replace_data(tmp_path):
    store, data = node(tmp_path, str(uuid.uuid4()))
    database = content(data)
    service = DeploymentService(store, data, database, Hooks())
    service.secrets.write("BOT_TOKEN", "fixture-secret")
    with pytest.raises(ValueError):
        await service.start_backup("missing-recovery-key")
    backup = await service.start_backup("encrypted-backup", "fixture recovery passphrase")
    await service.tasks[backup["id"]]
    assert service.verify_backup(backup["id"])["encrypted_secrets"]
    before = database_report(database)
    with pytest.raises(ValueError):
        service.restore_plan(backup["id"], "incorrect recovery passphrase")
    assert database_report(database) == before
    plan = service.restore_plan(backup["id"], "fixture recovery passphrase")
    with pytest.raises(ValueError):
        await service.execute_restore(plan["id"], True, "incorrect recovery passphrase")
    assert store.status()["publication_allowed"]
    assert service.secrets.read("BOT_TOKEN") == "fixture-secret"


async def test_retention_preserves_newest_verified_copies(tmp_path):
    store, data = node(tmp_path, str(uuid.uuid4()))
    service = DeploymentService(store, data, content(data), Hooks())
    assert service.backup_settings()["enabled"] is False
    service.configure_backups({"enabled": False, "keep": 2})
    copies = []
    for index in range(3):
        record = await service.start_backup("retention-" + str(index))
        copies.append(record["id"])
        await service.tasks[record["id"]]
    assert store.load("backup", copies[0])["pruned"] is True
    assert not (store.root / "backups" / copies[0]).exists()
    assert {record["id"] for record in service.list_backups()} == set(copies[1:])
    assert service.verify_backup(copies[-1])["status"] == "succeeded"


def test_restart_reclassifies_orphan_operations_and_keeps_blocks(tmp_path):
    store, data = node(tmp_path, str(uuid.uuid4()))
    identifier = str(uuid.uuid4())
    store.save("migration", identifier, {"id": identifier, "status": "running", "activation_committed": True})
    service = DeploymentService(store, data, content(data), Hooks())
    assert service.recover_pending()["recovered_operation_ids"] == [identifier]
    record = store.load("migration", identifier)
    assert record["status"] == "waiting_user"
    assert record["allowed_actions"] == ["resolve"]
    assert store.status()["blocks"][0]["reason"] == "migration"
    with pytest.raises(GateClosed):
        store.acquire_permit(1, 1)


async def test_target_identity_commit_without_receipt_can_recover(tmp_path, monkeypatch):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "target", system, "standby")
    service = DeploymentService(source, data, content(data), Hooks())
    endpoint = LocalNodeTarget(target)
    service.register_target(endpoint)
    original_save = target.save
    raised = False
    def fail_after_node_commit(kind, identifier, value):
        nonlocal raised
        if kind == "activation" and value.get("status") == "accepted" and not raised:
            raised = True
            raise OSError("fixture crash before accepted receipt commit")
        return original_save(kind, identifier, value)
    monkeypatch.setattr(target, "save", fail_after_node_commit)
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    result = await service.start_migration(plan["id"], 1, True, "crash-before-receipt")
    result = await finish(service, result["id"])
    assert result["migration_state"] == "recovery_required"
    assert target.identity()["role"] == "active"
    assert target.load("activation", result["id"])["status"] == "installing"
    assert not target.status()["publication_allowed"]
    await service.resolve(result["id"])
    assert (await finish(service, result["id"]))["status"] == "succeeded"
    assert source.identity()["role"] == "retired"


async def test_unready_confirmed_queue_prevents_stop(tmp_path):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "target", system, "standby")
    class UnreadyHooks(Hooks):
        async def deployment_readiness(self):
            return {"ready": False, "issues": [{"job_id": 99, "reasons": ["Файл отсутствует"]}]}
    hooks = UnreadyHooks()
    service = DeploymentService(source, data, content(data), hooks)
    service.register_target(LocalNodeTarget(target))
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    assert plan["readiness"]["issues"][0]["job_id"] == 99
    with pytest.raises(GateClosed):
        await service.start_migration(plan["id"], 1, True, "unready")
    assert source.status()["publication_allowed"]
    assert not hooks.drained


async def test_lost_final_ack_does_not_reset_new_target_pause(tmp_path):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "target", system, "standby")
    class LostFinalAck(LocalNodeTarget):
        raised = False
        def finalize(self, record):
            result = super().finalize(record)
            if not self.raised:
                self.raised = True
                self.store.block("user_pause", "new-target-user-action")
                raise ConnectionError("fixture final ACK lost")
            return result
    endpoint = LostFinalAck(target)
    service = DeploymentService(source, data, content(data), Hooks())
    service.register_target(endpoint)
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    record = await service.start_migration(plan["id"], 1, True, "lost-final-ack")
    record = await finish(service, record["id"])
    assert record["status"] == "waiting_user"
    assert target.load("finalization", record["id"])["status"] == "completed"
    assert source.identity()["role"] == "retired"
    await service.resolve(record["id"])
    assert (await finish(service, record["id"]))["status"] == "succeeded"
    assert target.status()["blocks"][0]["owner"] == "new-target-user-action"


@pytest.mark.parametrize("boundary", ["quiesced", "preparing_target", "snapshotting", "transferring", "verifying", "retiring_source", "activation_committed", "activating_target", "routing", "completed"])
async def test_checkpoint_interruption_reuses_one_grant_and_one_owner(tmp_path, boundary):
    system = str(uuid.uuid4())
    source, data = node(tmp_path / "source", system)
    target, _ = node(tmp_path / "target", system, "standby")
    class Interrupted(DeploymentService):
        raised = False
        def _checkpoint(self, record, stage, **values):
            super()._checkpoint(record, stage, **values)
            if stage == boundary and not self.raised:
                self.raised = True
                raise OSError("fixture checkpoint interruption")
    service = Interrupted(source, data, content(data), Hooks())
    endpoint = LocalNodeTarget(target)
    service.register_target(endpoint)
    plan = await service.create_plan({"target_node_id": target.identity()["node_id"]})
    record = await service.start_migration(plan["id"], 1, True, "boundary")
    record = await finish(service, record["id"])
    assert record["status"] == "waiting_user"
    assert sum(store.status()["publication_allowed"] for store in (source, target)) <= 1
    grant = record.get("grant")
    if record["activation_committed"]:
        with pytest.raises(GateClosed):
            await service.resume_source(record["id"])
        await service.resolve(record["id"])
    else:
        await service.resume(record["id"])
    completed = await finish(service, record["id"])
    assert completed["status"] == "succeeded", completed
    if grant:
        assert completed["grant"] == grant
    assert source.identity()["role"] == "retired"
    assert target.identity()["role"] == "active"
    assert target.status()["publication_allowed"]
