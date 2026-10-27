"""Authenticated loopback-only host/runtime channel. Tokens never cross the public API."""
import base64
import inspect

import httpx

from app.application.platform import PlatformError


METHODS = frozenset('validate_actor user_for_telegram ping list_publications get_publication create_publication patch_publication publication_action list_channels get_channel create_channel patch_channel archive_channel check_channel channel_statistics queue calendar reorder_queue bulk settings patch_settings operations operation operation_events operation_action apply_translation_proposal preview album put_album prepare apply_reimport config_import_plan config_import_apply history audit asset_metadata asset_content upload_asset ai_workers ai_models ai_tasks pair_worker revoke_worker ai_selection ai_task_action system_status publication_pause worker_command legacy_worker_command deployment_readiness prepare_handoff apply_overdue_policy'.split())


def encode(value):
    if isinstance(value, bytes):
        return {'__rpc_bytes__': base64.b64encode(value).decode()}
    if isinstance(value, (list, tuple)):
        return [encode(x) for x in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {'__rpc_bytes__'}:
            return base64.b64decode(value['__rpc_bytes__'], validate=True)
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(x) for x in value]
    return value


class RuntimeClient:
    def __init__(self, origin, token):
        self.origin, self.token = origin, token

    async def call(self, method, *args, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                response = await client.post(self.origin + '/rpc', headers={'Authorization': 'Bearer ' + self.token},
                    json={'method': method, 'args': encode(args), 'kwargs': encode(kwargs)})
            value = response.json()
        except (httpx.HTTPError, ValueError):
            raise PlatformError(503, 'runtime_unavailable', 'Нет связи с приложением. Управляющая панель продолжает работать.') from None
        if response.status_code != 200:
            error = value.get('error', {})
            raise PlatformError(response.status_code, error.get('code', 'runtime_error'),
                                error.get('message', 'Приложение отклонило команду'),
                                current_version=error.get('current_version'), current=error.get('current'))
        return decode(value)

    def __getattr__(self, name):
        if name not in METHODS:
            raise AttributeError(name)
        async def remote(*args, **kwargs):
            return await self.call(name, *args, **kwargs)
        return remote

    async def close_and_drain(self, migration_id):
        # Drain may take as long as the current Telegram call; never kill it to satisfy a timeout.
        return await self.call('_drain', migration_id)

    async def accept_maintenance(self,migration_id):
        return await self.call('_maintenance',migration_id)

    async def assert_quiesced(self):
        result = await self.call('_quiesced')
        if not result.get('quiesced'):
            raise PlatformError(503, 'runtime_not_quiesced', 'Приложение ещё завершает принятые операции')

    async def stop(self):
        # The frozen reader stays available for status/auth; all executors have stopped.
        return await self.assert_quiesced()
