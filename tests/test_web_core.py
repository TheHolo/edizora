import asyncio
import io
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.application.platform import PlatformService, PlatformError
from app.db.models import User, Channel, Job, MediaAsset, JobAsset, Operation, PublicationAttempt, PublicationPart
from app.db.session import create_database, create_schema
from app.host.control import ControlStore
from app.operations.runner import OperationRunner
from app.services.job_service import JobService
from app.services.publisher_service import TelegramPublisher
from app.services.preview_service import deserialize_post
from app.domain.exceptions import UncertainPublishError
from app.domain.enums import JobStatus
from app.storage.assets import store_bytes, safe_path


@pytest.fixture
async def core(tmp_path):
    engine,sessions=create_database(f"sqlite+aiosqlite:///{tmp_path/'content.sqlite'}")
    await create_schema(engine)
    async with sessions() as session,session.begin():
        user=User(telegram_user_id=1,is_admin=True,is_active=True);session.add(user)
        channel=Channel(alias="main",telegram_chat_id="-1001",title="Основной",is_default=True,is_enabled=True,default_tags=[]);session.add(channel)
        await session.flush();actor=user.id;channel_id=channel.id
    gate=ControlStore(tmp_path/'control');gate.initialize(system_id=str(uuid4()),data_root=tmp_path/'data',role="active")
    platform=PlatformService(sessions,data_root=tmp_path/'data',gate=gate)
    yield platform,engine,sessions,actor,channel_id,gate,tmp_path
    await engine.dispose()


async def draft(core,text="Первый текст"):
    platform,_,_,actor,channel,*_=core
    result=await platform.create_publication(actor,{"content_kind":"text","body":text,"channel_id":channel},str(uuid4()))
    return await platform.get_publication(actor,result["publication_id"])


async def prepare(core,job):
    platform,_,_,actor,*_=core
    result=await platform.publication_action(actor,job["id"],"prepare",{"expected_version":job["version"]},str(uuid4()))
    runner=OperationRunner(platform)
    await runner._prepare(result["operation_id"])
    return await platform.get_publication(actor,job["id"])


async def test_draft_count_includes_other_content_after_partial_import(core):
    from app.domain.models import SourcePost

    platform,_,sessions,actor,channel,*_=core
    async with sessions() as session,session.begin():
        second_channel=Channel(alias="other",telegram_chat_id="-1002",title="Другой",is_enabled=True,default_tags=[])
        session.add(second_channel);await session.flush();second_channel_id=second_channel.id
    urls=[f"https://example.org/{index}" for index in range(3)]
    posts=[SourcePost(provider="test",source_id=str(index),source_url=url,normalized_url=url,
        title=f"Изображение {index}",author_name="",author_url="",media_items=[]) for index,url in enumerate(urls[:2])]
    runtime=SimpleNamespace(ingest=SimpleNamespace(fetch=AsyncMock(side_effect=[*posts,OSError("unavailable")])))
    accepted=await platform.create_publication(actor,{"content_kind":"artwork","channel_id":channel,"urls":urls},str(uuid4()))
    await OperationRunner(platform,runtime)._import(accepted["operation_id"])
    operation=await platform.operation(actor,accepted["operation_id"])
    assert operation["status"]=="waiting_user" and len(operation["result"]["publication_ids"])==2

    created=await platform.create_publication(actor,{"content_kind":"text","body":"Другой черновик","channel_id":second_channel_id},str(uuid4()))
    text_draft=await platform.get_publication(actor,created["publication_id"])
    page=await platform.list_publications(actor,status="waiting_confirmation",limit=1)
    assert page["total"]==3 and len(page["items"])==1 and page["next_cursor"]
    assert (await platform.list_publications(actor,status="waiting_confirmation",channel_id=channel,limit=1))["total"]==2
    assert (await platform.list_publications(actor,status="waiting_confirmation",channel_id=second_channel_id,limit=1))["total"]==1

    ready=await prepare(core,text_draft)
    await platform.publication_action(actor,ready["id"],"enqueue",{"expected_version":ready["version"]},str(uuid4()))
    assert (await platform.list_publications(actor,status="waiting_confirmation",limit=1))["total"]==2


async def test_schema_has_foreign_keys_journal_and_upgrade_is_idempotent(core):
    platform,engine,sessions,*_=core
    await create_schema(engine)
    async with sessions() as session:
        assert await session.scalar(text("PRAGMA foreign_keys"))==1
        assert (await session.execute(text("SELECT version FROM schema_migrations ORDER BY version"))).scalars().all()==[1,2,3]


