import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.extras import WebPlatformService
from app.application.platform import PlatformError
from app.db.models import User,Channel,NewsTask,AIWorker,Job,Operation
from app.db.session import create_database,create_schema
from app.host.control import ControlStore
from app.worker_v1 import WorkerApi,RegisteredWorker,trusted_origin
from app.news.worker_config import HomeWorkerSettings


@pytest.fixture
async def worker_context(tmp_path):
    engine,sessions=create_database(f"sqlite+aiosqlite:///{tmp_path/'content.sqlite'}");await create_schema(engine)
    async with sessions() as session,session.begin():
        user=User(telegram_user_id=1);channel=Channel(alias="main",title="Main",telegram_chat_id="-1001",is_default=True)
        session.add_all([user,channel]);await session.flush();actor,channel_id=user.id,channel.id
    gate=ControlStore(tmp_path/'control');gate.initialize(system_id=str(uuid4()),data_root=tmp_path/'data',role="active")
    service=WebPlatformService(sessions,data_root=tmp_path/'data',gate=gate)
    code=await service.pair_worker(actor,{})
    registered=await service.worker_command("register",{"pairing_code":code["pairing_code"],"name":"home","provider":"lmstudio"})
    credential=registered["credential"]
    await service.worker_command("capabilities",{"models":[{"id":"text-old","type":"text"},{"id":"text-new","type":"text"},{"id":"embed-unsafe","type":"embedding"}],"capabilities":{"task_kinds":["news","title_translation"],"source_types":["manual"]}},credential)
    async with sessions() as session,session.begin():(await session.get(AIWorker,registered["worker_id"])).selected_model="text-old"
    yield service,sessions,actor,channel_id,gate,credential,registered
    await engine.dispose()


async def title_task(context):
    service,sessions,actor,channel,*_=context
    created=await service.create_publication(actor,{"content_kind":"text","title":"Original title","text":"Body","channel_id":channel},str(uuid4()))
    dto=await service.get_publication(actor,created["publication_id"])
    async with sessions() as session,session.begin():
        operation=Operation(id=str(uuid4()),actor_id=actor,job_id=dto["id"],kind="title_translation",status="waiting_worker",stage_key="worker",payload={})
        session.add(operation);await session.flush()
        task=NewsTask(created_by_user_id=actor,target_channel_id=channel,kind="title_translation",origin_chat_id=None,source_kind="text",operation_id=operation.id,source_job_version=dto["version"],input_payload={"kind":"title_translation","title":dto["title"],"job_id":dto["id"]},status="queued",model_name="text-old",user_tags=[])
        session.add(task);await session.flush();task_id=task.id
    return dto,task_id


async def lease(context,ready=None):
    service,_,_,_,_,credential,*_=context
    return await service.worker_command("lease",{"ready_models":ready if ready is not None else ["text-old","text-new"]},credential)


def task_payload(leased):return {"task_id":leased["id"],"lease_token":leased["lease_token"],"generation":leased["generation"],"lease_revision":leased["lease_revision"]}


async def test_registered_worker_credentials_have_no_admin_access_and_pairing_is_one_time(worker_context):
    service,_,actor,_,_,credential,registered=worker_context
    assert registered["system_id"] and "bot_token" not in registered
    with pytest.raises(PlatformError):await service.worker_command("heartbeat",{},"bad")
    code=await service.pair_worker(actor,{})
    await service.worker_command("register",{"pairing_code":code["pairing_code"]})
    with pytest.raises(PlatformError):await service.worker_command("register",{"pairing_code":code["pairing_code"]})


async def test_offline_or_embedding_only_worker_never_consumes_attempt(worker_context):
    service,sessions,*_=worker_context
    _,task_id=await title_task(worker_context)
    assert await lease(worker_context,[]) is None
    assert await lease(worker_context,["embed-unsafe"]) is None
    async with sessions() as session:assert (await session.get(NewsTask,task_id)).attempts==0


