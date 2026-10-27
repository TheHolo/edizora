from argparse import Namespace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.auth import AuthStore
from app.db.models import ApplicationState, User
from app.db.session import create_database
from app.host.bootstrap import prepare_initial_activation
from app.host.control import ControlStore, GateClosed, durable_json
from app.runtime import InstanceLock
from app.web import trusted_setup


def installation(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    store = ControlStore(tmp_path / "control")
    store.initialize(system_id=str(uuid4()), data_root=data, role="standby", schema_version=3)
    store.save("installed-release", "current", {"release_id": "fixture"})
    return store, data, data / "publisher.sqlite"


def options(lock, *, activate=True):
    return Namespace(initialize=True, upgrade=False, admin_id=17, set_password=False,
                     standby=False, adopt_checkout=False, activate_initial_installation=activate,
                     _host_lock=lock)


async def test_installed_standby_requires_explicit_activation_and_keeps_identity(tmp_path):
    store, data, database = installation(tmp_path)
    original = store.identity()
    with InstanceLock(store.root / "host.lock") as lock:
        await trusted_setup(options(lock), store, AuthStore(store.root), data, database)
    assert store.identity()["role"] == "active"
    assert store.identity()["node_id"] == original["node_id"]
    assert store.identity()["system_id"] == original["system_id"]
    engine, sessions = create_database("sqlite+aiosqlite:///" + database.as_posix())
    try:
        async with sessions() as session:
            state = await session.get(ApplicationState, 1)
            assert state.system_id == original["system_id"]
            assert (await session.scalar(select(User))).telegram_user_id == 17
    finally:
        await engine.dispose()


async def test_plain_initialize_does_not_grant_standby_authority(tmp_path):
    store, data, database = installation(tmp_path)
    with InstanceLock(store.root / "host.lock") as lock:
        await trusted_setup(options(lock, activate=False), store, AuthStore(store.root), data, database)
    assert store.identity()["role"] == "standby"
    assert not store.status()["publication_allowed"]


@pytest.mark.parametrize("history", ["database", "media", "migration", "block", "retired", "generation"])
def test_initial_activation_rejects_existing_content_and_prior_use(tmp_path, history):
    store, data, database = installation(tmp_path)
    if history == "database":
        database.write_bytes(b"an existing content copy")
    elif history == "media":
        (data / "original.jpg").write_bytes(b"existing original")
    elif history == "migration":
        store.save("migration", "fixture", {"stage": "activation_committed"})
    elif history == "block":
        store.block("migration", "fixture")
    elif history == "retired":
        durable_json(store.retired_path, {"generation": 1})
    else:
        durable_json(store.node_path, {**store.identity(), "generation": 2})
    with InstanceLock(store.root / "host.lock") as lock:
        with pytest.raises(GateClosed):
            prepare_initial_activation(store, data, database, host_lock=lock)
    assert not store.status()["publication_allowed"]


def test_initial_activation_requires_actual_matching_held_host_lock(tmp_path):
    store, data, database = installation(tmp_path)
    lock = InstanceLock(store.root / "host.lock")
    with pytest.raises(GateClosed):
        prepare_initial_activation(store, data, database, host_lock=lock)
    with InstanceLock(tmp_path / "another.lock") as wrong:
        with pytest.raises(GateClosed):
            prepare_initial_activation(store, data, database, host_lock=wrong)


async def test_failed_password_setup_never_activates_installed_node(tmp_path, monkeypatch):
    store, data, database = installation(tmp_path)
    entries = iter(["long-enough-password", "different-password"])
    monkeypatch.setattr("app.web.getpass.getpass", lambda _: next(entries))
    with InstanceLock(store.root / "host.lock") as lock:
        request = options(lock)
        request.set_password = True
        with pytest.raises(SystemExit, match="Пароли не совпали"):
            await trusted_setup(request, store, AuthStore(store.root), data, database)
    assert store.identity()["role"] == "standby"
    assert not database.exists()
