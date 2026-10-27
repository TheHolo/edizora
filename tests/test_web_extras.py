from datetime import UTC,datetime,timedelta
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
from sqlalchemy import select,insert

from app.api.extras import WebPlatformService
from app.application.platform import PlatformError
from app.db.models import User,Channel,Job,NewsTask,AuditEvent,ChannelMemberSnapshot,utcnow
from app.db.session import create_database,create_schema
from app.host.control import ControlStore


@pytest.fixture
async def extras(tmp_path):
    engine,sessions=create_database(f"sqlite+aiosqlite:///{tmp_path/'content.sqlite'}");await create_schema(engine)
    async with sessions() as session,session.begin():
        user=User(telegram_user_id=1);channel=Channel(alias="main",title="Original",telegram_chat_id="-1001",is_default=True,publish_interval_seconds=60)
        session.add_all([user,channel]);await session.flush();actor,channel_id=user.id,channel.id
    gate=ControlStore(tmp_path/'control');gate.initialize(system_id=str(uuid4()),data_root=tmp_path/'data',role="active")
    service=WebPlatformService(sessions,data_root=tmp_path/'data',gate=gate)
    yield service,sessions,actor,channel_id,gate
    await engine.dispose()


def job_values(actor,channel,index,scheduled=None):
    return {'created_by_user_id':actor,'target_channel_id':channel,'provider':'manual','source_id':str(index),'source_url':'','normalized_url':'','status':'scheduled' if scheduled else 'queued',
            'content_kind':'news','scheduled_at':scheduled,'queue_position':None if scheduled else index,'user_tags':[],'source_tags':[],
            'post_data':{'schema_version':1,'provider':'manual','source_id':str(index),'source_url':'','normalized_url':'','title':f'Post {index}','body':'Text','author_name':'','author_url':'','media_items':[],'metadata':{}}}


async def test_calendar_date_filter_sees_records_older_than_latest_hundred(extras):
    service,sessions,actor,channel,*_=extras
    monday=datetime(2030,1,7,tzinfo=UTC)
    async with sessions() as session,session.begin():
        await session.execute(insert(Job),[job_values(actor,channel,i,monday+timedelta(days=1) if i<=5 else monday+timedelta(days=30)) for i in range(1,151)])
    response=await service.calendar(actor,start=monday.isoformat(),end=(monday+timedelta(days=7)).isoformat())
    assert len(response['items'])==5 and response['total']==5
    assert {item['id'] for item in response['items']}==set(range(1,6))


async def test_calendar_pages_complete_week_with_stable_chronological_cursor(extras):
    service,sessions,actor,channel,*_=extras
    monday=datetime(2030,1,7,tzinfo=UTC)
    async with sessions() as session,session.begin():
        await session.execute(insert(Job),[job_values(actor,channel,i,monday+timedelta(minutes=i)) for i in range(1,131)])
    cursor=None;all_ids=[];pages=[]
    while True:
        page=await service.calendar(actor,start=monday.isoformat(),end=(monday+timedelta(days=7)).isoformat(),limit=50,cursor=cursor)
        assert len(page['items'])<=50 and page['total']==130
        all_ids.extend(item['id'] for item in page['items']);pages.append(page)
        cursor=page['next_cursor']
        if not cursor:break
    assert all_ids==list(range(1,131)) and len(set(all_ids))==130
    assert [len(page['items']) for page in pages]==[50,50,30]
    with pytest.raises(PlatformError):await service.calendar(actor,start=monday.isoformat(),end=(monday+timedelta(days=7)).isoformat(),cursor='not-a-valid-cursor')


async def test_calendar_forecast_accounts_for_entire_earlier_queue(extras):
    service,sessions,actor,channel,*_=extras
    start=utcnow()+timedelta(days=3)
    async with sessions() as session,session.begin():
        (await session.get(Channel,channel)).next_publish_at=start
        await session.execute(insert(Job),[job_values(actor,channel,i) for i in range(1,151)])
    page=await service.calendar(actor,start=(start+timedelta(minutes=120)).isoformat(),end=(start+timedelta(minutes=150)).isoformat())
    assert len(page['items'])==30
    assert [item['id'] for item in page['items']]==list(range(121,151))
    assert page['items'][0]['schedule']['estimate']==(start+timedelta(minutes=120)).isoformat().replace('+00:00','Z')


