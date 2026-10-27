"""Explicit offline cutover of an unmanaged checkout; never adopts a managed copy."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tomllib
from uuid import uuid4

from sqlalchemy import select

from app.db.models import ApplicationState, User, Job, JobEvent, MediaAsset, JobAsset, MediaRecord
from app.db.session import create_database, create_schema
from app.db.migrations import SCHEMA_VERSION
from app.deployment.snapshot import database_report, sha256
from app.host.control import ControlStore, durable_json
from app.runtime import InstanceLock


def _legacy_report(database: Path, admin_id: int):
    report = database_report(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        tables = set(report['tables'])
        if not {'users', 'channels', 'jobs'} <= tables:
            raise ValueError('Это не база старого Edizora')
        for table in ('application_state', 'operations', 'publication_attempts', 'telegram_inbox'):
            if table in tables and db.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone():
                raise ValueError('Управляемый набор нельзя активировать как legacy; используйте перенос или восстановление')
        if not db.execute('SELECT 1 FROM users WHERE telegram_user_id=? AND is_admin=1 AND is_active=1', (admin_id,)).fetchone():
            raise ValueError('В исходной базе нет указанного действующего администратора')
    return report


async def import_legacy(*, checkout: Path, database: Path, storage: Path, data: Path,
                        control: Path, admin_id: int, confirmed_stopped: bool):
    if not confirmed_stopped:
        raise ValueError('Сначала остановите старый бот и его автозапуск; требуется --confirm-stopped')
    checkout, database, storage, data, control = (p.resolve() for p in (checkout, database, storage, data, control))
    if not database.is_file() or not storage.is_dir():
        raise ValueError('Укажите существующие legacy SQLite и storage')
    guard = checkout / 'app' / 'runtime.py'
    if not guard.is_file() or 'managed_checkout_marker' not in guard.read_text(encoding='utf-8'):
        raise ValueError('Исходный checkout должен содержать защиту managed launcher из этого релиза')
    if data == control or data.is_relative_to(control) or control.is_relative_to(data):
        raise ValueError('Control и data должны быть отдельными каталогами')
    if data.is_relative_to(storage) or storage.is_relative_to(data) or database.is_relative_to(data):
        raise ValueError('Назначение должно быть отдельным от исходных данных')
    if control.is_relative_to(storage) or storage.is_relative_to(control):
        raise ValueError('Control не должен пересекаться с исходным storage')
    from app.config import Settings
    from sqlalchemy.engine import make_url
    configured_source = Settings(_config_file=checkout / 'bot-settings.toml', _env_file=checkout / '.env', bot_token='offline-import-validation')
    configured_storage = configured_source.storage_path
    if not configured_storage.is_absolute(): configured_storage = checkout / configured_storage
    url = make_url(configured_source.database_url)
    configured_database = Path(url.database or '')
    if not configured_database.is_absolute(): configured_database = checkout / configured_database
    if (configured_storage.resolve() != storage or url.get_backend_name() != 'sqlite' or configured_database.resolve() != database):
        raise ValueError('Указанные SQLite/storage не совпадают с конфигурацией исходного launcher; нельзя доказать блокировку отправителя')
    if any('onedrive' == part.casefold() for part in data.parts) or str(data).startswith('\\\\'):
        raise ValueError('Для активной SQLite нужен локальный каталог вне OneDrive')
    marker = checkout / '.publisher-managed.json'
    if marker.exists():
        raise ValueError('Checkout уже принят под управление; повторная активация запрещена')
    store = ControlStore(control)
    with InstanceLock(control / 'host.lock'), InstanceLock(storage / '.publisher.lock'):
        if data.exists() and any(data.iterdir()):
            raise ValueError('Назначение data должно быть пустым')
        identity = store.identity()
        if store.node_path.exists():
            if identity['role'] != 'standby' or identity['generation'] != 1 or Path(identity['data_root']).resolve() != data:
                raise ValueError('Назначение должно быть новой пассивной установкой')
        with store.connect() as db:
            unsafe = db.execute("SELECT 1 FROM documents WHERE kind != 'installed-release' LIMIT 1").fetchone()
            unsafe = unsafe or db.execute('SELECT 1 FROM control_events LIMIT 1').fetchone()
        if store.retired_path.exists() or store.status()['blocks'] or store.status()['active_sends'] or unsafe:
            raise ValueError('У назначения есть история управления; используйте штатный перенос')
        before = _legacy_report(database, admin_id)
        settings_file = checkout / 'bot-settings.toml'
        saved_settings = {'timezone': 'Asia/Vladivostok', 'config_seeded': True}
        if settings_file.is_file():
            configured = tomllib.loads(settings_file.read_text(encoding='utf-8')).get('bot', {})
            for key in ('timezone', 'auto_add_source_tags', 'auto_translate_titles', 'max_tags', 'max_tag_length', 'pixiv_media_limit_enabled', 'pixiv_max_images'):
                if key in configured: saved_settings[key] = configured[key]
        # Block every supported old entrypoint before releasing its OS lock.
        # On failure the marker deliberately remains: recovery must be explicit.
        durable_json(marker, {'format': 'publisher-managed-checkout-v1', 'control_dir': str(control), 'state': 'importing'})
        store.block('legacy_import', 'legacy-import')
        data.mkdir(parents=True, exist_ok=True)
        target = data / 'database.db'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
        if database_report(target)['tables'] != before['tables']:
            raise ValueError('Снимок SQLite не совпадает с исходным набором')
        for item in storage.rglob('*'):
            if item.is_symlink():
                raise ValueError('Символические ссылки в legacy storage запрещены')
            if item.is_file() and item.name not in {'.publisher.lock', 'lmstudio-model.json'}:
                relative = item.relative_to(storage)
                if relative.parts[0] in {'database.db', 'database.db-wal', 'database.db-shm'}:
                    raise ValueError('Файл storage конфликтует с SQLite назначения')
                destination = data / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
                if sha256(item) != sha256(destination):
                    raise ValueError('Не совпадает скопированный файл storage')
        engine, sessions = create_database('sqlite+aiosqlite:///' + target.as_posix())
        try:
            await create_schema(engine)
            if not store.node_path.exists():
                release_file = Path(__file__).resolve().parents[2] / 'release-manifest.json'
                release = json.loads(release_file.read_text())['release_id'] if release_file.exists() else 'development'
                identity = store.initialize(system_id=str(uuid4()), data_root=data, role='standby', release_id=release, schema_version=SCHEMA_VERSION)
            async with sessions() as session, session.begin():
                actor = await session.scalar(select(User).where(User.telegram_user_id == admin_id))
                session.add(ApplicationState(id=1, system_id=identity['system_id'], settings=saved_settings, publication_blocks={'user_pause': 'user'}, maintenance_state='normal'))
                # An interrupted legacy send has no durable per-call journal.
                # Preserve uncertainty rather than allowing startup recovery to retry.
                jobs = (await session.scalars(select(Job))).all()
                from app.operations.runner import OperationRunner
                from app.application.platform import PlatformService
                runner = OperationRunner(PlatformService(sessions, data_root=data))
                for job in jobs:
                    await runner._catalog(session, job)
                    if job.status == 'publishing':
                        job.status = 'failed'; job.error_code = 'uncertain_publish'
                        session.add(JobEvent(job_id=job.id, event_type='legacy_import_unknown', new_status='failed', message='Незавершённая старая отправка требует ручной сверки канала'))
                    session.add(JobEvent(job_id=job.id, event_type='legacy_import', new_status=job.status, message='Принято из остановленной legacy установки; публикации на паузе'))
                await session.flush()
                records = (await session.scalars(select(MediaRecord))).all()
                # Pin known originals in portable storage without rewriting legacy
                # path fields or the source DB. Unavailable media stays explicit.
                from app.storage.assets import store_bytes
                for record in records:
                    if not record.local_path:
                        continue
                    original = Path(record.local_path)
                    if not original.is_absolute(): original = checkout / original
                    original = original.resolve()
                    if not original.is_relative_to(storage) or not original.is_file():
                        continue
                    assets = (await session.scalars(select(MediaAsset).join(JobAsset, JobAsset.asset_id == MediaAsset.id).where(JobAsset.job_id == record.job_id, MediaAsset.source_reference == record.source_url))).all()
                    for asset in assets:
                        if asset.state == 'available': continue
                        try: stored = store_bytes(data, original.read_bytes(), require_image=record.media_type in {'image', 'animation'})
                        except ValueError: continue
                        for key, value in stored.items():
                            if key != 'id': setattr(asset, key, value)
                actor_id = actor.id
            store.block('user_pause', 'user')
            durable_json(store.node_path, {**identity, 'role': 'active', 'database_key': 'database.db', 'schema_version': SCHEMA_VERSION})
            result = {'status': 'imported_paused', 'system_id': identity['system_id'], 'admin_user_id': actor_id,
                      'database_before': before, 'database_after': database_report(target), 'source_unchanged': True}
            store.save('legacy-import', 'initial', result)
            store.unblock('legacy_import', 'legacy-import')
            durable_json(marker, {'format': 'publisher-managed-checkout-v1', 'control_dir': str(control), 'state': 'imported'})
            return result
        finally:
            await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description='Однократное принятие остановленной legacy установки; исходная SQLite не изменяется')
    for name in ('checkout', 'database', 'storage', 'data', 'control'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--admin-id', type=int, required=True)
    parser.add_argument('--confirm-stopped', action='store_true')
    args = parser.parse_args()
    result = asyncio.run(import_legacy(checkout=args.checkout, database=args.database, storage=args.storage,
        data=args.data, control=args.control, admin_id=args.admin_id, confirmed_stopped=args.confirm_stopped))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
