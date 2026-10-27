"""Encrypted AI profiles and host-owned outgoing workers; no extra pairing commands."""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.host.secrets import SecretStore
from app.news.worker_config import HomeWorkerSettings


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    provider: str = "openai"
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=255)
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4096)
    cloud_consent: bool = False

    def settings(self):
        if self.provider not in {"lmstudio", "ollama", "openai"}:
            raise ValueError("Выберите поддерживаемый протокол AI")
        from urllib.parse import urlsplit
        import ipaddress
        host = urlsplit(self.base_url).hostname or ""
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_private
        except ValueError:
            local = host == "localhost"
        if not local and not self.cloud_consent:
            raise ValueError("Подтвердите передачу текстов облачному провайдеру")
        values = {"llm_provider": self.provider, f"{self.provider}_base_url": self.base_url,
                  f"{self.provider}_model": self.model}
        if self.provider != "ollama":
            values[f"{self.provider}_api_key"] = self.api_key
        elif self.api_key.get_secret_value():
            raise ValueError("Для ключа доступа выберите совместимый с OpenAI API протокол")
        return HomeWorkerSettings(_config_file="", _env_file=None, token="desktop-internal", **values)


def profiles(store: SecretStore) -> list[dict]:
    return json.loads(store.read("DESKTOP_AI_CONNECTIONS") or "[]")


def public_profile(profile):
    return {key: value for key, value in profile.items() if key not in {"api_key", "credential", "command_id", "configured"}}


async def probe(connection: Connection):
    from app.news.llm_factory import create_news_client, list_model_ids
    from app.services.title_client import TitleTranslation
    settings = connection.settings()
    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            catalog = await list_model_ids(session, settings)
        except Exception:
            catalog = []  # Some compatible providers have no catalog endpoint.
        client = create_news_client(session, settings)
        client.max_retries = 0
        client.timeout = aiohttp.ClientTimeout(total=45)
        await client._structured_chat(
            "Translate into English. Return JSON matching the supplied schema.",
            '{"title":"Тестовое название"}', TitleTranslation)
    return {"status": "ok", "models": catalog, "message": "Модель отвечает в нужном формате"}


class DirectApi:
    lease_seconds = 1800

    def __init__(self, runtime, credential):
        self.runtime, self.credential = runtime, credential

    async def call(self, operation, payload):
        return await self.runtime.worker_command(operation, payload, self.credential)

    @staticmethod
    def task_payload(task):
        from app.worker_v1 import WorkerApi
        return WorkerApi.task_payload(task)


async def add_connection(runtime, store, actor_id, connection, command_id=None):
    connection.settings()
    current = profiles(store)
    existing = next((p for p in current if command_id and p.get('command_id') == command_id), None)
    if existing and existing.get('configured'):
        return public_profile(existing)
    if not existing and len(current) >= 12:
        raise ValueError("Можно сохранить до 12 подключений")
    if existing:
        profile = existing
    else:
        pair = await runtime.pair_worker(actor_id, {})
        registered = await runtime.worker_command("register", {
            "pairing_code": pair["pairing_code"], "name": connection.name, "provider": connection.provider}, "")
        profile = {**connection.model_dump(exclude={"api_key"}), "api_key": connection.api_key.get_secret_value(),
                   "id": str(uuid4()), "worker_id": registered["worker_id"], "credential": registered["credential"],
                   "command_id": command_id, "configured": False}
        current.append(profile)
        store.write("DESKTOP_AI_CONNECTIONS", json.dumps(current))
    await runtime.worker_command("capabilities", {"models": [{"id": profile['model'], "type": "text"}],
        "capabilities": {"task_kinds": ["news", "title_translation"], "source_types": ["website", "youtube", "telegram", "manual"]}}, profile['credential'])
    await runtime.ai_selection(actor_id, {"worker_id": profile['worker_id'], "model_id": profile['model']})
    profile['configured'] = True
    store.write("DESKTOP_AI_CONNECTIONS", json.dumps(current))
    return public_profile(profile)


async def run_connections(runtime, control):
    from app.news.facade import NewsSourceFacade
    from app.news.http import PublicOnlyResolver
    from app.worker_v1 import RegisteredWorker

    class ConfiguredWorker(RegisteredWorker):
        async def model_catalog(self):
            return [self.settings.model_name]

    tasks = {}

    async def run_profile(profile):
        values = {k: profile[k] for k in Connection.model_fields if k in profile}
        settings = Connection(**values).settings()
        async with aiohttp.ClientSession(trust_env=False) as session, aiohttp.ClientSession(
            trust_env=False, connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver())) as sources:
            worker = ConfiguredWorker(api=DirectApi(runtime, profile["credential"]), settings=settings,
                session=session, extractor=NewsSourceFacade.from_session(sources))
            while True:
                try:
                    worked = await worker.run_once()
                except Exception:
                    worked = False  # Transport bodies and credentials never enter diagnostics.
                await asyncio.sleep(.1 if worked else 5)

    try:
        while True:
            current = {p["id"]: p for p in profiles(SecretStore(control))}
            for identifier in list(tasks):
                if identifier not in current:
                    tasks.pop(identifier).cancel()
            for identifier, profile in current.items():
                if identifier not in tasks or tasks[identifier].done():
                    tasks[identifier] = asyncio.create_task(run_profile(profile))
            await asyncio.sleep(2)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