async def test_model_selection_does_not_reassign_waiting_snapshot(worker_context):
    service,sessions,_,_,_,credential,registered=worker_context
    _,task_id=await title_task(worker_context)
    async with sessions() as session,session.begin():(await session.get(AIWorker,registered["worker_id"])).selected_model="text-new"
    assert await lease(worker_context,["text-new"]) is None
    leased=await lease(worker_context,["text-old","text-new"])
    assert leased["id"]==task_id and leased["model_name"]=="text-old"


async def test_title_completion_is_idempotent_and_does_not_create_second_job(worker_context):
    service,sessions,actor,_,_,credential,*_=worker_context
    job,task_id=await title_task(worker_context)
    leased=await lease(worker_context)
    result={**task_payload(leased),"result":{"translated_text":"English title","source_language":"ja"}}
    first=await service.worker_command("complete",result,credential)
    assert first["applied"] is True
    assert first==await service.worker_command("complete",result,credential)
    assert (await service.get_publication(actor,job["id"]))["title_translation"]=="English title"
    async with sessions() as session:assert len((await session.scalars(select(Job))).all())==1
    with pytest.raises(PlatformError):await service.worker_command("complete",{**result,"result":{"translated_text":"Other","source_language":"ja"}},credential)


async def test_late_title_result_becomes_proposal_without_overwriting_user(worker_context):
    service,sessions,actor,_,_,credential,*_=worker_context
    job,_=await title_task(worker_context);leased=await lease(worker_context)
    await service.patch_publication(actor,job["id"],{"expected_version":job["version"],"title":"User corrected title"})
    result=await service.worker_command("complete",{**task_payload(leased),"result":{"translated_text":"Stale title","source_language":"ja"}},credential)
    assert result["applied"] is False
    current=await service.get_publication(actor,job["id"])
    assert current["title"]=="User corrected title" and current["title_translation"]==""
    async with sessions() as session:assert (await session.scalar(select(Operation))).status=="waiting_user"


async def test_cancel_reassignment_and_generation_reject_late_events(worker_context):
    service,sessions,actor,_,gate,credential,*_=worker_context
    _,task_id=await title_task(worker_context);leased=await lease(worker_context)
    with pytest.raises(PlatformError):await service.worker_command("progress",{**task_payload(leased),"generation":999,"stage":"model"},credential)
    await service.ai_task_action(actor,task_id,"cancel",{'expected_version':leased['lease_revision']})
    with pytest.raises(PlatformError):await service.worker_command("complete",{**task_payload(leased),"result":{"translated_text":"Title","source_language":"en"}},credential)


async def test_heartbeat_cannot_revive_released_lease(worker_context):
    service,_,_,_,_,credential,*_=worker_context
    await title_task(worker_context);leased=await lease(worker_context)
    await service.worker_command("release",task_payload(leased),credential)
    with pytest.raises(PlatformError):await service.worker_command("heartbeat",task_payload(leased),credential)


@pytest.mark.parametrize("origin",["http://public.example","https://user:password@example.com","https://example.com/api","https://example.com/#x","https://example.com/?token=secret"])
def test_worker_origin_refuses_credential_redirect_paths_and_public_http(origin):
    with pytest.raises(ValueError):trusted_origin(origin)


def test_worker_origin_allows_https_and_explicit_local_profile():
    assert trusted_origin("https://example.com/")=="https://example.com"
    assert trusted_origin("http://127.0.0.1:8000")=="http://127.0.0.1:8000"


async def test_worker_offline_reports_catalog_and_does_not_lease(monkeypatch):
    import app.worker_v1 as module
    monkeypatch.setattr(module,"list_model_ids",AsyncMock(side_effect=RuntimeError("offline")))
    api=SimpleNamespace(call=AsyncMock(return_value={}))
    settings=HomeWorkerSettings(_config_file="missing.toml",_env_file=None,token="isolated")
    worker=RegisteredWorker(api=api,settings=settings,session=None,extractor=None)
    assert await worker.run_once() is False
    assert [call.args[0] for call in api.call.await_args_list]==["capabilities","heartbeat"]


