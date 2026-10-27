import subprocess
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select,event
from sqlalchemy.orm import Session

from app.api.extras import WebPlatformService
from app.db.models import (User,Channel,Job,Publication,PublicationAttempt,PublicationPart,
    Operation,JobEvent)
from app.db.session import create_database,create_schema
from app.host.control import ControlStore
from app.operations.recovery import recover_orphaned_permits
from app.runtime import InstanceLock


@pytest.fixture
async def interrupted(tmp_path):
    engine,sessions=create_database(f"sqlite+aiosqlite:///{tmp_path/'content.sqlite'}");await create_schema(engine)
    async with sessions() as session,session.begin():
        user=User(telegram_user_id=1);channel=Channel(alias='main',title='Main',telegram_chat_id='-1001')
        session.add_all([user,channel]);await session.flush()
        job=Job(created_by_user_id=user.id,target_channel_id=channel.id,provider='manual',source_id='original',source_url='',normalized_url='',status='publishing',post_data={'title':'Original'},user_tags=[],source_tags=[])
        session.add(job);await session.flush();channel.active_job_id=job.id
        attempt=PublicationAttempt(id=str(uuid4()),job_id=job.id,job_version=job.version,node_id='old-node',generation=1,stage='sending',render_plan={'private_path':'DO-NOT-EXPOSE'})
        session.add(attempt);await session.flush()
        session.add(PublicationPart(attempt_id=attempt.id,sequence=1,request_digest='a'*64,status='confirmed',message_ids=[11,12]))
        session.add(PublicationPart(attempt_id=attempt.id,sequence=2,request_digest='b'*64,status='unknown',message_ids=[]))
        actor,channel_id,job_id,attempt_id=user.id,channel.id,job.id,attempt.id
    store=ControlStore(tmp_path/'control');store.initialize(system_id=str(uuid4()),data_root=tmp_path/'data',role='active')
    permit=store.acquire_permit(job_id,1,channel_id)
    yield sessions,store,actor,channel_id,job_id,attempt_id,permit
    await engine.dispose()


