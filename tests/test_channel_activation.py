from uuid import uuid4

import httpx

from app.api.auth import AuthStore
from app.api.server import WebConfig, create_app
from test_web_core import core, draft, prepare


async def test_channel_activation_and_pause_control_queued_publications(core, tmp_path):
    platform, _, _, actor, channel_id, *_ = core
    prepared = await prepare(core, await draft(core))
    await platform.publication_action(actor, prepared['id'], 'enqueue', {'expected_version': prepared['version']}, str(uuid4()))

    auth = AuthStore(tmp_path / 'channel-control-auth')
    token = auth.exchange_local(auth.local_code(actor, 1))['token']
    app = create_app(platform, auth, config=WebConfig(origin='http://localhost', admin_ids={1}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
            headers={'Authorization': 'Bearer ' + token, 'Origin': 'http://localhost'}) as client:
        path = f'/api/v1/channels/{channel_id}'
        current = (await client.get(path)).json()
        response = await client.patch(path, json={'expected_version': current['version'], 'is_enabled': False, 'is_paused': True})
        assert response.status_code == 200

        response = await client.patch(path, json={'expected_version': response.json()['version'], 'is_enabled': True, 'is_paused': False})
        assert response.status_code == 200
        activated = response.json()
        assert activated['is_enabled'] is True and activated['is_paused'] is False

        response = await client.patch(path, json={'expected_version': activated['version'], 'is_paused': True})
        assert response.status_code == 200
        paused = response.json()
        assert paused['is_enabled'] is True and paused['is_paused'] is True
        assert await platform.jobs.claim_next() is None
        assert (await draft(core, 'Можно готовить материалы на паузе'))['channel']['id'] == channel_id

        stale = await client.patch(path, json={'expected_version': activated['version'], 'is_enabled': True, 'is_paused': False})
        assert stale.status_code == 409
        assert (await client.get(path)).json()['is_paused'] is True

        response = await client.patch(path, json={'expected_version': paused['version'], 'is_enabled': True, 'is_paused': False})
        assert response.status_code == 200 and response.json()['is_paused'] is False
        claimed = await platform.jobs.claim_next()
        assert claimed and claimed.id == prepared['id']
