"""Exercise the UI wire contracts against real HTTP routes and isolated SQLite."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, select

from app.api.auth import AuthStore
from app.api.server import WebConfig, create_app
from app.application.platform import PlatformError
from app.db.models import AuditEvent, Job
from app.storage.assets import store_bytes
from test_web_core import core, draft, prepare
from test_web_extras import extras, job_values


@pytest.fixture
async def browser_api(core, tmp_path):
    service, _, _, actor, *_ = core
    auth = AuthStore(tmp_path / 'http-auth')
    token = auth.exchange_local(auth.local_code(actor, 1))['token']
    app = create_app(service, auth, config=WebConfig(origin='http://localhost', admin_ids={1}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
        headers={'Authorization': 'Bearer ' + token, 'Origin': 'http://localhost'}) as client:
        yield client


@pytest.mark.parametrize('status', ['queue', 'history'])
async def test_publication_tab_wire_filters(core, browser_api, status):
    job = await prepare(core, await draft(core))
    await browser_api.post(f"/api/v1/publications/{job['id']}/actions/enqueue",
        json={'expected_version': job['version']}, headers={'Idempotency-Key': str(uuid4())})
    response = await browser_api.get('/api/v1/publications', params={'status': status})
    assert response.status_code == 200, response.text
    assert len(response.json()['items']) == (1 if status == 'queue' else 0)


@pytest.mark.parametrize('outcome,expected_error,expected_status', [
    ('published', None, 'completed'),
    ('not_published', 'manually_checked_not_published', 'failed'),
    ('partial', 'uncertain_publish', 'failed'),
    ('still_unknown', 'uncertain_publish', 'failed'),
])
async def test_uncertain_form_http_contract(core, browser_api, outcome, expected_error, expected_status):
    service, _, sessions, actor, *_ = core
    job = await prepare(core, await draft(core))
    async with sessions() as session, session.begin():
        row = await session.get(Job, job['id'])
        row.status = 'failed'; row.error_code = 'uncertain_publish'
    job = await service.get_publication(actor, job['id'])
    payload = {'expected_version': job['version'], 'outcome': outcome, 'note': 'Проверено вручную', 'message_ids': [123]}
    key = str(uuid4())
    response = await browser_api.post(f"/api/v1/publications/{job['id']}/actions/resolve-uncertain",
        json=payload, headers={'Idempotency-Key': key})
    assert response.status_code == 200, response.text
    resolved = response.json()
    assert resolved['status'] == expected_status
    assert (resolved['error'] or {}).get('code') == expected_error
    repeat = await browser_api.post(f"/api/v1/publications/{job['id']}/actions/resolve-uncertain",
        json=payload, headers={'Idempotency-Key': key})
    assert repeat.json() == resolved
    async with sessions() as session:
        audits = list((await session.scalars(select(AuditEvent).where(AuditEvent.action == 'publication.resolve_uncertain'))).all())
        assert len(audits) == 1 and audits[0].details['note'] == payload['note']
    if outcome in {'partial', 'still_unknown'}:
        retry = await browser_api.post(f"/api/v1/publications/{job['id']}/actions/retry",
            json={'expected_version': resolved['version']}, headers={'Idempotency-Key': str(uuid4())})
        assert retry.status_code == 409 and retry.json()['error']['code'] == 'uncertain_publish'


async def test_unknown_resolution_fields_are_rejected(browser_api):
    response = await browser_api.post('/api/v1/publications/1/actions/resolve-uncertain',
        json={'expected_version': 1, 'resolution': 'confirmed_sent'}, headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 422


@pytest.mark.parametrize('ids', [[True], [-1], ['123'], [1.5]])
async def test_resolution_message_ids_are_strict_positive_integers(browser_api, ids):
    response = await browser_api.post('/api/v1/publications/1/actions/resolve-uncertain',
        json={'expected_version': 1, 'outcome': 'published', 'message_ids': ids}, headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 422


async def test_already_invalidated_queue_can_be_prepared_without_losing_position(core):
    from app.operations.runner import OperationRunner
    service, _, sessions, actor, *_ = core
    job=await prepare(core,await draft(core))
    job=await service.publication_action(actor,job['id'],'enqueue',{'expected_version':job['version']},str(uuid4()))
    async with sessions() as session,session.begin():
        row=await session.get(Job,job['id']);row.render_plan=None
    current=await service.get_publication(actor,job['id'])
    assert 'prepare' in current['allowed_actions']
    op=await service.publication_action(actor,job['id'],'prepare',{'expected_version':current['version']},str(uuid4()))
    await OperationRunner(service)._prepare(op['operation_id'])
    current=await service.get_publication(actor,job['id'])
    assert current['status']=='queued' and current['queue_position']==job['queue_position'] and current['readiness']['ready']


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('changes', [{'caption_override': 'Изменённая подпись'}, {'user_tags': ['#new']}])
async def test_queued_safe_edit_keeps_plan_position_and_schedule(core, changes, scheduled):
    service, _, sessions, actor, *_ = core
    job = await prepare(core, await draft(core))
    payload = {'expected_version': job['version']}
    if scheduled: payload['scheduled_at'] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    job = await service.publication_action(actor, job['id'], 'schedule' if scheduled else 'enqueue', payload, str(uuid4()))
    changed = await service.patch_publication(actor, job['id'], {'expected_version': job['version'], **changes})
    assert changed['readiness']['ready'], changed['readiness']
    assert changed['queue_position'] == job['queue_position']
    assert changed['schedule']['utc'] == job['schedule']['utc']
    plan = await service.preview(actor, job['id'])
    assert plan['job_version'] == changed['version'] and plan['assets_prepared']
    if 'caption_override' in changes: assert 'Изменённая подпись' in plan['html']
    if not scheduled:
        claimed = await service.jobs.claim_next()
        assert claimed is not None and claimed.id == job['id']


async def test_duplicate_confirmation_is_separate_versioned_and_idempotent(core, browser_api):
    service, _, sessions, actor, *_ = core
    first = await draft(core)
    second = await draft(core)
    async with sessions() as session, session.begin():
        for job_id in (first['id'], second['id']):
            row = await session.get(Job, job_id)
            row.normalized_url = 'https://example.com/source'
    job = await prepare(core, await service.get_publication(actor, second['id']))
    enqueue = f"/api/v1/publications/{job['id']}/actions/enqueue"
    assert (await browser_api.post(enqueue, json={'expected_version': job['version']}, headers={'Idempotency-Key': str(uuid4())})).status_code == 409
    path = f"/api/v1/publications/{job['id']}/actions/confirm-duplicate"
    headers = {'Idempotency-Key': str(uuid4())}; payload = {'expected_version': job['version']}
    accepted = await browser_api.post(path, json=payload, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['status'] == 'waiting_confirmation'
    assert (await browser_api.post(path, json=payload, headers=headers)).json() == accepted.json()
    assert 'confirm-duplicate' not in accepted.json()['allowed_actions']
    queued = await browser_api.post(enqueue, json={'expected_version': accepted.json()['version']}, headers={'Idempotency-Key': str(uuid4())})
    assert queued.status_code == 200 and queued.json()['status'] == 'queued'


async def test_full_queue_pages_in_position_order_and_move_across_page_boundary(extras):
    service, sessions, actor, channel, *_ = extras
    async with sessions() as session, session.begin():
        await session.execute(insert(Job), [job_values(actor, channel, i) for i in range(1, 151)])
    pages=[]; cursor=None
    for _ in range(3):
        page=await service.queue(actor,channel_id=channel,limit=50,cursor=cursor)
        pages.extend(item['id'] for item in page['items']);cursor=page['next_cursor']
    assert pages == list(range(1,151)) and cursor is None
    first=await service.queue(actor,channel_id=channel,limit=50)
    assert first['items'][-1]['queue_movement']['down']
    payload={'expected_queue_revision':first['revisions'][str(channel)],'job_id':51,'direction':'up'}
    key=str(uuid4())
    moved=await service.move_queue(actor,channel,payload,key)
    assert await service.move_queue(actor,channel,payload,key) == moved
    latest=await service.queue(actor,channel_id=channel,limit=50)
    assert latest['items'][-1]['id']==51
    with pytest.raises(PlatformError) as stale:
        await service.queue(actor,channel_id=channel,cursor=first['next_cursor'])
    assert stale.value.code=='queue_conflict'
    with pytest.raises(PlatformError): await service.move_queue(actor,channel,payload,str(uuid4()))


def test_new_upload_rejects_apng_and_unneeded_plugins_before_allocation(tmp_path, monkeypatch):
    from test_security_regressions import apng, block_allocation, ForbiddenPluginAttempt
    from PIL import Image
    block_allocation(monkeypatch)
    with pytest.raises(ValueError): store_bytes(tmp_path,apng(1))
    Image.init()
    _,accept=Image.OPEN['XBM']
    def forbidden(*args,**kwargs):raise ForbiddenPluginAttempt('Upload invoked XBM')
    monkeypatch.setitem(Image.OPEN,'XBM',(forbidden,accept))
    with pytest.raises(ValueError):store_bytes(tmp_path,b'#define im_width 1\n#define im_height 1\nstatic char im_bits[] = {0};')
    assert not list(tmp_path.glob('assets/**/*'))