async def test_legacy_adapter_uses_same_queue_and_never_claims_translation(worker_context,monkeypatch):
    service,sessions,actor,channel,_,credential,*_=worker_context
    monkeypatch.setenv("NEWS_WORKER_TOKEN","isolated-legacy-secret")
    await title_task(worker_context)
    from app.services.news_task_service import NewsTaskService
    news=await NewsTaskService(sessions).create(user_id=actor,channel_id=channel,origin_chat_id=None,source_kind="manual",input_payload={"kind":"manual","source_text":"Text"},user_tags=[],model_name="text-old",max_attempts=3)
    old=await service.legacy_worker_command("lease",{"worker_id":"old-home","capabilities":{"model":"text-old"}},"isolated-legacy-secret")
    assert old["id"]==news.id
    new=await service.worker_command("lease",{"ready_models":["text-old"]},credential)
    assert new["kind"]=="title_translation" and new["id"]!=news.id
    assert await service.legacy_worker_command("lease",{"worker_id":"old-home","capabilities":{"model":"text-old"}},"isolated-legacy-secret") is None
    with pytest.raises(PlatformError):await service.legacy_worker_command("health",{},credential)


async def test_legacy_adapter_cannot_report_title_result_even_with_task_id(worker_context,monkeypatch):
    service,_,_,_,_,credential,*_=worker_context
    monkeypatch.setenv("NEWS_WORKER_TOKEN","isolated-legacy-secret")
    await title_task(worker_context);leased=await lease(worker_context)
    with pytest.raises(PlatformError):await service.legacy_worker_command("progress",{**task_payload(leased),"stage":"llm_processing","message":"Working"},"isolated-legacy-secret")


async def test_web_news_uploads_survive_ai_completion(worker_context):
    import io
    from PIL import Image
    from app.news.models import ExtractedNewsSource,NewsSourceKind
    from app.news.worker_models import WorkerResult,NewsDraft
    service,sessions,actor,channel,_,credential,*_=worker_context
    image=Image.new("RGB",(24,24),(12,55,99));stream=io.BytesIO();image.save(stream,"PNG")
    asset=await service.upload_asset(actor,stream.getvalue(),filename="attached.png")
    accepted=await service.create_publication(actor,{"content_kind":"news","text":"Original source","channel_id":channel,"asset_ids":[asset["id"]]},str(uuid4()))
    leased=await lease(worker_context)
    result=WorkerResult(source=ExtractedNewsSource(kind=NewsSourceKind.MANUAL,source_id="s",source_url=None,normalized_url=None,title="Original",raw_text="Original source"),draft=NewsDraft(headline="Headline",body="Rewritten body")).model_dump(mode="json")
    completed=await service.worker_command("complete",{**task_payload(leased),"result":result},credential)
    job=await service.get_publication(actor,completed["job_id"])
    assert [item["id"] for item in job["assets"]]==[asset["id"]]
    assert job["assets"][0]["selected"]


async def second_worker(context):
    service,_,actor,*_=context
    code=await service.pair_worker(actor,{})
    registered=await service.worker_command('register',{'pairing_code':code['pairing_code'],'name':'second','provider':'lmstudio'})
    await service.worker_command('capabilities',{'models':['text-old','text-new'],'capabilities':{'task_kinds':['news','title_translation'],'source_types':['manual']}},registered['credential'])
    return registered