async def test_calendar_reads_correct_forecast_page_from_ten_thousand_jobs(extras,record_testsuite_property):
    from time import perf_counter
    service,sessions,actor,channel,*_=extras
    start=utcnow()+timedelta(days=3)
    async with sessions() as session,session.begin():
        (await session.get(Channel,channel)).next_publish_at=start
        await session.execute(insert(Job),[job_values(actor,channel,i) for i in range(1,10001)])
    before=perf_counter()
    page=await service.calendar(actor,start=(start+timedelta(minutes=8900)).isoformat(),end=(start+timedelta(minutes=8930)).isoformat())
    record_testsuite_property('calendar_10000_ms',round((perf_counter()-before)*1000,2))
    assert page['total']==30 and [item['id'] for item in page['items']]==list(range(8901,8931))


async def test_ai_actions_match_server_state_and_explicit_retry_resets_attempts(extras):
    service,sessions,actor,channel,*_=extras
    async with sessions() as session,session.begin():
        tasks=[]
        for status in ['queued','leased','failed','completed']:
            task=NewsTask(created_by_user_id=actor,target_channel_id=channel,origin_chat_id=None,source_kind='manual',input_payload={'kind':'manual','source_text':'Text'},status=status,user_tags=[],attempts=3,max_attempts=3)
            session.add(task);tasks.append(task)
        await session.flush();ids=[t.id for t in tasks]
    values={item['id']:item for item in (await service.ai_tasks(actor))['items']}
    assert values[ids[0]]['allowed_actions']==['cancel','reassign']
    assert values[ids[1]]['allowed_actions']==['cancel']
    assert 'retry' in values[ids[2]]['allowed_actions'] and values[ids[3]]['allowed_actions']==[]
    with pytest.raises(PlatformError):await service.ai_task_action(actor,ids[1],'retry',{'expected_version':1})
    await service.ai_task_action(actor,ids[2],'retry',{'expected_version':1})
    async with sessions() as session:assert (await session.get(NewsTask,ids[2])).attempts==0


async def test_default_config_import_uses_only_configured_channel_fields(extras,monkeypatch):
    import app.config as config
    service,_,actor,channel,*_=extras
    monkeypatch.setattr(config,'Settings',lambda **kwargs:SimpleNamespace(channels_json={'main':{'chat_id':'-1001','title':'Configured','tags':['new']}},bot_token='never-show-this-secret'))
    plan=await service.config_import_plan(actor,{})
    assert plan['changes'][0]['after']=={'title':'Configured','default_tags':['new']}
    assert 'never-show-this-secret' not in json.dumps(plan)
    applied=await service.config_import_apply(actor,{'plan_id':plan['id']},str(uuid4()))
    assert applied['channels_changed']==1
    assert (await service.get_channel(actor,channel))['title']=='Configured'


async def test_channel_rights_and_last_good_statistic_survive_reload(extras):
    service,sessions,actor,channel,*_=extras
    now=utcnow()
    async with sessions() as session,session.begin():
        session.add(AuditEvent(actor_id=actor,action='channel.check',resource_id=str(channel),details={'can_post':True,'checked_at':(now-timedelta(days=1)).isoformat()}))
        session.add(AuditEvent(actor_id=actor,action='channel.check',resource_id=str(channel),details={'can_post':False,'checked_at':now.isoformat()}))
        session.add(ChannelMemberSnapshot(channel_id=channel,member_count=120,source='manual',captured_at=now-timedelta(days=1)))
        session.add(ChannelMemberSnapshot(channel_id=channel,member_count=130,source='telegram',captured_at=now))
    item=await service.get_channel(actor,channel)
    assert item['permissions']['can_post'] is False and item['permissions']['status']=='denied'
    assert item['member_count']==130 and item['member_count_updated_at']
    assert (await service.list_channels(actor))['items'][0]['member_count']==130