async def test_unknown_schema_rejected_without_modification(tmp_path):
    path=tmp_path/'unknown.db'
    with sqlite3.connect(path) as db:db.execute("CREATE TABLE alien(id INTEGER)")
    engine,_=create_database(f"sqlite+aiosqlite:///{path}")
    with pytest.raises(RuntimeError,match="Unknown"):
        await create_schema(engine)
    await engine.dispose()
    with sqlite3.connect(path) as db:
        assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]==["alien"]
    assert list(tmp_path.glob('unknown.db.pre-web-*.bak'))


async def test_create_retries_return_same_effect_and_changed_payload_conflicts(core):
    platform,_,sessions,actor,channel,*_=core
    key=str(uuid4());payload={"content_kind":"text","text":"Текст","channel_id":channel}
    first=await platform.create_publication(actor,payload,key)
    second=await platform.create_publication(actor,payload,key)
    assert first==second
    with pytest.raises(PlatformError) as error:await platform.create_publication(actor,{**payload,"text":"Иной"},key)
    assert error.value.code=="idempotency_conflict"
    async with sessions() as session:assert len((await session.scalars(select(Job))).all())==1


async def test_concurrent_edit_has_one_winner_and_preserves_unknown_json(core):
    platform,_,sessions,actor,*_=core
    job=await draft(core)
    async with sessions() as session,session.begin():
        row=await session.get(Job,job["id"]);row.post_data={**row.post_data,"unknown_future_field":{"keep":True}}
    job=await platform.get_publication(actor,job["id"])
    result=await asyncio.gather(*(platform.patch_publication(actor,job["id"],{"expected_version":job["version"],"title":v}) for v in ["А","Б"]),return_exceptions=True)
    assert sum(isinstance(x,dict) for x in result)==1
    assert sum(isinstance(x,PlatformError) and x.code=="version_conflict" for x in result)==1
    async with sessions() as session:assert (await session.get(Job,job["id"])).post_data["unknown_future_field"]=={"keep":True}


async def test_legacy_orm_and_sql_writes_increment_versions(core):
    platform,_,sessions,actor,*_=core
    job=await draft(core)
    await platform.jobs.set_caption_override(job["id"],"Ручной текст")
    changed=await platform.get_publication(actor,job["id"])
    assert changed["version"]>job["version"]
    async with sessions() as session,session.begin():await session.execute(text("UPDATE jobs SET force_publish=1 WHERE id=:id"),{"id":job["id"]})
    assert (await platform.get_publication(actor,job["id"]))["version"]>changed["version"]


async def test_preview_is_read_only_and_safely_escapes_source_text(core):
    platform,_,sessions,actor,*_=core
    job=await draft(core,"<script>bad()</script>")
    before=await platform.get_publication(actor,job["id"])
    plan=await platform.preview(actor,job["id"])
    assert "<script>" not in plan["html"] and "&lt;script&gt;" in plan["html"]
    await platform.preview(actor,job["id"])
    assert await platform.get_publication(actor,job["id"])==before
    async with sessions() as session:assert not (await session.scalars(select(Operation))).all()


async def test_prepare_persists_plan_and_queue_is_idempotent(core):
    platform,_,_,actor,*_=core
    job=await prepare(core,await draft(core))
    assert job["readiness"]["ready"]
    key=str(uuid4());payload={"expected_version":job["version"]}
    one=await platform.publication_action(actor,job["id"],"enqueue",payload,key)
    two=await platform.publication_action(actor,job["id"],"enqueue",payload,key)
    assert one==two and one["status"]=="queued" and one["readiness"]["ready"]
    with pytest.raises(PlatformError):await platform.publication_action(actor,job["id"],"enqueue",{"expected_version":one["version"]},str(uuid4()))


async def test_force_cannot_bypass_global_pause_or_migration_gate(core):
    platform,_,_,actor,_,gate,_=core
    job=await prepare(core,await draft(core))
    await platform.publication_pause(actor,True)
    with pytest.raises(PlatformError) as error:await platform.publication_action(actor,job["id"],"publish-now",{"expected_version":job["version"]},str(uuid4()))
    assert error.value.status==503
    assert await platform.jobs.claim_next() is None
    await platform.publication_pause(actor,False)
    gate.block("migration","m1")
    with pytest.raises(PlatformError):await platform.patch_publication(actor,job["id"],{"expected_version":job["version"],"title":"Недопустимо"})
    assert await platform.jobs.claim_next() is None


