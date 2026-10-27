from typing import Annotated

from fastapi import Depends, Header

from app.api.auth import Principal,AuthError
from app.api.models import MutationInput
from app.api.server import invoke


def add_deployment_routes(app, deployment, principal, auth, key):
    Actor = Annotated[Principal, Depends(principal)]

    @app.get('/api/v1/deployment/current')
    async def current(actor: Actor):
        return await invoke(deployment, 'current')

    @app.get('/api/v1/deployment/nodes')
    async def nodes(actor: Actor):
        return await invoke(deployment, 'nodes')

    @app.post('/api/v1/deployment/profiles', status_code=201)
    async def create_profile(body: MutationInput, actor: Actor):
        auth.require_fresh(actor)
        return await invoke(deployment, 'create_profile', body.model_dump())

    @app.post('/api/v1/deployment/profiles/{profile_id}/check')
    async def check_profile(profile_id: str, actor: Actor):
        auth.require_fresh(actor)
        return await invoke(deployment, 'check_profile', profile_id)

    @app.post('/api/v1/deployment/profiles/{profile_id}/credentials')
    async def profile_credentials(profile_id: str,body: MutationInput,actor: Actor):
        try:auth.require_fresh(actor)
        except AuthError:
            profile=deployment.store.load('profile',profile_id)
            continued=False
            if profile:
                for record in deployment.store.list('migration'):
                    if record.get('status') not in {'succeeded','cancelled'} and record.get('plan',{}).get('target_node_id')==profile.get('node_id'):
                        try:auth.require_operation_action(actor,record['id'],'credentials')
                        except AuthError:continue
                        continued=True;break
            if not continued:raise
        return await invoke(deployment,'set_profile_credentials',profile_id,body.model_dump())

    @app.post('/api/v1/deployment/profiles/{profile_id}/trust')
    async def trust_profile(profile_id: str,body: MutationInput,actor: Actor):
        auth.require_fresh(actor)
        return await invoke(deployment,'trust_profile',profile_id,body.model_dump().get('confirmed_fingerprint'))

    @app.post('/api/v1/deployment/plans', status_code=201)
    async def plan(body: MutationInput, actor: Actor):
        auth.require_fresh(actor)
        return await invoke(deployment, 'create_plan', body.model_dump())

    @app.post('/api/v1/deployment/migrations', status_code=202)
    async def migrate(body: MutationInput, actor: Actor, idempotency_key: str | None = Header(None)):
        auth.require_fresh(actor)
        data = body.model_dump()
        result=await invoke(deployment, 'start_migration', data.get('plan_id'),
                            data.get('revision', data.get('plan_revision')),
                            data.get('accepted_pause', data.get('accept_publication_stop', False)), key(idempotency_key),data.get('confirmed_skip_job_ids'))
        auth.grant_operation(actor,result['id'],['resume','cancel','resume-source','resolve','credentials'])
        return result

    @app.get('/api/v1/deployment/migrations/{migration_id}')
    async def migration(migration_id: str, actor: Actor):
        return await invoke(deployment, 'migration', migration_id)

    def control(action):
        async def command(migration_id: str, actor: Actor):
            auth.require_operation_action(actor,migration_id,action)
            return await invoke(deployment, action.replace('-', '_'), migration_id)
        return command

    for action in ('resume', 'cancel', 'resume-source', 'resolve'):
        app.add_api_route('/api/v1/deployment/migrations/{migration_id}/' + action,
                         control(action), methods=['POST'])

    @app.get('/api/v1/backups')
    async def backups(actor: Actor):
        return await invoke(deployment, 'list_backups')

    @app.get('/api/v1/backups/settings')
    async def backup_settings(actor: Actor):
        return await invoke(deployment,'backup_settings')

    @app.patch('/api/v1/backups/settings')
    async def configure_backups(body: MutationInput,actor: Actor):
        auth.require_fresh(actor)
        return await invoke(deployment,'configure_backups',body.model_dump())

    @app.post('/api/v1/backups/{backup_id}/{action}')
    async def backup_action(backup_id: str,action: str,actor: Actor,body: MutationInput | None = None):
        if action=='verify':
            auth.require_fresh(actor)
            return await invoke(deployment,'verify_backup',backup_id)
        auth.require_operation_action(actor,backup_id,'resume' if action=='retry' else action)
        if action not in {'retry','resume','resume-source','cancel'}:
            from fastapi import HTTPException
            raise HTTPException(404)
        return await invoke(deployment,'backup_action',backup_id,'resume' if action=='retry' else action,(body.model_dump() if body else {}).get('recovery_passphrase'))

    @app.post('/api/v1/backups', status_code=202)
    async def backup(actor: Actor, body: MutationInput | None = None, idempotency_key: str | None = Header(None)):
        auth.require_fresh(actor)
        result=await invoke(deployment, 'start_backup', key(idempotency_key), (body.model_dump() if body else {}).get('recovery_passphrase'))
        auth.grant_operation(actor,result['id'],['resume','cancel'])
        return result

    @app.post('/api/v1/backups/{backup_id}/verify')
    async def verify(backup_id: str, actor: Actor):
        return await invoke(deployment, 'verify_backup', backup_id)

    @app.post('/api/v1/restore/plans', status_code=201)
    async def restore_plan(body: MutationInput, actor: Actor):
        auth.require_fresh(actor)
        return await invoke(deployment, 'restore_plan', body.model_dump().get('backup_id'),body.model_dump().get('recovery_passphrase'))

    @app.post('/api/v1/restore/execute', status_code=202)
    async def restore(body: MutationInput, actor: Actor):
        auth.require_fresh(actor)
        data = body.model_dump()
        result=await invoke(deployment, 'execute_restore', data.get('plan_id'), data.get('confirmation'),data.get('recovery_passphrase'))
        auth.grant_operation(actor,result['id'],['resume','resolve'])
        return result

    @app.post('/api/v1/restore/{restore_id}/resolve')
    async def resolve_restore(restore_id: str,body: MutationInput,actor: Actor):
        auth.require_operation_action(actor,restore_id,'resolve')
        return await invoke(deployment,'resolve_restore',restore_id,body.model_dump().get('acknowledge_duplicate_risk',False))

    @app.post('/api/v1/restore/{restore_id}/resume')
    async def resume_restore(restore_id: str,body: MutationInput,actor: Actor):
        auth.require_operation_action(actor,restore_id,'resume')
        return await invoke(deployment,'resume_restore',restore_id,body.model_dump().get('recovery_passphrase'))
