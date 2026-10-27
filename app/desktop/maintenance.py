"""Offline local pre-update backup. Files remain tied to this Windows account."""
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

from app.runtime import InstanceLock


def register_bundled_release(store):
    from app.deployment.release import verify_release
    from app.deployment.snapshot import sha256
    from app.host.control import durable_json
    bundled = Path(__file__).resolve().parents[2] / 'desktop-release.zip'
    if not bundled.is_file():
        return
    manifest = verify_release(bundled)
    archive = store.root.parent / 'archives' / (manifest['release_id'] + '.zip')
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists():
        shutil.copy2(bundled, archive)
    if sha256(archive) != sha256(bundled):
        raise ValueError('Сохранённый релиз не прошёл проверку')
    durable_json(store.node_path, {**store.identity(), 'release_id': manifest['release_id'], 'schema_version': manifest['schema_version']})
    store.save('installed-release', 'current', {'archive': str(archive), 'release_id': manifest['release_id'],
        'schema_version': manifest['schema_version'], 'sha256': sha256(archive)})


def prepare_update(root: Path):
    from app.desktop.launcher import stop_host
    from app.host.control import ControlStore, durable_json
    if not (root / 'control/node.json').is_file():
        return
    try:
        lock = InstanceLock(root / 'control/host.lock')
        lock.__enter__()
    except RuntimeError:
        stop_host(root)
        deadline = time.monotonic() + 120
        while True:
            try:
                lock.__enter__()
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Приложение ещё завершает отправки. Повторите обновление позже') from None
                time.sleep(.2)
    try:
        store = ControlStore(root / 'control')
        if store.status()['active_sends']:
            raise RuntimeError('Перед обновлением требуется сверить незавершённые отправки')
        node = store.identity()
        data = Path(node['data_root']).resolve()
        if data != (root / 'data').resolve():
            raise RuntimeError('Для нестандартного размещения данных требуется управляемое обновление')
        destination = root / 'update-backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
        destination.mkdir(parents=True)
        shutil.copytree(data, destination / 'data')
        shutil.copytree(root / 'control', destination / 'control', ignore=shutil.ignore_patterns('*.lock', 'host.json', 'runtime-status*'))
        from app.deployment.snapshot import database_report
        database_report(destination / 'data' / node.get('database_key', 'database.db'))
        durable_json(destination / 'backup.json', {'format': 'publisher-local-update-backup-v1',
            'system_id': node['system_id'], 'release_id': node['release_id'], 'verified': True,
            'note': 'Local Windows account backup; use the panel for portable encrypted backups'})
    finally:
        lock.__exit__()
