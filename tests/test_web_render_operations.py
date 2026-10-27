import hashlib
import io
from uuid import uuid4

from PIL import Image

from app.api.extras import WebPlatformService
from app.db.models import User, Channel, Operation
from app.db.session import create_database, create_schema
from app.operations.runner import OperationRunner
from app.utils.safe_html import safe_telegram_html


def test_render_html_disallows_active_source_markup_and_urls():
    value=safe_telegram_html('<b>Название</b><a href="javascript:alert(1)" onclick="bad()">ссылка</a><script>alert(2)</script>')
    assert '<b>Название</b>' in value
    assert 'javascript:' not in value and 'onclick=' not in value
    assert '<script>' not in value and '&lt;script&gt;' in value
    assert safe_telegram_html('<a href="https://example.org/a?a=1&amp;b=2">Источник</a>')=='<a href="https://example.org/a?a=1&amp;b=2">Источник</a>'


async def setup_platform(tmp_path):
    engine,sessions=create_database('sqlite+aiosqlite:///'+(tmp_path/'content.db').as_posix())
    await create_schema(engine)
    async with sessions() as session,session.begin():
        session.add_all([User(id=1,telegram_user_id=17,is_active=True,is_admin=True),Channel(id=1,alias='test',title='Тест',telegram_chat_id='-1001',is_enabled=True)])
    return engine,sessions,WebPlatformService(sessions,data_root=tmp_path)


async def test_upload_create_prepare_preserves_original_and_actual_document_plan(tmp_path):
    engine,sessions,platform=await setup_platform(tmp_path)
    try:
        picture=Image.new('RGB',(11000,2),'#315058')
        buffer=io.BytesIO();picture.save(buffer,'PNG');payload=buffer.getvalue()
        uploaded=await platform.upload_asset(1,payload,filename='широкое-изображение.png')
        created=await platform.create_publication(1,{'content_kind':'artwork','channel_id':1,'title':'Панорама','asset_ids':[uploaded['id']]},str(uuid4()))
        job=await platform.get_publication(1,created['publication_id'])
        assert len(job['assets'])==job['album']['selected']==1
        assert job['readiness']['ready'] is False
        accepted=await platform.prepare(1,job['id'],'prepare',{'expected_version':job['version']},str(uuid4()))
        await OperationRunner(platform)._prepare(accepted['operation_id'])
        plan=await platform.preview(1,job['id'])
        assert plan['assets_prepared'] is True
        assert plan['media'][0]['as_document'] is True
        assert plan['parts'][0]['method']=='sendDocument'
        assert hashlib.sha256((tmp_path/plan['media'][0]['storage_key']).read_bytes()).digest()==hashlib.sha256(payload).digest()
        prepared=await platform.get_publication(1,job['id'])
        assert prepared['readiness']['ready']
        async with sessions() as session:
            operation=await session.get(Operation,accepted['operation_id'])
            assert operation.result['job_version']==prepared['version']
        queued=await platform.publication_action(1,job['id'],'enqueue',{'expected_version':prepared['version']},str(uuid4()))
        changed=await platform.patch_publication(1,job['id'],{'expected_version':queued['version'],'caption_override':'Новая подпись панорамы'})
        updated=await platform.preview(1,job['id'])
        assert changed['readiness']['ready'] and changed['version']==queued['version']+1
        assert updated['media']==plan['media']
        assert updated['parts'][0]['method']=='sendDocument'
        assert updated['parts'][0]['caption_html']=='Новая подпись панорамы'
        assert changed['queue_position']==queued['queue_position']
    finally:await engine.dispose()


async def test_translation_proposal_apply_is_atomic_and_repeatable(tmp_path):
    engine,sessions,platform=await setup_platform(tmp_path)
    try:
        created=await platform.create_publication(1,{'content_kind':'text','channel_id':1,'title':'Новое название','body':'Материал'},str(uuid4()))
        job=await platform.get_publication(1,created['publication_id'])
        identifier=str(uuid4())
        async with sessions() as session,session.begin():
            session.add(Operation(id=identifier,actor_id=1,job_id=job['id'],kind='title_translation',status='waiting_user',stage_key='complete',payload={},result={'proposal':{'translated_text':'New title','source_language':'ru'},'applied':False}))
        command_key=str(uuid4());body={'expected_version':job['version']}
        first=await platform.apply_translation_proposal(1,identifier,body,command_key)
        again=await platform.apply_translation_proposal(1,identifier,body,command_key)
        assert again==first
        assert first['operation']['status']=='succeeded'
        assert first['operation']['result']['applied'] is True
        assert first['publication']['title_translation']=='New title'
        assert first['publication']['version']>job['version']
    finally:await engine.dispose()


async def test_interrupted_personal_preview_never_automatically_replays(tmp_path):
    engine,sessions,platform=await setup_platform(tmp_path)
    try:
        identifier=str(uuid4())
        async with sessions() as session,session.begin():
            session.add(Operation(id=identifier,actor_id=1,kind='telegram_preview',status='running',stage_key='sending',payload={}))
        runner=OperationRunner(platform)
        await runner.start();await runner.stop()
        operation=await platform.operation(1,identifier)
        assert operation['status']=='waiting_user'
        assert operation['error']['code']=='uncertain_publish'
        assert operation['allowed_actions']==[]
    finally:await engine.dispose()
