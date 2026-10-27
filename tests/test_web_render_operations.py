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


async def test_background_preparation_downloads_imported_draft_and_refreshes_edits(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.domain.models import SourcePost, MediaItem, DownloadedMedia
    from app.services.job_service import serialize_post
    from app.db.models import Job

    engine,sessions,platform=await setup_platform(tmp_path)
    image=tmp_path/'source.png'
    Image.new('RGB',(12,12),'green').save(image)
    item=MediaItem(url='https://example.org/image.png',filename='image.png',order=0)
    post=SourcePost(provider='pixiv',source_id='123',source_url='https://example.org/post',
        normalized_url='https://example.org/post',title='Artwork',author_name='Artist',author_url='',
        media_items=[item,MediaItem(url='https://example.org/second.png',filename='second.png',order=1)])
    downloader=AsyncMock(return_value=DownloadedMedia(source=item,path=image,mime_type='image/png',size=image.stat().st_size))
    runtime=SimpleNamespace(workers=SimpleNamespace(downloader=SimpleNamespace(download=downloader)))
    # A persisted draft from before startup also gets prepared without a browser command.
    async with sessions() as session,session.begin():
        job=Job(created_by_user_id=1,target_channel_id=1,provider=post.provider,source_id=post.source_id,
            source_url=post.source_url,normalized_url=post.normalized_url,content_kind='artwork',
            status='waiting_confirmation',post_data=serialize_post(post),user_tags=[],source_tags=[],max_attempts=3)
        session.add(job);await session.flush();job_id=job.id
    runner=OperationRunner(platform,runtime)
    async with sessions() as session,session.begin():
        await runner._catalog(session,await session.get(Job,job_id))
    initial=await platform.get_publication(1,job_id)
    first_id,second_id=[asset['id'] for asset in initial['assets']]
    await platform.patch_publication(1,job_id,{'expected_version':initial['version'],'selected_asset_ids':[first_id]})
    async def ready():
        async with asyncio.timeout(8):
            while True:
                dto=await platform.get_publication(1,job_id)
                if dto['readiness']['ready']:return dto
                await asyncio.sleep(.05)
    try:
        await runner.start()
        prepared=await ready()
        assert prepared['assets'][0]['state']=='available'
        assert prepared['assets'][0]['thumbnail_url']
        downloader.assert_awaited_once()
        changed=await platform.patch_publication(1,job_id,{'expected_version':prepared['version'],'caption_override':'Edited caption'})
        assert not changed['readiness']['ready']
        refreshed=await ready()
        assert refreshed['version']>=changed['version']
        assert (await platform.preview(1,job_id))['html']=='Edited caption'
        downloader.assert_awaited_once()  # Existing images are reused for new revisions.
        assert refreshed['status']=='waiting_confirmation'  # Never enqueue or send automatically.
        await platform.patch_publication(1,job_id,{'expected_version':refreshed['version'],'selected_asset_ids':[second_id]})
        selected=await ready()
        assert [asset['id'] for asset in selected['assets'] if asset['selected']]==[second_id]
        assert all(asset['thumbnail_url'] for asset in selected['assets'])
        assert downloader.await_count==2  # Newly selected images also load automatically.
    finally:
        await runner.stop();await engine.dispose()


async def test_automatic_preparation_deduplicates_failures_and_retries_new_revisions(tmp_path):
    from sqlalchemy import select
    engine,sessions,platform=await setup_platform(tmp_path)
    try:
        created=await platform.create_publication(1,{'content_kind':'text','channel_id':1,'body':'Draft'},str(uuid4()))
        runner=OperationRunner(platform)
        await runner._queue_preparations();await runner._queue_preparations()
        async with sessions() as session,session.begin():
            operations=(await session.scalars(select(Operation).where(Operation.kind=='prepare'))).all()
            assert len(operations)==1
            operations[0].status='failed'
        await runner._queue_preparations()
        async with sessions() as session:
            assert len((await session.scalars(select(Operation).where(Operation.kind=='prepare'))).all())==1
        dto=await platform.get_publication(1,created['publication_id'])
        await platform.patch_publication(1,dto['id'],{'expected_version':dto['version'],'body':'New revision'})
        await runner._queue_preparations()
        async with sessions() as session:
            operations=(await session.scalars(select(Operation).where(Operation.kind=='prepare').order_by(Operation.created_at))).all()
            assert len(operations)==2
            assert operations[0].payload['job_version']!=operations[1].payload['job_version']
        await runner._prepare(operations[1].id)
        assert (await platform.get_publication(1,dto['id']))['readiness']['ready']
    finally:await engine.dispose()


async def test_import_automatically_downloads_and_exposes_retry_after_source_failure(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.domain.models import SourcePost, MediaItem, DownloadedMedia

    engine,sessions,platform=await setup_platform(tmp_path)
    image=tmp_path/'source.png'
    Image.new('RGB',(12,12),'blue').save(image)
    item=MediaItem(url='https://example.org/image.png',filename='image.png',order=0)
    post=SourcePost(provider='pixiv',source_id='123',source_url='https://example.org/post',
        normalized_url='https://example.org/post',title='Artwork',author_name='Artist',author_url='',media_items=[item])
    downloader=AsyncMock(side_effect=[OSError('Source unavailable'),
        DownloadedMedia(source=item,path=image,mime_type='image/png',size=image.stat().st_size)])
    runtime=SimpleNamespace(ingest=SimpleNamespace(fetch=AsyncMock(return_value=post)),
        workers=SimpleNamespace(downloader=SimpleNamespace(download=downloader)))
    created=await platform.create_publication(1,{'content_kind':'artwork','channel_id':1,'source_url':post.source_url},str(uuid4()))
    runner=OperationRunner(platform,runtime)
    try:
        await runner.start()
        async with asyncio.timeout(8):
            while True:
                async with sessions() as session:
                    failed=await session.scalar(select(Operation).where(Operation.kind=='prepare',Operation.status=='failed'))
                if failed:break
                await asyncio.sleep(.05)
        imported=await platform.operation(1,created['operation_id'])
        assert imported['status']=='succeeded'
        job_id=imported['result']['publication_id']
        dto=await platform.get_publication(1,job_id)
        assert dto['last_operation_id']==failed.id
        assert not dto['readiness']['ready']
        await runner._queue_preparations()
        downloader.assert_awaited_once()  # No endless background retry of an unavailable source.
        retry=await platform.prepare(1,job_id,'prepare',{'expected_version':dto['version']},str(uuid4()))
        async with asyncio.timeout(8):
            while True:
                ready=(await platform.get_publication(1,job_id))['readiness']['ready']
                finished=(await platform.operation(1,retry['operation_id']))['status']=='succeeded'
                if ready and finished:break
                await asyncio.sleep(.05)
        assert (await platform.operation(1,retry['operation_id']))['status']=='succeeded'
        assert downloader.await_count==2
        assert (await platform.list_publications(1))['total']==1
    finally:
        await runner.stop();await engine.dispose()


async def test_channel_edit_during_download_automatically_rebuilds_current_plan(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    import pytest
    from app.db.models import Job
    from app.domain.models import SourcePost, MediaItem, DownloadedMedia
    from app.services.job_service import serialize_post

    engine,sessions,platform=await setup_platform(tmp_path)
    image=tmp_path/'source.png'
    Image.new('RGB',(12,12),'blue').save(image)
    item=MediaItem(url='https://example.org/image.png',filename='image.png',order=0)
    post=SourcePost(provider='pixiv',source_id='123',source_url='https://example.org/post',
        normalized_url='https://example.org/post',title='Artwork',author_name='Artist',author_url='',media_items=[item])
    async def download(*args):
        channel=await platform.get_channel(1,1)
        await platform.patch_channel(1,1,{'expected_version':channel['version'],'publish_mode':'document'})
        return DownloadedMedia(source=item,path=image,mime_type='image/png',size=image.stat().st_size)
    downloader=AsyncMock(side_effect=download)
    runner=OperationRunner(platform,SimpleNamespace(workers=SimpleNamespace(downloader=SimpleNamespace(download=downloader))))
    try:
        async with sessions() as session,session.begin():
            job=Job(created_by_user_id=1,target_channel_id=1,provider=post.provider,source_id=post.source_id,
                source_url=post.source_url,normalized_url=post.normalized_url,content_kind='artwork',
                status='waiting_confirmation',post_data=serialize_post(post),user_tags=[],source_tags=[],max_attempts=3)
            session.add(job);await session.flush();job_id=job.id
        await runner._queue_preparations()
        async with sessions() as session:
            original=await session.scalar(select(Operation).where(Operation.kind=='prepare'))
        with pytest.raises(ValueError,match='stale_job'):
            await runner._prepare(original.id)
        assert not (await platform.get_publication(1,job_id))['readiness']['ready']
        await runner._queue_preparations()
        async with sessions() as session:
            updated=await session.scalar(select(Operation).where(Operation.kind=='prepare',Operation.id!=original.id))
        assert updated is not None
        await runner._prepare(updated.id)
        plan=await platform.preview(1,job_id)
        assert plan['parts'][0]['method']=='sendDocument'
        assert (await platform.get_publication(1,job_id))['readiness']['ready']
        downloader.assert_awaited_once()
    finally:await engine.dispose()
