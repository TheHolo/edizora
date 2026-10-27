import io
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models import User, Channel, Job, MediaRecord, ApplicationState, JobAsset, MediaAsset
from app.db.session import create_database, create_schema
from app.host.control import ControlStore
from app.host.import_legacy import import_legacy
from app.runtime import InstanceLock


@pytest.fixture
def local_tmp():
    with tempfile.TemporaryDirectory(prefix='publisher-legacy-test-', dir=os.environ.get('PUBLISHER_TEST_TMP_ROOT')) as directory:
        yield Path(directory)


@pytest.fixture
async def legacy(local_tmp):
    tmp_path = local_tmp
    checkout = tmp_path / 'old'; (checkout / 'app').mkdir(parents=True)
    (checkout / 'app/runtime.py').write_text('def managed_checkout_marker(): pass', encoding='utf-8')
    storage = checkout / 'storage'; storage.mkdir()
    image = storage / 'оригинал.png'; payload = io.BytesIO(); Image.new('RGB', (8,8), 'blue').save(payload, 'PNG'); image.write_bytes(payload.getvalue())
    database = checkout / 'data' / 'database.db'; database.parent.mkdir()
    engine, sessions = create_database('sqlite+aiosqlite:///' + database.as_posix()); await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(id=7, telegram_user_id=42, is_admin=True, is_active=True)
        channel = Channel(id=9, alias='test', telegram_chat_id='-1001', title='Test', default_tags=[])
        session.add_all([user, channel]); await session.flush()
        for identifier, status in ((11,'queued'), (12,'publishing')):
            session.add(Job(id=identifier, created_by_user_id=7, target_channel_id=9, provider='manual', source_id=str(identifier), source_url='https://example.org/'+str(identifier), normalized_url='https://example.org/'+str(identifier), status=status, queue_position=identifier, post_data={'media_items':[{'url':'https://example.org/image.png','filename':'image.png','media_type':'image','order':0}]}))
        await session.flush()
        session.add(MediaRecord(job_id=11, source_url='https://example.org/image.png', local_path=str(image), filename='image.png', sort_order=0))
    await engine.dispose()
    return dict(checkout=checkout, storage=storage, database=database, data=tmp_path/'new-data', control=tmp_path/'new-control', admin_id=42, confirmed_stopped=True)


async def test_offline_import_preserves_source_ids_queue_and_pins_originals(legacy):
    before=legacy['database'].read_bytes()
    result=await import_legacy(**legacy)
    assert result['status']=='imported_paused'
    assert legacy['database'].read_bytes()==before
    assert (legacy['checkout']/'.publisher-managed.json').is_file()
    store=ControlStore(legacy['control']); assert store.identity()['role']=='active'
    assert [b['reason'] for b in store.status()['blocks']]==['user_pause']
    engine,sessions=create_database('sqlite+aiosqlite:///'+(legacy['data']/'database.db').as_posix())
    try:
        async with sessions() as session:
            job=await session.get(Job,11); assert job.status=='queued' and job.queue_position==11
            unknown=await session.get(Job,12); assert unknown.status=='failed' and unknown.error_code=='uncertain_publish'
            state=await session.get(ApplicationState,1); assert state.system_id==result['system_id']
            asset=await session.scalar(select(MediaAsset).join(JobAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==11))
            assert asset.state=='available' and (legacy['data']/asset.storage_key).is_file()
            assert (await session.get(User,7)).telegram_user_id==42
    finally: await engine.dispose()
    with pytest.raises(ValueError,match='уже принят'): await import_legacy(**legacy)


@pytest.mark.parametrize('case',['not_confirmed','active_sender','managed_copy','used_target','existing_data','wrong_admin','wrong_storage','overlapping_control'])
async def test_import_refuses_unsafe_source_or_target(legacy,case):
    if case=='not_confirmed': legacy['confirmed_stopped']=False
    if case=='managed_copy':
        with closing(sqlite3.connect(legacy['database'])) as db, db:
            db.execute("INSERT INTO application_state(id,system_id,version,settings,publication_blocks,maintenance_state,updated_at) VALUES(1,?,1,'{}','{}','normal','2026-10-27')",(str(uuid4()),))
    if case=='used_target':
        store=ControlStore(legacy['control']); store.initialize(system_id=str(uuid4()),data_root=legacy['data'],role='active')
    if case=='existing_data': legacy['data'].mkdir(); (legacy['data']/'existing').write_text('keep')
    if case=='wrong_admin': legacy['admin_id']=999
    if case=='wrong_storage': legacy['storage']=legacy['checkout']/'another-storage'; legacy['storage'].mkdir()
    if case=='overlapping_control': legacy['control']=legacy['storage']/'control'
    before=legacy['database'].read_bytes()
    if case=='active_sender':
        with InstanceLock(legacy['storage']/'.publisher.lock'):
            with pytest.raises(RuntimeError): await import_legacy(**legacy)
    else:
        with pytest.raises(ValueError): await import_legacy(**legacy)
    assert legacy['database'].read_bytes()==before
    assert not (legacy['checkout']/'.publisher-managed.json').exists()


async def test_import_into_installer_standby_retains_system_and_release(legacy):
    store=ControlStore(legacy['control']); node=store.initialize(system_id=str(uuid4()),data_root=legacy['data'],role='standby',release_id='fixture-release')
    result=await import_legacy(**legacy)
    assert result['system_id']==node['system_id']
    assert store.identity()['release_id']=='fixture-release'