async def test_orphan_recovery_requires_real_os_lock_and_old_process_to_be_dead(interrupted):
    sessions,store,actor,channel,job_id,attempt,permit=interrupted
    lock_path=store.root/'runtime.lock'
    script="import sys\nfrom pathlib import Path\nfrom app.runtime import InstanceLock\nwith InstanceLock(Path(sys.argv[1])):\n print('LOCKED',flush=True)\n sys.stdin.read()\n"
    child=subprocess.Popen([sys.executable,'-c',script,str(lock_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='LOCKED'
        assert child.poll() is None
        with pytest.raises(RuntimeError):
            with InstanceLock(lock_path):pass
        with pytest.raises(RuntimeError,match='acquired runtime OS lock'):
            await recover_orphaned_permits(sessions,store,runtime_lock=InstanceLock(lock_path))
        assert store.status()['active_sends']==1
        child.terminate();child.wait(timeout=10)
        assert child.poll() is not None
        # Windows may signal process exit just before its final file handle closes.
        lock=None
        for _ in range(100):
            candidate=InstanceLock(lock_path)
            try:lock=candidate.__enter__();break
            except RuntimeError:await asyncio.sleep(.02)
        assert lock is not None,'The actual OS lock must be acquired, not inferred from a role flag'
        try:result=await recover_orphaned_permits(sessions,store,runtime_lock=lock)
        finally:lock.__exit__()
        assert result['uncertain_job_ids']==[job_id] and store.status()['active_sends']==0
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=10)
        for stream in (child.stdin,child.stdout,child.stderr):
            if stream:stream.close()
    async with sessions() as session:
        job=await session.get(Job,job_id)
        assert job.status=='failed' and job.error_code=='uncertain_publish'
        assert (await session.get(Channel,channel)).active_job_id is None
        parts=(await session.scalars(select(PublicationPart).order_by(PublicationPart.sequence))).all()
        assert parts[0].message_ids==[11,12] and parts[0].status=='confirmed' and parts[1].status=='unknown'


async def test_content_commit_failure_keeps_permit_and_retry_is_safe(interrupted):
    sessions,store,actor,channel,job_id,attempt,permit=interrupted
    def fail_commit(session):
        raise RuntimeError('Injected content failure')
    event.listen(Session,'before_commit',fail_commit)
    try:
        with InstanceLock(store.root/'runtime.lock') as lock:
            with pytest.raises(RuntimeError,match='Injected content failure'):
                await recover_orphaned_permits(sessions,store,runtime_lock=lock)
    finally:event.remove(Session,'before_commit',fail_commit)
    assert store.status()['active_sends']==1
    async with sessions() as session:assert (await session.get(Job,job_id)).status=='publishing'
    with InstanceLock(store.root/'runtime.lock') as lock:await recover_orphaned_permits(sessions,store,runtime_lock=lock)
    assert store.status()['active_sends']==0


async def test_control_cleanup_failure_retries_after_content_commit_without_reverting_uncertainty(interrupted,monkeypatch):
    sessions,store,actor,channel,job_id,attempt,permit=interrupted
    release=store.release_permit
    def fail_release(value):raise RuntimeError('Injected control failure')
    monkeypatch.setattr(store,'release_permit',fail_release)
    with InstanceLock(store.root/'runtime.lock') as lock:
        with pytest.raises(RuntimeError,match='Injected control failure'):await recover_orphaned_permits(sessions,store,runtime_lock=lock)
    async with sessions() as session:
        job=await session.get(Job,job_id);version=job.version
        assert job.error_code=='uncertain_publish'
    assert store.status()['active_sends']==1
    monkeypatch.setattr(store,'release_permit',release)
    with InstanceLock(store.root/'runtime.lock') as lock:
        await recover_orphaned_permits(sessions,store,runtime_lock=lock)
        await recover_orphaned_permits(sessions,store,runtime_lock=lock)
    async with sessions() as session:
        assert (await session.get(Job,job_id)).version==version
        assert len((await session.scalars(select(JobEvent).where(JobEvent.event_type=='runtime_recovery_unknown'))).all())==1
    assert store.status()['active_sends']==0


async def test_persisted_publication_with_confirmed_parts_proves_completion(interrupted):
    sessions,store,actor,channel,job_id,attempt,permit=interrupted
    async with sessions() as session,session.begin():
        part=await session.scalar(select(PublicationPart).where(PublicationPart.sequence==2));part.status='confirmed';part.message_ids=[13]
        session.add(Publication(job_id=job_id,channel_id=channel,telegram_chat_id='-1001',telegram_message_ids=[11,12,13],caption='Caption'))
    with InstanceLock(store.root/'runtime.lock') as lock:
        result=await recover_orphaned_permits(sessions,store,runtime_lock=lock)
    assert result['proven_completed_job_ids']==[job_id] and not result['uncertain_job_ids']
    async with sessions() as session:
        assert (await session.get(Job,job_id)).status=='completed'
        assert (await session.get(PublicationAttempt,attempt)).stage=='completed'


async def test_personal_preview_orphan_is_waiting_user_without_automatic_retry(interrupted):
    sessions,store,actor,channel,job_id,attempt,permit=interrupted
    op_id=str(uuid4())
    async with sessions() as session,session.begin():
        session.add(Operation(id=op_id,kind='telegram_preview',actor_id=actor,job_id=job_id,status='running',stage_key='sending',payload={},last_event_seq=3))
    store.acquire_permit(0,0)
    with InstanceLock(store.root/'runtime.lock') as lock:
        result=await recover_orphaned_permits(sessions,store,runtime_lock=lock)
    assert result['uncertain_preview_operation_ids']==[op_id]
    service=WebPlatformService(sessions,data_root=store.root.parent/'data',gate=store)
    operation=await service.operation(actor,op_id)
    assert operation['status']=='waiting_user' and operation['allowed_actions']==[]
    assert operation['last_event_seq']==4 and store.status()['active_sends']==0


async def test_history_exposes_known_parts_without_render_paths_or_raw_errors(interrupted):
    sessions,store,actor,channel,job_id,attempt,permit=interrupted
    async with sessions() as session,session.begin():(await session.get(PublicationAttempt,attempt)).error='SECRET-TOKEN-DO-NOT-EXPOSE'
    service=WebPlatformService(sessions,data_root=store.root.parent/'data',gate=store)
    history=await service.history(actor,job_id)
    assert history['attempts'][0]['known_message_ids']==[11,12]
    assert history['attempts'][0]['has_unknown_parts']
    import json
    assert 'DO-NOT-EXPOSE' not in json.dumps(history) and 'private_path' not in json.dumps(history)
