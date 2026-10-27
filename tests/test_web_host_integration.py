import asyncio
import os
import secrets
import socket
from uuid import uuid4

from app.db.models import User
from app.db.session import create_database, create_schema
from app.db.migrations import SCHEMA_VERSION
from app.deployment.controller import DeploymentService, LocalNodeTarget
from app.host.control import ControlStore
from app.host.runtime_manager import RuntimeManager
from app.api.auth import AuthStore,AuthError
import pytest


async def test_real_runtime_processes_stop_swap_and_resume_after_handoff(tmp_path):
    """Real loopback RPC + separate processes; no Telegram, AI, SSH or external hosts."""
    system_id=str(uuid4())
    managers=[]
    monitors=[]
    stores=[]
    for index,role in enumerate(('active','standby')):
        root=tmp_path / str(index); data=root / 'data'; data.mkdir(parents=True)
        database=data / ('database.db' if role=='active' else 'publisher.sqlite')
        engine,sessions=create_database('sqlite+aiosqlite:///' + database.as_posix())
        if role=='active':
            await create_schema(engine)
            async with sessions() as session,session.begin():
                session.add(User(id=1,telegram_user_id=17,is_active=True,is_admin=True))
        await engine.dispose()
        store=ControlStore(root / 'control')
        store.initialize(system_id=system_id,data_root=data,role=role,release_id='integration',schema_version=SCHEMA_VERSION)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        token=secrets.token_urlsafe(40)
        env={**os.environ,'PUBLISHER_CONTROL_DIR':str(store.root),'PUBLISHER_DATA_ROOT':str(data),
             'PUBLISHER_DATABASE_PATH':str(database),'PUBLISHER_RUNTIME_TOKEN':token,'PUBLISHER_RUNTIME_PORT':str(port),'PUBLISHER_ENABLE_BOT':'0'}
        manager=RuntimeManager(f'http://127.0.0.1:{port}',token,store,env)
        managers.append(manager);stores.append(store)
    try:
        for manager in managers:
            await manager.start()
            monitors.append(asyncio.create_task(manager.consume_commands()))
        source,target=stores
        assert not (tmp_path/'1/data/database.db').exists()
        source_auth=AuthStore(source.root)
        source_auth.provision(1,17,'isolated-admin-password-123')
        original_session=source_auth.password_login(17,'isolated-admin-password-123','test')
        # Actual content is committed through the source's authenticated runtime.
        channel=await managers[0].create_channel(1,{'alias':'test','title':'Тест','telegram_chat_id':'-100123'},str(uuid4()))
        await managers[0].patch_channel(1,channel['id'],{'expected_version':channel['version'],'is_enabled':True})
        created=await managers[0].create_publication(1,{'content_kind':'text','channel_id':channel['id'],'title':'Актуальные данные','text':'Принятый материал'},str(uuid4()))
        deployment=DeploymentService(source,tmp_path / '0/data',tmp_path / '0/data/database.db',runtime_hooks=managers[0])
        deployment.register_target(LocalNodeTarget(target))
        plan=await deployment.create_plan({'target_node_id':target.identity()['node_id'],'overdue_policy':'catch_up'})
        migration=await deployment.start_migration(plan['id'],plan['revision'],True,str(uuid4()))
        await asyncio.wait_for(deployment.tasks[migration['id']],timeout=40)
        final=deployment.migration(migration['id'])
        assert final['status']=='succeeded',final
        assert source.identity()['role']=='retired'
        assert target.identity()['role']=='active'
        assert managers[0].process.poll() is not None
        for _ in range(100):
            try:
                listing=await managers[1].list_publications(1)
                if listing['total']==1:break
            except Exception:
                pass
            await asyncio.sleep(.1)
        else:raise AssertionError('Target runtime did not reopen the transferred current database')
        assert listing['items'][0]['title']=='Актуальные данные'
        assert listing['items'][0]['id']==created['publication_id']
        assert target.status()['publication_allowed']
        target_auth=AuthStore(target.root)
        imported_session=target_auth.password_login(17,'isolated-admin-password-123','test')
        assert target_auth.authenticate(imported_session['token']).user_id==1
        with pytest.raises(AuthError):target_auth.authenticate(original_session['token'])
    finally:
        for monitor in monitors:monitor.cancel()
        await asyncio.gather(*monitors,return_exceptions=True)
        for manager in managers:
            await manager.close_and_drain('test-cleanup')
            await manager.stop()