async def test_reading_during_freeze_does_not_touch_content(core):
    platform,_,_,actor,_,gate,_=core
    job=await draft(core)
    gate.block("migration","migration")
    assert (await platform.get_publication(actor,job["id"]))["version"]==job["version"]
    assert await platform.validate_actor(actor)
    await platform.operations(actor)
    await platform.preview(actor,job["id"])


async def test_channel_identity_with_history_cannot_change(core):
    platform,_,_,actor,channel,*_=core
    await draft(core)
    c=await platform.get_channel(actor,channel)
    with pytest.raises(PlatformError) as error:await platform.patch_channel(actor,channel,{"expected_version":c["version"],"telegram_chat_id":"-100999"})
    assert error.value.code=="channel_has_history"


async def test_channel_edit_invalidates_readiness(core):
    platform,_,_,actor,channel,*_=core
    job=await prepare(core,await draft(core))
    c=await platform.get_channel(actor,channel)
    await platform.patch_channel(actor,channel,{"expected_version":c["version"],"default_tags":["new"]})
    assert not (await platform.get_publication(actor,job["id"]))["readiness"]["ready"]


async def test_uncertain_cannot_auto_retry_and_resolution_does_not_send(core):
    platform,_,sessions,actor,*_=core
    job=await draft(core)
    async with sessions() as session,session.begin():
        row=await session.get(Job,job["id"]);row.status="failed";row.error_code="uncertain_publish"
    job=await platform.get_publication(actor,job["id"])
    with pytest.raises(PlatformError):await platform.publication_action(actor,job["id"],"retry",{"expected_version":job["version"]},str(uuid4()))
    resolved=await platform.publication_action(actor,job["id"],"resolve-uncertain",{"expected_version":job["version"],"outcome":"not_published"},str(uuid4()))
    assert resolved["status"]=="failed"


@pytest.mark.parametrize("key",["../secret","/etc/passwd","C:/secret","\\\\server\\share","assets/../secret","assets\\secret"])
def test_asset_path_rejects_traversal(tmp_path,key):
    with pytest.raises(ValueError):safe_path(tmp_path,key)


async def test_upload_validates_content_and_album_uses_immutable_ids(core):
    platform,_,_,actor,*_=core
    image=Image.new("RGB",(32,32),(255,0,0));stream=io.BytesIO();image.save(stream,format="PNG")
    asset=await platform.upload_asset(actor,stream.getvalue(),filename="../../картинка.png")
    job=await draft(core)
    updated=await platform.patch_publication(actor,job["id"],{"expected_version":job["version"],"asset_ids":[asset["id"]]})
    assert len(updated["assets"])==1
    assert updated["assets"][0]["filename"]=="картинка.png"
    metadata=await platform.asset_metadata(actor,asset["id"])
    assert "path" not in metadata and "storage_key" not in metadata
    with pytest.raises(PlatformError):await platform.upload_asset(actor,b"<html>not an image</html>",filename="photo.png")


async def test_publisher_journals_each_send_and_preserves_partial_message_ids(core):
    platform,_,sessions,actor,channel,gate,_=core
    job=await draft(core)
    row=await platform.jobs.get(job["id"])
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
    publisher=TelegramPublisher(bot,sessions=sessions,gate=gate)
    result=await publisher.publish(row,deserialize_post(row.post_data),[],row.channel,"Текст")
    assert result.message_ids==[10] and gate.status()["active_sends"]==0
    async with sessions() as session:
        attempt=await session.scalar(select(PublicationAttempt));part=await session.scalar(select(PublicationPart))
        assert attempt.stage=="confirmed" and part.message_ids==[10] and part.status=="confirmed"


async def test_prepared_only_managed_worker_does_not_use_home_ai(core):
    platform,_,_,actor,*_=core
    job=await prepare(core,await draft(core))
    # Prepared plan remains readable without any runtime or AI connection.
    assert platform.runtime is None
    assert (await platform.get_publication(actor,job["id"]))["readiness"]["ready"]


async def test_actor_recheck_rejects_revoked_admin(core):
    platform,_,sessions,actor,*_=core
    async with sessions() as session,session.begin():(await session.get(User,actor)).is_active=False
    with pytest.raises(PlatformError) as error:await platform.list_publications(actor)
    assert error.value.status==403


