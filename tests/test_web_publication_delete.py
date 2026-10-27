import io
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.api.extras import WebPlatformService
from app.application.platform import PlatformError
from app.db.models import (User, Channel, Job, JobAsset, MediaAsset, MediaRecord, JobEvent,
    NewsTask, Operation, OperationEvent, Publication, PublicationAttempt, PublicationPart)
from app.db.session import create_database, create_schema
from app.operations.runner import OperationRunner


@pytest.fixture
async def deletion(tmp_path):
    engine,sessions=create_database('sqlite+aiosqlite:///'+(tmp_path/'content.db').as_posix())
    await create_schema(engine)
    async with sessions() as session,session.begin():
        session.add_all([User(id=1,telegram_user_id=17,is_active=True,is_admin=True),
            Channel(id=1,alias='test',title='Test',telegram_chat_id='-1001',is_enabled=True)])
    platform=WebPlatformService(sessions,data_root=tmp_path)
    yield platform,sessions,tmp_path
    await engine.dispose()


async def picture(platform,color='green',asset_id=None):
    if asset_id is None:
        output=io.BytesIO();Image.new('RGB',(32,32),color).save(output,'PNG')
        asset_id=(await platform.upload_asset(1,output.getvalue(),filename='picture.png'))['id']
    result=await platform.create_publication(1,{'content_kind':'artwork','channel_id':1,'title':color,'asset_ids':[asset_id]},str(uuid4()))
    return await platform.get_publication(1,result['publication_id'])


async def test_delete_removes_database_dependencies_original_thumbnail_and_cache(deletion):
    platform,sessions,root=deletion
    dto=await picture(platform)
    prepared=await platform.prepare(1,dto['id'],'prepare',{'expected_version':dto['version']},str(uuid4()))
    await OperationRunner(platform)._prepare(prepared['operation_id'])
    async with sessions() as session,session.begin():
        job=await session.get(Job,dto['id']);job.status='completed'
        asset=await session.get(MediaAsset,dto['assets'][0]['id'])
        paths=[root/asset.storage_key,root/asset.thumbnail_key]
        session.add(Publication(job_id=job.id,channel_id=1,telegram_chat_id='-1001',telegram_message_ids=[55],caption='Caption'))
        session.add(JobEvent(job_id=job.id,event_type='completed'))
        session.add(MediaRecord(job_id=job.id,source_url='https://example.org/image',filename='image',sort_order=0))
        attempt=PublicationAttempt(id=str(uuid4()),job_id=job.id,job_version=job.version,node_id='test',generation=1,render_plan=job.render_plan,stage='completed')
        session.add(attempt);await session.flush()
        session.add(PublicationPart(attempt_id=attempt.id,sequence=1,request_digest='abc',status='sent',message_ids=[55]))
        session.add(NewsTask(created_by_user_id=1,target_channel_id=1,source_kind='manual',input_payload={},job_id=job.id,status='completed'))
    cache=root/'jobs'/str(dto['id'])/'download.png';cache.parent.mkdir(parents=True);cache.write_bytes(b'cache');paths.append(cache)
    dto=await platform.get_publication(1,dto['id'])
    body={'expected_version':dto['version']};key=str(uuid4())
    result=await platform.delete_publication(1,dto['id'],body,key)
    assert result['deleted'] and not result['cleanup_pending']
    assert await platform.delete_publication(1,dto['id'],body,key)==result
    assert all(not path.exists() for path in paths)
    async with sessions() as session:
        for model in (Job,JobAsset,MediaAsset,MediaRecord,JobEvent,NewsTask,Publication,PublicationAttempt,PublicationPart):
            assert (await session.scalars(select(model))).all()==[]
        assert all(op.kind=='storage_cleanup' and op.job_id is None for op in (await session.scalars(select(Operation))).all())
        assert (await session.execute(text('PRAGMA foreign_key_check'))).all()==[]
    with pytest.raises(PlatformError) as missing:await platform.get_publication(1,dto['id'])
    assert missing.value.status==404
    new=await picture(platform,'blue')
    assert new['id']>dto['id']  # Stale editor URLs cannot refer to a different new publication.


@pytest.mark.parametrize('shared_asset',[True,False])
async def test_shared_assets_or_identical_files_survive_until_last_publication(deletion,shared_asset):
    platform,sessions,root=deletion
    first=await picture(platform)
    second=await picture(platform,asset_id=first['assets'][0]['id'] if shared_asset else None)
    async with sessions() as session:
        asset=await session.get(MediaAsset,first['assets'][0]['id'])
        paths=[root/asset.storage_key,root/asset.thumbnail_key]
    await platform.delete_publication(1,first['id'],{'expected_version':first['version']},str(uuid4()))
    assert all(path.exists() for path in paths)
    assert (await platform.get_publication(1,second['id']))['assets'][0]['thumbnail_url']
    await platform.delete_publication(1,second['id'],{'expected_version':second['version']},str(uuid4()))
    assert all(not path.exists() for path in paths)


