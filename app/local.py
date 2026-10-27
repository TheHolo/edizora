"""Local bot + news worker launcher and diagnostics. No Telegram writes in check mode."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.bootstrap import bootstrap
from app.config import Settings
from app.home_worker import run_worker
from app.logging_config import SecretFilter, configure_logging
from app.news.llm_factory import create_news_client, list_model_ids
from app.news.models import ExtractedNewsSource, NewsSourceKind
from app.news.worker_config import HomeWorkerSettings
from app.news.worker_errors import LlmError
from app.runtime import InstanceLock,assert_legacy_entrypoint_allowed
from app.services.model_selection import LMStudioModels, ModelSelection

WORKER_DEPENDENCIES = {
    "trafilatura": "trafilatura", "extruct": "extruct", "yt_dlp": "yt-dlp",
    "youtube_transcript_api": "youtube-transcript-api",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    level: str
    message: str


def load_worker_settings(config_file: Path, token: str) -> HomeWorkerSettings:
    settings = HomeWorkerSettings(_config_file=config_file, token=token)
    if "llm_provider" not in settings.model_fields_set:
        settings = settings.model_copy(update={"llm_provider": "lmstudio"})
    return settings


def load_local_settings(config_file: Path) -> tuple[Settings, HomeWorkerSettings]:
    # A single process owns both ends; no manually synchronized secrets are needed.
    token = secrets.token_urlsafe(32)
    worker = load_worker_settings(config_file, token)
    settings = Settings(
        _config_file=config_file, news_worker_token=token,
        news_api_bind_host="127.0.0.1", news_api_host="127.0.0.1",
        news_model_name=worker.model_name,
    )
    if worker.llm_provider == "lmstudio":
        worker = ModelSelection(settings.storage_path, worker).worker_settings()
        settings = settings.model_copy(update={"news_model_name": worker.model_name})
    worker = worker.model_copy(update={
        "vps_api_url": f"http://127.0.0.1:{settings.news_api_port}",
    })
    return settings, worker


def sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Эта команда рассчитана на файловую SQLite; для PostgreSQL используйте pg_dump.")
    return Path(url.database).resolve()


def backup_database(database_url: str, directory: Path = Path("backups")) -> Path:
    source = sqlite_path(database_url)
    if not source.is_file():
        raise ValueError("База ещё не создана. Резервное копирование доступно после первого запуска.")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = (directory / f"publisher-{stamp}.sqlite3").resolve()
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(target)) as copy:
                # SQLite's backup API produces a consistent snapshot, including WAL data.
                original.backup(copy)
                result = copy.execute("PRAGMA quick_check").fetchone()
                if result != ("ok",):
                    raise ValueError("Проверка целостности резервной копии не пройдена.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def diagnose(settings: Settings, worker: HomeWorkerSettings, *, smoke_test: bool = False) -> list[Check]:
    checks: list[Check] = []
    try:
        validate_token(settings.bot_token)
        checks.append(Check("OK", "Формат BOT_TOKEN корректен; связь с Telegram проверит /health full."))
    except TokenValidationError:
        checks.append(Check("ERROR", "Заполните BOT_TOKEN в .env токеном от @BotFather."))
    if not settings.admin_ids or any(admin_id <= 0 for admin_id in settings.admin_ids):
        checks.append(Check("ERROR", "ADMIN_IDS должен содержать положительные числовые Telegram ID."))
    else:
        checks.append(Check("OK", f"Администраторов: {len(settings.admin_ids)}."))
    if settings.channels_json and settings.default_channel_alias not in settings.channels_json:
        checks.append(Check("ERROR", "DEFAULT_CHANNEL_ALIAS отсутствует в CHANNELS_JSON."))
    elif settings.channels_json and not any(channel.get("enabled", True) for channel in settings.channels_json.values()):
        checks.append(Check("ERROR", "Все каналы в CHANNELS_JSON отключены."))
    elif settings.channels_json:
        checks.append(Check("OK", f"Каналов в конфигурации: {len(settings.channels_json)}."))
    missing = [package for module, package in WORKER_DEPENDENCIES.items() if importlib.util.find_spec(module) is None]
    if missing:
        checks.append(Check("ERROR", "Не установлены зависимости news-worker: " + ", ".join(missing)
                            + '. Выполните deploy/setup.ps1 или pip install -e ".[news-worker]".'))
    else:
        checks.append(Check("OK", "Зависимости извлечения новостей установлены."))
    saved_channels = 0
    try:
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=settings.storage_path) as probe:
            probe.write(b"local-check")
        checks.append(Check("OK", "Папка storage доступна для записи."))
        if make_url(settings.database_url).get_backend_name() == "sqlite":
            database = sqlite_path(settings.database_url)
            database.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=database.parent) as probe:
                probe.write(b"local-check")
            if database.exists():
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ValueError("SQLite: проверка целостности не пройдена")
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='channels'").fetchone():
                        saved_channels = connection.execute(
                            "SELECT COUNT(*) FROM channels WHERE is_enabled=1"
                        ).fetchone()[0]
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='news_tasks'").fetchone():
                        pending = connection.execute(
                            "SELECT COUNT(*) FROM news_tasks WHERE status IN ('queued', 'leased') AND model_name != ?",
                            (worker.model_name,),
                        ).fetchone()[0]
                        if pending and worker.llm_provider != "lmstudio":
                            checks.append(Check("WARN", f"Новостей для другой модели: {pending}. "
                                                "Завершите их прежней моделью либо отмените и создайте заново."))
                checks.append(Check("OK", "Целостность SQLite проверена."))
            else:
                checks.append(Check("OK", "SQLite будет создана при первом запуске."))
    except (OSError, sqlite3.Error, ValueError):
        checks.append(Check("ERROR", "Проверьте доступ к storage и базе данных, а также целостность SQLite."))
    if not settings.channels_json:
        if saved_channels:
            checks.append(Check("OK", f"Используются сохранённые в SQLite каналы: {saved_channels}."))
        else:
            checks.append(Check("ERROR", "Добавьте канал с chat_id в CHANNELS_JSON в .env."))
    if settings.auto_translate_titles:
        primary = "LM Studio" if worker.lmstudio_model else "LM Studio не настроен"
        fallback = "MyMemory (внешний API)" if settings.translation_fallback_mymemory else "отключён"
        checks.append(Check("OK", f"Перевод названий → английский: {primary}; резерв: {fallback}."))
    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            models = await list_model_ids(session, worker)
            checks.append(Check("OK", f"API {worker.llm_provider} доступен; моделей: {len(models)}."))
            if not worker.model_name:
                checks.append(Check("ERROR" if smoke_test else "WARN", "Модель не выбрана. "
                                    "После запуска используйте /models или python -m app.local --select-model ID."))
            elif worker.model_name not in models:
                checks.append(Check("ERROR" if smoke_test else "WARN", "Настроенная модель отсутствует в API. "
                                    "Загрузите её в LM Studio или выберите другую через /models."))
            else:
                checks.append(Check("OK", f"Модель: {worker.model_name}. Бот и worker используют один ID."))
                if smoke_test:
                    logger.info("Генерация тестового черновика в %s; Telegram не используется.", worker.llm_provider)
                    client = create_news_client(session, worker)
                    source = ExtractedNewsSource(
                        kind=NewsSourceKind.MANUAL, source_id="local-smoke-test",
                        source_url=None, normalized_url=None, title="Проверка установки",
                        raw_text="8 сентября городская библиотека открыла читальный зал на 20 мест. Вход свободный.",
                    )
                    await client.rewrite(source)
                    checks.append(Check("OK", "Модель создала черновик, соответствующий JSON Schema. Telegram не использовался."))
        except (LlmError, ValueError) as error:
            checks.append(Check("ERROR" if smoke_test else "WARN", str(error)))
            if not smoke_test:
                checks.append(Check("WARN", "Бот может работать без локальной модели. Новости дождутся её доступности; "
                                    "для названий используется настроенный резервный перевод."))
    return checks


def print_checks(checks: list[Check]) -> bool:
    for check in checks:
        print(f"[{check.level}] {SecretFilter.redact(check.message)}", flush=True)
    return not any(check.level == "ERROR" for check in checks)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Бот и локальная нейросеть: запуск, диагностика и резервная копия")
    result.add_argument("--config", type=Path, default=Path("bot-settings.toml"))
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Проверить установку без запуска Telegram")
    modes.add_argument("--smoke-test", action="store_true", help="Проверить установку и создать тестовый черновик только в памяти")
    modes.add_argument("--models", action="store_true", help="Показать ID моделей локального API")
    modes.add_argument("--select-model", metavar="ID", help="Сохранить выбор текстовой модели LM Studio")
    modes.add_argument("--backup", action="store_true", help="Сохранить согласованную копию SQLite в backups/")
    return result


async def main(args: argparse.Namespace) -> int:
    if not args.check and not args.smoke_test and not args.models and not args.backup:
        assert_legacy_entrypoint_allowed()
    if not args.config.is_file():
        raise ValueError(f"Файл конфигурации не найден: {args.config}. Запускайте из корня проекта.")
    if args.models or args.select_model:
        worker = load_worker_settings(args.config, secrets.token_urlsafe(32))
        settings = Settings(_config_file=args.config, bot_token="")
        selection = ModelSelection(settings.storage_path, worker)
        async with aiohttp.ClientSession(trust_env=False) as session:
            if args.select_model:
                if worker.llm_provider != "lmstudio":
                    raise ValueError("Для выбора модели задайте HOME_WORKER_LLM_PROVIDER=lmstudio.")
                chosen = await LMStudioModels(session, selection).select(args.select_model)
                print(f"Выбрана модель: {chosen.id}. Настройка сохранена; новые запросы используют её без перезапуска.")
                return 0
            if worker.llm_provider == "lmstudio":
                catalog = await LMStudioModels(session, selection).list()
                models = [model.id for model in catalog]
                for model in catalog:
                    status = "загружена" if model.loaded else "установлена" if model.loaded is False else "доступна"
                    print(f"{'*' if model.id == selection.selected else ' '} {model.id} [{status}]")
                print(f"Выбрана: {selection.selected or 'не выбрана'}")
                print('Выбор: python -m app.local --select-model "ID" или /models в Telegram')
            else:
                models = await list_model_ids(session, worker)
                print("\n".join(models))
        if not models:
            print("Текстовых моделей нет. Скачайте модель в LM Studio.")
        return 0 if models else 1
    if args.backup:
        # Backups must remain available even when the bot token or LLM is unconfigured.
        settings = Settings(_config_file=args.config, bot_token="")
        print(f"Копия SQLite: {backup_database(settings.database_url)}")
        return 0
    settings, worker = load_local_settings(args.config)
    configure_logging(settings.log_level)
    print("Проверка локальной установки...", flush=True)
    if not print_checks(await diagnose(settings, worker, smoke_test=args.smoke_test)):
        return 1
    if args.check or args.smoke_test:
        return 0
    with InstanceLock(settings.storage_path / ".publisher.lock"):
        application = await bootstrap(settings, home_worker_settings=worker)
        print("Запуск бота и обработчика новостей. Остановка: Ctrl+C.", flush=True)
        selection = ModelSelection(settings.storage_path, worker) if worker.llm_provider == "lmstudio" else None
        await application.run(news_worker=lambda: run_worker(worker, model_selection=selection))
    return 0


def run() -> None:
    try:
        code = asyncio.run(main(parser().parse_args()))
    except KeyboardInterrupt:
        code = 0
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False, include_context=False):
            field = ".".join(str(part) for part in item["loc"])
            print(f"[ERROR] {field}: {SecretFilter.redact(item['msg'])}", file=sys.stderr)
        code = 1
    except (ValueError, OSError, RuntimeError, sqlite3.Error, LlmError) as error:
        print(f"[ERROR] {SecretFilter.redact(str(error))}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    run()