async def test_durable_inbox_replays_committed_command_once_after_interruption(core):
    from app.bot.durable import DurableDispatcher,current_update
    from app.db.models import TelegramInbox
    from aiogram.types import Update
    platform,_,sessions,actor,channel,*_=core
    dispatcher=DurableDispatcher(sessions=sessions)
    update=Update.model_validate({"update_id":345,"message":{"message_id":1,"date":1700000000,"chat":{"id":10,"type":"private"},"from":{"id":1,"is_bot":False,"first_name":"Admin"},"text":"text"}})
    await dispatcher.persist_updates("123",[update,update])
    await dispatcher.persist_updates("123",[update])
    # Simulate a crash after the business result commit but before inbox completion.
    base=await draft(core)
    post=deserialize_post((await platform.jobs.get(base["id"])).post_data)
    identity=current_update.set(("123",345))
    try:first=await platform.jobs.create_preview(actor,post,channel,[],3)
    finally:current_update.reset(identity)
    async def handle(bot,received,**kwargs):
        assert current_update.get()==("123",345)
        retried=await platform.jobs.create_preview(actor,post,channel,[],3)
        assert retried.id==first.id
    dispatcher.feed_update=handle
    await dispatcher.process_inbox(SimpleNamespace(id=123))
    async with sessions() as session:
        rows=list((await session.scalars(select(TelegramInbox))).all())
        assert len(rows)==1 and rows[0].status=="processed"
        assert len((await session.scalars(select(Job))).all())==2


async def test_wizard_storage_survives_new_storage_instance(core):
    from app.bot.durable import PersistentStorage
    from aiogram.fsm.storage.base import StorageKey
    _,_,sessions,*_=core
    key=StorageKey(bot_id=1,chat_id=1,user_id=1)
    storage=PersistentStorage(sessions)
    await storage.set_state(key,"waiting_text")
    await storage.set_data(key,{"draft":"долгий текст"})
    reloaded=PersistentStorage(sessions)
    assert await reloaded.get_state(key)=="waiting_text"
    assert await reloaded.get_data(key)=={"draft":"долгий текст"}


async def test_drain_waits_for_album_collector_before_inbox_processed(core):
    from app.bot.durable import DurableDispatcher
    from app.db.models import TelegramInbox
    from aiogram import Router
    from aiogram.types import Update
    _,_,sessions,*_=core
    dispatcher=DurableDispatcher(sessions=sessions);router=Router();dispatcher.include_router(router)
    updates=[Update.model_validate({"update_id":i,"message":{"message_id":i,"media_group_id":"album1","date":1700000000,"chat":{"id":10,"type":"private"}}}) for i in [1,2]]
    await dispatcher.persist_updates("5",updates)
    completed=asyncio.Event()
    async def collector():
        await asyncio.sleep(.02);completed.set()
    router.forwarded_album_tasks={asyncio.create_task(collector())}
    dispatcher.feed_update=AsyncMock()
    await dispatcher.process_inbox(SimpleNamespace(id=5))
    assert completed.is_set()
    async with sessions() as session:assert {r.status for r in (await session.scalars(select(TelegramInbox))).all()}=={"processed"}


async def test_news_creation_uses_legacy_worker_contract_and_nullable_chat(core):
    from app.news.worker_models import NewsTaskInput
    from app.db.models import NewsTask
    platform,_,sessions,actor,channel,*_=core
    accepted=await platform.create_publication(actor,{"content_kind":"news","text":"Важная новость для переписывания","channel_id":channel},str(uuid4()))
    async with sessions() as session:
        task=await session.get(NewsTask,accepted["task_id"])
        assert task.origin_chat_id is None
        assert NewsTaskInput.model_validate(task.input_payload).kind.value=="manual"


async def test_legacy_lease_never_claims_title_translation_task(core):
    from app.services.news_task_service import NewsTaskService
    from app.db.models import NewsTask
    _,_,sessions,actor,channel,*_=core
    async with sessions() as session,session.begin():session.add(NewsTask(created_by_user_id=actor,target_channel_id=channel,origin_chat_id=None,source_kind="text",kind="title_translation",input_payload={"kind":"title_translation","title":"Title"},status="queued",user_tags=[]))
    assert await NewsTaskService(sessions).lease(worker_id="legacy",lease_seconds=60) is None


