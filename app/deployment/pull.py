"""Windows host pull transport: all connections originate at the home computer.

The cloud controller keeps owning the operation. A paired Windows bridge polls
its authenticated SSH account and executes fixed, generation-bound node calls.
"""
from __future__ import annotations

import io
import json
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from app.deployment.agent import receive
from app.deployment.controller import LocalNodeTarget
from app.deployment.snapshot import VerificationError, verify_snapshot
from app.host.control import ControlStore, GateClosed


class PullNodeTarget:
    def __init__(self, store: ControlStore, node_id: str, *, timeout: float = 120):
        self.store = store
        self.node_id = str(uuid.UUID(node_id))
        self.timeout = timeout

    def info(self) -> dict:
        record = self.store.load("pull-node", self.node_id)
        if not record or time.time() - record["heartbeat_at"] > 45:
            raise GateClosed("Ждём домашний компьютер: локальный host не выходит на связь")
        return record["info"]

    def _call(self, action: str, migration_id: str | None = None, value=None) -> dict:
        self.info()
        identifier = str(uuid.uuid4())
        request = {"id": identifier, "node_id": self.node_id, "action": action,
                   "migration_id": migration_id, "value": value, "created_at": time.time(),
                   "expires_at": time.time() + self.timeout}
        self.store.save("pull-command", identifier, request)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            response = self.store.load("pull-response", identifier)
            if response:
                if response.get("error"):
                    raise VerificationError("Локальный host отклонил этап. Проверьте журнал локальной установки")
                return response["result"]
            time.sleep(0.1)
        raise GateClosed("Нет ответа домашнего host. Этап сохранён; публикации остаются остановленными")

    def transfer(self, migration_id: str, snapshot: Path, progress=None) -> dict:
        manifest = verify_snapshot(snapshot)
        result = self._call("receive", migration_id, {"snapshot_hash": manifest["database_sha256"]})
        if progress:
            progress(manifest["total_bytes"], manifest["total_bytes"])
        return result

    def verify(self, migration_id: str):
        return self._call("verify", migration_id)

    def activate(self, grant: dict):
        return self._call("activate", grant["migration_id"], grant)

    def query(self, migration_id: str):
        return self._call("query", migration_id)

    def finalize(self, record: dict):
        return self._call("finalize", record["id"], record)

    def apply_blocks(self, blocks: list[dict], migration_id: str | None = None):
        return self._call("blocks", value={"blocks": blocks, "migration_id": migration_id})

    def route_probe(self, nonce: str, challenge: str) -> dict:
        return self._call("route-probe", nonce, {"challenge": challenge})

    def abort(self, migration_id: str):
        return self._call("abort", migration_id)


def register_pull_node(store: ControlStore, info: dict) -> dict:
    node = store.identity()
    if info.get("system_id") != node["system_id"] or info.get("role") not in {"standby", "retired"}:
        raise GateClosed("Домашний host имеет другую систему или активную роль")
    identifier = str(uuid.UUID(info["node_id"]))
    store.save("pull-node", identifier, {"info": info, "heartbeat_at": time.time()})
    store.save("profile", identifier, {"id": identifier, "name": info.get("hostname", "Домашний компьютер"),
               "node_id": identifier, "type": "pull_host", "trusted": True, "status": "checked"})
    return {"registered": True, "system_id": node["system_id"], "node_id": identifier}


def poll_pull_commands(store: ControlStore, node_id: str) -> dict:
    record = store.load("pull-node", node_id)
    if not record:
        raise GateClosed("Host ещё не зарегистрирован")
    record["heartbeat_at"] = time.time()
    store.save("pull-node", node_id, record)
    commands = [item for item in store.list("pull-command")
                if item["node_id"] == node_id and item["expires_at"] > time.time()
                and not store.load("pull-response", item["id"])]
    return {"commands": sorted(commands, key=lambda x: x["created_at"])[:1]}


def reply_pull_command(store: ControlStore, node_id: str, response: dict) -> dict:
    command = store.load("pull-command", response["id"])
    if not command or command["node_id"] != node_id:
        raise GateClosed("Ответ не принадлежит этому host")
    old = store.load("pull-response", response["id"])
    if old and old != response:
        raise GateClosed("Повторный ответ изменяет уже принятый результат")
    store.save("pull-response", response["id"], response)
    return {"accepted": True}


class WindowsPullBridge:
    def __init__(self, target: LocalNodeTarget, ssh_endpoint):
        self.target = target
        self.endpoint = ssh_endpoint

    def register(self):
        info = self.target.info()
        result = self.endpoint._invoke("bridge-register", value=info)
        if result["system_id"] != info["system_id"]:
            raise GateClosed("Удалённая система не соответствует локальной привязке")
        return result

    def once(self):
        node = self.target.store.identity()
        response = self.endpoint._invoke("bridge-poll", node["node_id"])
        for request in response["commands"]:
            existing = self.target.store.load("pull-result", request["id"])
            if existing:
                result = existing
            else:
                try:
                    value = self._execute(request)
                    result = {"id": request["id"], "result": value}
                except Exception:
                    result = {"id": request["id"], "error": {"code": "local_stage_failed"}}
                self.target.store.save("pull-result", request["id"], result)
            self.endpoint._invoke("bridge-reply", node["node_id"], result)

    def _execute(self, request):
        node = self.target.store.identity()
        if request["node_id"] != node["node_id"] or request["expires_at"] < time.time():
            raise GateClosed("Запрос устарел или предназначен другому host")
        action, identifier, value = request["action"], request["migration_id"], request.get("value")
        if action == "receive":
            process = subprocess.Popen(self.endpoint._command("export", identifier), stdout=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                manifest = receive(self.target, identifier, process.stdout)
                if process.wait(timeout=120) or manifest["database_sha256"] != value["snapshot_hash"]:
                    raise VerificationError("Актуальный снимок облачного источника не подтверждён")
                return manifest
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        if action == "verify":
            return self.target.verify(identifier)
        if action == "activate":
            return self.target.activate(value)
        if action == "query":
            return self.target.query(identifier)
        if action == "finalize":
            return self.target.finalize(value)
        if action == "blocks":
            self.target.apply_blocks(value["blocks"], value.get("migration_id"))
            return {"ok": True}
        if action == "route-probe":
            return self.target.route_probe(identifier, value["challenge"])
        if action == "abort":
            self.target.abort(identifier)
            return {"ok": True}
        raise ValueError("Unsupported pull operation")