async def test_global_selection_binds_new_news_to_chosen_worker_and_keeps_active_snapshot(worker_context):
    from app.db.models import ApplicationState
    service,sessions,actor,channel,_,credential,first=worker_context
    second=await second_worker(worker_context)
    await service.ai_selection(actor,{'worker_id':second['worker_id'],'model_id':'text-new'})
    async with sessions() as session:assert (await session.get(ApplicationState,1)).settings['ai_worker_id']==second['worker_id']
    accepted=await service.create_publication(actor,{'content_kind':'news','text':'Original news','channel_id':channel},str(uuid4()))
    assert await lease(worker_context) is None
    active=await service.worker_command('lease',{'ready_models':['text-new']},second['credential'])
    assert active['id']==accepted['task_id'] and active['model_name']=='text-new'
    await service.ai_selection(actor,{'worker_id':first['worker_id'],'model_id':'text-old'})
    later=await service.create_publication(actor,{'content_kind':'news','text':'Next news','channel_id':channel},str(uuid4()))
    assigned=await lease(worker_context)
    assert assigned['id']==later['task_id'] and assigned['model_name']=='text-old'
    await service.worker_command('progress',{**task_payload(active),'stage':'model','message':'Working'},second['credential'])
    async with sessions() as session:
        previous=await session.get(NewsTask,active['id'])
        assert previous.model_name=='text-new' and previous.lease_owner==second['worker_id']


async def test_revoked_selected_worker_does_not_silently_fall_back(worker_context):
    service,sessions,actor,channel,_,credential,first=worker_context
    second=await second_worker(worker_context)
    await service.ai_selection(actor,{'worker_id':second['worker_id'],'model_id':'text-new'})
    await service.revoke_worker(actor,second['worker_id'])
    accepted=await service.create_publication(actor,{'content_kind':'news','text':'Await explicit reassignment','channel_id':channel},str(uuid4()))
    assert await lease(worker_context) is None
    async with sessions() as session:
        task=await session.get(NewsTask,accepted['task_id'])
        assert task.input_payload['metadata']['awaiting_selection'] and task.attempts==0
    await service.ai_task_action(actor,accepted['task_id'],'reassign',{'expected_version':1,'worker_id':first['worker_id'],'model_id':'text-old'})
    assigned=await lease(worker_context)
    assert assigned['id']==accepted['task_id'] and assigned['model_name']=='text-old'


async def test_preferred_worker_pending_queue_does_not_starve_another_worker(worker_context):
    from sqlalchemy import insert
    service,sessions,actor,channel,_,credential,first=worker_context
    second=await second_worker(worker_context)
    async with sessions() as session,session.begin():
        values=[{'created_by_user_id':actor,'target_channel_id':channel,'origin_chat_id':None,'kind':'news','source_kind':'manual','input_payload':{'kind':'manual','source_text':'Waiting','metadata':{'worker_id':second['worker_id']}},'status':'queued','model_name':'text-new','user_tags':[]} for _ in range(130)]
        await session.execute(insert(NewsTask),values)
        own=NewsTask(created_by_user_id=actor,target_channel_id=channel,origin_chat_id=None,kind='news',source_kind='manual',input_payload={'kind':'manual','source_text':'Ready','metadata':{'worker_id':first['worker_id']}},status='queued',model_name='text-old',user_tags=[])
        session.add(own);await session.flush();own_id=own.id
    assigned=await lease(worker_context)
    assert assigned['id']==own_id


async def test_title_translation_captures_global_worker_selection(worker_context):
    from app.operations.runner import OperationRunner
    service,sessions,actor,channel,_,credential,first=worker_context
    second=await second_worker(worker_context)
    await service.ai_selection(actor,{'worker_id':second['worker_id'],'model_id':'text-new'})
    created=await service.create_publication(actor,{'content_kind':'text','title':'Title','text':'Body','channel_id':channel},str(uuid4()))
    job=await service.get_publication(actor,created['publication_id'])
    operation=await service.publication_action(actor,job['id'],'translate',{'expected_version':job['version']},str(uuid4()))
    await OperationRunner(service)._translation(operation['operation_id'])
    assert await lease(worker_context) is None
    assigned=await service.worker_command('lease',{'ready_models':['text-new']},second['credential'])
    assert assigned['kind']=='title_translation' and assigned['model_name']=='text-new'