async def test_delete_cleans_prepared_variants_but_never_external_paths(deletion):
    platform,sessions,root=deletion
    dto=await picture(platform)
    external=root/'unrelated.png';external.write_bytes(b'user original')
    from app.storage.assets import store_bytes
    output=io.BytesIO();Image.new('RGB',(16,16),'red').save(output,'PNG')
    stored=store_bytes(root,output.getvalue())
    async with sessions() as session,session.begin():
        variant=MediaAsset(**stored,source_kind='prepared',source_reference=dto['assets'][0]['id'])
        session.add(variant)
        session.add(MediaRecord(job_id=dto['id'],source_url='',filename='original',local_path=str(external),sort_order=0))
    await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    assert not (root/stored['storage_key']).exists()
    assert not (root/stored['thumbnail_key']).exists()
    assert external.read_bytes()==b'user original'


async def test_delete_rejects_conflicts_active_work_and_maintenance(deletion):
    platform,sessions,root=deletion
    dto=await picture(platform)
    with pytest.raises(PlatformError) as conflict:
        await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']+1},str(uuid4()))
    assert conflict.value.code=='version_conflict'
    async with sessions() as session,session.begin():
        op=platform._new_operation(session,1,'prepare',{'job_version':dto['version']},'Busy',status='running',job_id=dto['id'])
    with pytest.raises(PlatformError) as busy:
        await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    assert busy.value.code=='publication_busy'
    async with sessions() as session,session.begin():
        operation=await session.get(Operation,op.id);operation.status='queued'
    # Queued background preparation can be removed atomically before the runner claims it.
    await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    await OperationRunner(platform)._queue_preparations()
    assert (await platform.list_publications(1))['total']==0


async def test_locked_file_cleanup_is_durable_and_retryable(deletion,monkeypatch):
    platform,sessions,root=deletion
    dto=await picture(platform)
    async with sessions() as session:
        asset=await session.get(MediaAsset,dto['assets'][0]['id']);target=root/asset.storage_key
    unlink=Path.unlink
    def locked(path,*args,**kwargs):
        if path==target:raise PermissionError('locked')
        return unlink(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'unlink',locked)
        result=await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    assert result['cleanup_pending'] and target.exists()
    async with sessions() as session,session.begin():
        op=await session.get(Operation,result['cleanup_operation_id'])
        assert op.status=='queued' and op.payload['keys']==[asset.storage_key]
        op.payload={**op.payload,'retry_at':0}
    import asyncio
    runner=OperationRunner(platform)
    try:
        await runner.start()
        async with asyncio.timeout(8):
            while (await platform.operation(1,result['cleanup_operation_id']))['status']!='succeeded':
                await asyncio.sleep(.05)
        assert not target.exists()
    finally:await runner.stop()


async def test_bulk_delete_reports_success_and_stale_selection_individually(deletion):
    platform,sessions,root=deletion
    first=await picture(platform);second=await picture(platform,'blue')
    result=await platform.bulk(1,{'action':'delete','items':[{'id':first['id'],'expected_version':first['version']},
        {'id':second['id'],'expected_version':second['version']+1}],'expected_selection_count':2},str(uuid4()))
    assert [item['status'] for item in result['items']]==['success','conflict']
    assert (await platform.list_publications(1))['total']==1


async def test_delete_api_requires_auth_csrf_version_and_supports_replay(deletion):
    from fastapi.testclient import TestClient
    from app.api.auth import AuthStore
    from app.api.server import WebConfig,create_app
    from app.api.rpc import METHODS
    platform,sessions,root=deletion
    assert 'delete_publication' in METHODS
    dto=await picture(platform)
    auth=AuthStore(root/'control');auth.provision(1,17,'a strong password 123')
    app=create_app(platform,auth,config=WebConfig(origin='http://localhost',admin_ids={17}))
    with TestClient(app,base_url='http://localhost') as client:
        url=f"/api/v1/publications/{dto['id']}"
        body={'expected_version':dto['version']}
        assert client.request('DELETE',url,json=body).status_code==401
        login=client.post('/api/v1/auth/password',json={'telegram_id':17,'password':'a strong password 123'},headers={'Origin':'http://localhost'})
        headers={'Origin':'http://localhost','Idempotency-Key':str(uuid4())}
        assert client.request('DELETE',url,json=body,headers=headers).status_code==403
        headers['X-CSRF-Token']=login.json()['csrf_token']
        assert client.request('DELETE',url,json={},headers=headers).status_code==422
        result=client.request('DELETE',url,json=body,headers=headers)
        assert result.status_code==200,result.text
        assert client.request('DELETE',url,json=body,headers=headers).json()==result.json()
        assert client.get(url).status_code==404


