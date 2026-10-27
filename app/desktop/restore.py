"""Explicit offline recovery into an empty installation, using verified snapshots."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


async def restore_initial(store, auth, data, body):
    from app.db.migrations import SCHEMA_VERSION
    from app.deployment.snapshot import copy_verified, safe_path, verify_snapshot
    from app.deployment.controller import DeploymentService
    from app.host.secrets import SecretStore
    from app.host.control import durable_json
    if not body.confirmed_stopped:
        raise ValueError('Подтвердите остановку прежних экземпляров программы перед восстановлением')
    source = Path(body.backup).resolve()
    manifest = verify_snapshot(source)
    if manifest['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Копия создана другой версией схемы. Для восстановления нужен совместимый выпуск Publisher')
    with closing(sqlite3.connect(safe_path(source, manifest['database_key']).as_uri() + '?mode=ro', uri=True)) as db:
        user = db.execute('SELECT id FROM users WHERE telegram_user_id=? AND is_active=1 AND is_admin=1', (body.admin_id,)).fetchone()
    if not user:
        raise ValueError('В резервной копии нет указанного действующего администратора')
    envelope_path = source / 'secrets-envelope.json'
    secrets = SecretStore(store.root)
    envelope = json.loads(envelope_path.read_text(encoding='utf-8')) if envelope_path.exists() else None
    context = {'system_id': manifest['system_id'], 'backup_id': manifest.get('backup_id')}
    if envelope:
        if not body.recovery_password:
            raise ValueError('Введите пароль восстановления резервной копии')
        try:
            secrets.validate_backup(envelope, body.recovery_password, context=context)
        except Exception:
            raise ValueError('Не удалось открыть секреты копии. Проверьте пароль восстановления') from None
    copy_verified(source, data)
    identity = store.initialize(system_id=manifest['system_id'], data_root=data, role='active',
        schema_version=SCHEMA_VERSION, release_id=manifest['release_id'])
    identifier = str(uuid4())
    store.block('user_pause', 'user')
    store.block('recovery', identifier)
    durable_json(store.node_path, {**identity, 'database_key': manifest['database_key'], 'restored_snapshot': manifest['database_sha256']})
    if envelope:
        secrets.import_backup(envelope, body.recovery_password, context=context)
    auth.provision(user[0], body.admin_id, body.password)

    class OfflineRuntime:
        async def assert_quiesced(self):
            return  # No host or runtime has been started for this staged installation.

        async def resume(self):
            return

    deployment = DeploymentService(store, data, safe_path(data, manifest['database_key']), runtime_hooks=OfflineRuntime())
    store.save('restore', identifier, {'id': identifier, 'kind': 'restore', 'status': 'waiting_user',
        'stage_key': 'review_queue', 'plan': {'backup_id': manifest.get('backup_id')}, 'allowed_actions': ['resolve']})
    # The form explicitly authorizes converting the restored queue to drafts.
    await deployment.resolve_restore(identifier, True)
