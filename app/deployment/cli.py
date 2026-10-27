from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path

from app.deployment.controller import DeploymentService, LocalNodeTarget
from app.deployment.pull import WindowsPullBridge
from app.deployment.release import build_release, verify_release
from app.deployment.ssh import SSHNodeTarget
from app.host.control import ControlStore


def main():
    parser = argparse.ArgumentParser(description="Trusted Edizora installation commands")
    parser.add_argument("--control-dir", type=Path, default=Path(os.environ.get("PUBLISHER_CONTROL_DIR", "control")))
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("initialize")
    initialize.add_argument("--system-id", required=True)
    initialize.add_argument("--data-root", type=Path, required=True)
    initialize.add_argument("--release-id", required=True)
    initialize.add_argument("--schema-version", type=int)
    initialize.add_argument("--node-id")
    initialize.add_argument("--profile", choices=["local", "ubuntu-24.04-x86_64"], default="local")
    initialize.add_argument("--activate-initial-installation", action="store_true",
                            help="Trusted first setup only; never use this for a copied content database")
    commands.add_parser("status")
    register_release = commands.add_parser("register-release")
    register_release.add_argument("--archive", type=Path, required=True)
    register = commands.add_parser("register-ssh")
    register.add_argument("profile_id")
    register.add_argument("--known-hosts", type=Path, required=True)
    register.add_argument("--identity-file", type=Path)
    bridge = commands.add_parser("bridge")
    bridge.add_argument("--host", required=True)
    bridge.add_argument("--user", required=True)
    bridge.add_argument("--port", type=int, default=22)
    bridge.add_argument("--known-hosts", type=Path, required=True)
    bridge.add_argument("--identity-file", type=Path)
    bridge.add_argument("--once", action="store_true")
    release = commands.add_parser("build-release")
    release.add_argument("--source", type=Path, default=Path.cwd())
    release.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-release")
    verify.add_argument("archive", type=Path)
    options = parser.parse_args()
    if options.command == "build-release":
        result = build_release(options.source, options.output)
    elif options.command == "verify-release":
        result = verify_release(options.archive)
    else:
        store = ControlStore(options.control_dir)
        if options.command == "initialize":
            from app.db.migrations import SCHEMA_VERSION
            # Active bootstrap is deliberately unavailable when a content set is
            # already present: restore/migration must prove previous ownership.
            if options.activate_initial_installation and options.data_root.exists() and any(options.data_root.iterdir()):
                raise ValueError("Initial activation requires an empty data directory; use managed import/recovery for existing content")
            result = store.initialize(system_id=str(uuid.UUID(options.system_id)), data_root=options.data_root,
                role="active" if options.activate_initial_installation else "standby", profile=options.profile,
                node_id=str(uuid.UUID(options.node_id)) if options.node_id else None,
                release_id=options.release_id, schema_version=options.schema_version if options.schema_version is not None else SCHEMA_VERSION)
        elif options.command == "register-release":
            from app.deployment.snapshot import sha256
            manifest = verify_release(options.archive)
            identity = store.identity()
            if manifest["release_id"] != identity["release_id"] or manifest["schema_version"] != identity["schema_version"]:
                raise ValueError("Release/schema does not match this installation")
            result = {"archive": str(options.archive.resolve()), "release_id": manifest["release_id"],
                      "schema_version": manifest["schema_version"], "sha256": sha256(options.archive)}
            store.save("installed-release", "current", result)
        elif options.command == "status":
            result = store.status()
        elif options.command == "register-ssh":
            node = store.identity()
            service = DeploymentService(store, node["data_root"], Path(node["data_root"]) / "publisher.db")
            result = service.register_ssh_target(options.profile_id, known_hosts=options.known_hosts, identity_file=options.identity_file)
        else:
            endpoint = SSHNodeTarget(host=options.host, user=options.user, port=options.port,
                                     known_hosts=options.known_hosts, identity_file=options.identity_file)
            worker = WindowsPullBridge(LocalNodeTarget(store), endpoint)
            worker.register()
            if options.once:
                worker.once()
                result = {"polled": True}
            else:
                while True:
                    worker.once()
                    time.sleep(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
