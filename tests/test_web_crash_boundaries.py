"""Hard interruption around durable migration writes, bypassing error handlers."""
import uuid
import pytest

from app.deployment.controller import DeploymentService, LocalNodeTarget, STAGES
from app.host.control import GateClosed
from test_web_deployment import node, content, Hooks, finish


class SimulatedProcessExit(BaseException):
    pass


@pytest.mark.parametrize('boundary', STAGES)
@pytest.mark.parametrize('moment', ['before', 'after'])
async def test_restart_before_and_after_each_checkpoint_commit(tmp_path, monkeypatch, boundary, moment):
    system=str(uuid.uuid4())
    source,data=node(tmp_path/'source',system)
    target,_=node(tmp_path/'target',system,'standby')
    database=content(data)
    service=DeploymentService(source,data,database,Hooks())
    endpoint=LocalNodeTarget(target);service.register_target(endpoint)
    plan=await service.create_plan({'target_node_id':target.identity()['node_id']})
    fired=False
    original_save=source.save
    original_accept=source.accept_command

    def interrupt_write(original, kind, identifier, payload, **kwargs):
        nonlocal fired
        matches=kind=='migration' and payload.get('stage_key')==boundary and not fired
        if matches and moment=='before':
            fired=True;raise SimulatedProcessExit()
        result=original(kind,identifier,payload,**kwargs)
        if matches and moment=='after':
            fired=True;raise SimulatedProcessExit()
        return result

    def save(kind,identifier,payload):
        return interrupt_write(original_save,kind,identifier,payload)

    def accept(kind,key,digest,record,**kwargs):
        nonlocal fired
        if kind=='migration' and boundary=='stopping' and not fired:
            if moment=='before':fired=True;raise SimulatedProcessExit()
            result=original_accept(kind,key,digest,record,**kwargs)
            fired=True;raise SimulatedProcessExit()
        return original_accept(kind,key,digest,record,**kwargs)

    monkeypatch.setattr(source,'save',save)
    monkeypatch.setattr(source,'accept_command',accept)
    with pytest.raises(SimulatedProcessExit):
        record=await service.start_migration(plan['id'],1,True,'crash-boundary')
        await finish(service,record['id'])
    assert fired
    monkeypatch.setattr(source,'save',original_save)
    monkeypatch.setattr(source,'accept_command',original_accept)
    assert sum(store.status()['publication_allowed'] for store in (source,target))<=1
    persisted=source.list('migration')
    restarted=DeploymentService(source,data,database,Hooks())
    restarted.register_target(endpoint)
    restarted.recover_pending()
    if not persisted:
        # The stopping intent never committed: starting again is a new acceptance.
        assert boundary=='stopping' and moment=='before'
        record=await restarted.start_migration(plan['id'],1,True,'crash-boundary')
    else:
        record=restarted.migration(persisted[0]['id'])
        if record['status']=='succeeded':
            assert source.identity()['role']=='retired' and target.identity()['role']=='active'
            return
        assert not source.status()['publication_allowed']
        if record['activation_committed']:
            with pytest.raises(GateClosed):await restarted.resume_source(record['id'])
            await restarted.resolve(record['id'])
        else:await restarted.resume(record['id'])
    completed=await finish(restarted,record['id'])
    assert completed['status']=='succeeded',completed
    assert source.identity()['role']=='retired' and target.identity()['role']=='active'
    assert target.status()['publication_allowed']
    if persisted and persisted[0].get('grant'):
        assert completed['grant']==persisted[0]['grant']