async def test_news_complete_retry_returns_one_job_and_rejects_changed_result(core):
    from app.services.news_task_service import NewsTaskService,NewsTaskLeaseError
    from app.news.models import ExtractedNewsSource,NewsSourceKind
    from app.news.worker_models import WorkerResult,NewsDraft
    _,_,sessions,actor,channel,*_=core
    service=NewsTaskService(sessions)
    task=await service.create(user_id=actor,channel_id=channel,origin_chat_id=None,source_kind="manual",input_payload={"kind":"manual","source_text":"Исходный текст"},user_tags=[],model_name=None,max_attempts=3)
    lease=await service.lease(worker_id="worker",lease_seconds=60)
    result=WorkerResult(source=ExtractedNewsSource(kind=NewsSourceKind.MANUAL,source_id="source",source_url=None,normalized_url=None,title="Title",raw_text="Original"),draft=NewsDraft(headline="Новость",body="Текст новости")).model_dump(mode="json")
    first=await service.complete(task.id,lease_token=lease.lease_token,result_payload=result)
    second=await service.complete(task.id,lease_token=lease.lease_token,result_payload=result)
    assert first.job_id==second.job_id
    changed={**result,"draft":{**result["draft"],"headline":"Изменённая новость"}}
    with pytest.raises(NewsTaskLeaseError):await service.complete(task.id,lease_token=lease.lease_token,result_payload=changed)
    async with sessions() as session:assert len((await session.scalars(select(Job))).all())==1


async def test_telegram_success_before_commit_failure_is_unknown(core):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    platform,_,sessions,actor,_,gate,_=core
    job=await draft(core);row=await platform.jobs.get(job["id"])
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)))
    publisher=TelegramPublisher(bot,sessions=sessions,gate=gate)
    def fail_confirmation(session):
        if any(isinstance(record,PublicationPart) and record.status=="confirmed" for record in session.dirty):
            raise RuntimeError("Injected crash before commit")
    event.listen(Session,"before_commit",fail_confirmation)
    try:
        with pytest.raises(UncertainPublishError):await publisher.publish(row,deserialize_post(row.post_data),[],row.channel,"Text")
    finally:event.remove(Session,"before_commit",fail_confirmation)
    bot.send_message.assert_awaited_once()
    async with sessions() as session:
        assert (await session.scalar(select(PublicationAttempt))).stage=="unknown"
        assert (await session.scalar(select(PublicationPart))).status=="unknown"
    assert gate.status()["active_sends"]==0


async def test_partial_album_preserves_confirmed_ids_and_unknown_part(core):
    from aiogram.exceptions import TelegramNetworkError
    from app.domain.models import PreparedMedia
    platform,_,sessions,actor,_,gate,tmp_path=core
    job=await draft(core);row=await platform.jobs.get(job["id"])
    post=deserialize_post(row.post_data);post.content_kind="artwork"
    bot=SimpleNamespace(send_media_group=AsyncMock(return_value=[SimpleNamespace(message_id=i) for i in range(1,11)]),send_photo=AsyncMock(side_effect=TelegramNetworkError(method=None,message="lost response")))
    publisher=TelegramPublisher(bot,sessions=sessions,gate=gate)
    media=[PreparedMedia(path=tmp_path/f'{i}.png',as_document=False,order=i) for i in range(11)]
    with pytest.raises(UncertainPublishError):await publisher.publish(row,post,media,row.channel,"Caption")
    async with sessions() as session:
        parts=(await session.scalars(select(PublicationPart).order_by(PublicationPart.sequence))).all()
        assert parts[0].message_ids==list(range(1,11)) and parts[0].status=="confirmed"
        assert parts[1].status=="unknown"


async def test_catchup_uses_success_limiter_without_changing_original_schedule(core):
    from datetime import timedelta
    from app.db.models import ApplicationState,utcnow
    from app.application.scheduling import record_catchup_success
    platform,_,sessions,actor,channel,*_=core
    jobs=[await prepare(core,await draft(core,str(i))) for i in range(2)]
    original=utcnow()-timedelta(minutes=10)
    async with sessions() as session,session.begin():
        for dto in jobs:
            job=await session.get(Job,dto["id"]);job.status="scheduled";job.scheduled_at=original;job.queue_position=None
    await platform.apply_overdue_policy()
    claimed=await platform.jobs.claim_next();assert claimed
    published=utcnow()
    async with sessions() as session,session.begin():
        await record_catchup_success(session,claimed,published)
    await platform.jobs.transition(claimed.id,JobStatus.COMPLETED)
    assert await platform.jobs.claim_next() is None
    async with sessions() as session:
        remaining=await session.get(Job,next(j["id"] for j in jobs if j["id"]!=claimed.id))
        assert remaining.scheduled_at.replace(tzinfo=published.tzinfo)==original
        assert remaining.next_attempt_at.replace(tzinfo=published.tzinfo)>=published+timedelta(seconds=60)