async def test_web_news_keeps_configured_legacy_worker_before_v1_registration(worker_context,monkeypatch):
    from sqlalchemy import delete
    service,sessions,actor,channel,*_=worker_context
    async with sessions() as session,session.begin():await session.execute(delete(AIWorker))
    service.runtime=SimpleNamespace(settings=SimpleNamespace(news_worker_token='legacy-only',news_model_name='legacy-model'))
    accepted=await service.create_publication(actor,{'content_kind':'news','text':'Original','channel_id':channel},str(uuid4()))
    leased=await service.legacy_worker_command('lease',{'worker_id':'old-home','capabilities':{'model':'legacy-model'}},'legacy-only')
    assert leased['id']==accepted['task_id'] and leased['model_name']=='legacy-model'


async def test_ai_task_mutation_rejects_version_before_lease_claim(worker_context):
    service,_,actor,_,_,credential,*_=worker_context
    _,task_id=await title_task(worker_context)
    before=next(item for item in (await service.ai_tasks(actor))['items'] if item['id']==task_id)
    leased=await lease(worker_context)
    with pytest.raises(PlatformError) as stale:
        await service.ai_task_action(actor,task_id,'cancel',{'expected_version':before['version']})
    assert stale.value.code=='version_conflict' and stale.value.current_version==leased['lease_revision']
    with pytest.raises(PlatformError) as missing:
        await service.ai_task_action(actor,task_id,'cancel',{})
    assert missing.value.status==422
    await service.ai_task_action(actor,task_id,'cancel',{'expected_version':leased['lease_revision']})


async def test_cancelling_operation_revokes_linked_ai_lease(worker_context):
    service,sessions,actor,_,_,credential,*_=worker_context
    _,task_id=await title_task(worker_context);leased=await lease(worker_context)
    async with sessions() as session:operation_id=(await session.get(NewsTask,task_id)).operation_id
    await service.operation_action(actor,operation_id,'cancel',{})
    with pytest.raises(PlatformError):
        await service.worker_command('complete',{**task_payload(leased),'result':{'translated_text':'Late result','source_language':'en'}},credential)
    async with sessions() as session:assert (await session.get(NewsTask,task_id)).status=='cancelled'


async def test_retrying_failed_operation_requeues_ai_task_and_attempts(worker_context):
    service,sessions,actor,_,_,credential,*_=worker_context
    _,task_id=await title_task(worker_context)
    async with sessions() as session,session.begin():
        task=await session.get(NewsTask,task_id);task.status='failed';task.attempts=3
        operation=await session.get(Operation,task.operation_id);operation.status='failed';operation_id=operation.id
    await service.operation_action(actor,operation_id,'retry',{})
    leased=await lease(worker_context)
    assert leased['id']==task_id
    async with sessions() as session:assert (await session.get(NewsTask,task_id)).attempts==1


async def test_telegram_news_replay_keeps_accepted_worker_after_global_selection_changes(worker_context):
    from app.bot.durable import current_update
    service,sessions,actor,channel,_,credential,first=worker_context
    second=await second_worker(worker_context)
    await service.ai_selection(actor,{'worker_id':first['worker_id'],'model_id':'text-old'})
    identity=current_update.set(('bot',12345))
    payload={'kind':'manual','source_text':'Accepted original text'}
    try:
        one=await service.news.create(user_id=actor,channel_id=channel,origin_chat_id=1,source_kind='manual',input_payload=payload,user_tags=[],model_name='ignored',max_attempts=3)
        await service.ai_selection(actor,{'worker_id':second['worker_id'],'model_id':'text-new'})
        replay=await service.news.create(user_id=actor,channel_id=channel,origin_chat_id=1,source_kind='manual',input_payload=payload,user_tags=[],model_name='ignored',max_attempts=3)
    finally:current_update.reset(identity)
    assert replay.id==one.id and replay.model_name=='text-old'
    assert replay.input_payload['metadata']['worker_id']==first['worker_id']
    async with sessions() as session:assert len((await session.scalars(select(NewsTask))).all())==1