async def test_shared_original_keeps_variants_while_another_draft_is_preparing(deletion):
    platform,sessions,root=deletion
    first=await picture(platform)
    second=await picture(platform,asset_id=first['assets'][0]['id'])
    from app.storage.assets import store_bytes
    output=io.BytesIO();Image.new('RGB',(16,16),'red').save(output,'PNG')
    stored=store_bytes(root,output.getvalue())
    async with sessions() as session,session.begin():
        # An in-progress preparation has stored its variant but not yet committed its manifest.
        session.add(MediaAsset(**stored,source_kind='prepared',source_reference=first['assets'][0]['id']))
        platform._new_operation(session,1,'prepare',{},'Preparing another draft',status='running',job_id=second['id'])
    await platform.delete_publication(1,first['id'],{'expected_version':first['version']},str(uuid4()))
    assert (root/stored['storage_key']).exists()
    async with sessions() as session:assert await session.get(MediaAsset,stored['id'])


async def test_runtime_download_cache_is_removed_from_its_configured_storage(deletion):
    from types import SimpleNamespace
    platform,sessions,root=deletion
    dto=await picture(platform)
    storage=root/'runtime-storage'
    platform.runtime=SimpleNamespace(workers=SimpleNamespace(downloader=SimpleNamespace(storage=storage)))
    async with sessions() as session,session.begin():
        op=platform._new_operation(session,1,'prepare',{},'Done',status='succeeded',job_id=dto['id'])
    directory=storage/'jobs'/f'prepare-{op.id}'
    directory.mkdir(parents=True)
    cache=directory/'download.png';cache.write_bytes(b'cached image')
    unrelated=storage/'jobs'/'another-job'/'image.png';unrelated.parent.mkdir(parents=True);unrelated.write_bytes(b'keep')
    result=await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    assert not result['cleanup_pending'] and not cache.exists()
    assert unrelated.read_bytes()==b'keep'


async def test_delete_preserves_other_members_of_import_batch(deletion):
    platform,sessions,root=deletion
    first=await picture(platform);second=await picture(platform,'blue')
    async with sessions() as session,session.begin():
        op=platform._new_operation(session,1,'import',{'urls':['one','two']},'Imported',status='succeeded',job_id=second['id'])
        op.result={'items':{'0':{'status':'succeeded','job_id':first['id']},'1':{'status':'succeeded','job_id':second['id']}},
            'publication_ids':[first['id'],second['id']]}
    await platform.delete_publication(1,second['id'],{'expected_version':second['version']},str(uuid4()))
    async with sessions() as session:
        retained=await session.get(Operation,op.id)
        assert retained.job_id is None
        assert retained.result['items']['1']=={'status':'deleted'}
        assert retained.result['publication_ids']==[first['id']]
        assert (await session.execute(text('PRAGMA foreign_key_check'))).all()==[]
    assert (await platform.get_publication(1,first['id']))['id']==first['id']


async def test_database_failure_rolls_back_without_removing_files(deletion,monkeypatch):
    platform,sessions,root=deletion
    dto=await picture(platform)
    async with sessions() as session:
        asset=await session.get(MediaAsset,dto['assets'][0]['id'])
        paths=[root/asset.storage_key,root/asset.thumbnail_key]
    def fail(*args,**kwargs):raise RuntimeError('Simulated database failure')
    with monkeypatch.context() as patch:
        patch.setattr(platform,'_save_receipt',fail)
        with pytest.raises(RuntimeError,match='Simulated database failure'):
            await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    assert (await platform.get_publication(1,dto['id']))['id']==dto['id']
    assert all(path.exists() for path in paths)
    async with sessions() as session:
        assert (await session.execute(text('PRAGMA foreign_key_check'))).all()==[]


async def test_replaying_old_create_after_deletion_never_recreates_the_record(deletion):
    platform,sessions,root=deletion
    key=str(uuid4());body={'content_kind':'text','channel_id':1,'body':'Deleted text'}
    created=await platform.create_publication(1,body,key)
    dto=await platform.get_publication(1,created['publication_id'])
    await platform.delete_publication(1,dto['id'],{'expected_version':dto['version']},str(uuid4()))
    with pytest.raises(PlatformError) as deleted:
        await platform.create_publication(1,body,key)
    assert deleted.value.status==410 and deleted.value.code=='publication_deleted'
    assert (await platform.list_publications(1))['total']==0