async def test_orm_telegram_write_cannot_overwrite_a_concurrent_web_version(core):
    from sqlalchemy.orm.exc import StaleDataError
    platform,_,sessions,actor,*_=core
    job=await draft(core)
    async with sessions() as first,sessions() as second:
        a=await first.get(Job,job["id"]);b=await second.get(Job,job["id"])
        a.post_data={**a.post_data,"title":"Winner"}
        await first.commit()
        b.caption_override="Stale edit"
        with pytest.raises(StaleDataError):await second.commit()
    current=await platform.get_publication(actor,job["id"])
    assert current["title"]=="Winner" and current["caption_override"] is None


async def test_interval_pause_change_keeps_content_plan_but_template_change_holds_queue(core):
    platform,_,_,actor,channel,*_=core
    prepared=await prepare(core,await draft(core))
    enqueued=await platform.publication_action(actor,prepared["id"],"enqueue",{"expected_version":prepared["version"]},str(uuid4()))
    c=await platform.get_channel(actor,channel)
    c=await platform.patch_channel(actor,channel,{"expected_version":c["version"],"publish_interval_seconds":60,"is_paused":True})
    assert (await platform.get_publication(actor,prepared["id"]))["readiness"]["ready"]
    c=await platform.patch_channel(actor,channel,{"expected_version":c["version"],"caption_template":"<b>{title}</b>","is_paused":False})
    assert not (await platform.get_publication(actor,prepared["id"]))["readiness"]["ready"]
    assert await platform.jobs.claim_next() is None


async def test_managed_telegram_preview_uses_durable_common_commands_once(core):
    from app.api.extras import WebPlatformService
    from app.bot.durable import current_update
    from app.services.preview_service import PreviewService
    platform,_,sessions,actor,_,gate,_=core
    managed=WebPlatformService(sessions,data_root=platform.data_root,gate=gate)
    job=await draft(core);stored=await managed.jobs.get(job["id"])
    bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=71)))
    publisher=TelegramPublisher(bot,sessions=sessions,gate=gate)
    runtime=SimpleNamespace(job_service=managed.jobs,publisher=publisher)
    managed.bind_runtime(runtime)
    translator=SimpleNamespace(enrich_title=AsyncMock())
    previews=PreviewService(downloader=None,media=None,captions=None,publisher=publisher,storage=managed.data_root,auto_add_source_tags=True,max_tags=20,max_tag_length=64,translator=translator,jobs=managed.jobs)
    runner=OperationRunner(managed,runtime);await runner.start()
    identity=current_update.set(("123",999))
    try:
        await previews.send(stored,1)
        await previews.send(stored,1)
    finally:
        current_update.reset(identity);await runner.stop()
    bot.send_message.assert_awaited_once()
    translator.enrich_title.assert_not_awaited()
    assert (await managed.get_publication(actor,job["id"]))["readiness"]["ready"]


async def test_skip_handoff_cancels_only_explicit_ids_and_holds_unconfirmed(core):
    from datetime import timedelta
    from app.db.models import utcnow
    platform,_,sessions,actor,_,gate,*_=core
    jobs=[await prepare(core,await draft(core,str(i))) for i in range(2)]
    async with sessions() as session,session.begin():
        for dto in jobs:
            job=await session.get(Job,dto["id"]);job.status="scheduled";job.scheduled_at=utcnow()-timedelta(minutes=2)
    preflight=await platform.deployment_readiness()
    assert set(preflight["overdue_job_ids"])=={j["id"] for j in jobs}
    gate.block("migration","move")
    with pytest.raises(PlatformError):await platform.prepare_handoff("move",1,"skip")
    await platform.prepare_handoff("move",1,"skip",60,[jobs[0]["id"]])
    one=await platform.get_publication(actor,jobs[0]["id"]);two=await platform.get_publication(actor,jobs[1]["id"])
    assert one["status"]=="cancelled" and two["status"]=="scheduled"
    async with sessions() as session:assert (await session.get(Job,two["id"])).post_data["metadata"]["overdue_hold"]
