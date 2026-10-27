from sqlalchemy import select

from app.bootstrap import _sync_channels, bootstrap
from app.config import Settings
from app.db.models import Channel
from app.db.session import create_database, create_schema
from app.news.http import PublicOnlyResolver


async def test_application_bootstraps_without_network():
    settings = Settings(
        _env_file=None,
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
        admin_ids={1},
        database_url="sqlite+aiosqlite:///:memory:",
        channels_json={"artwork": {"chat_id": "-1001", "title": "Artwork"}},
    )
    application = await bootstrap(settings)
    assert application.dispatcher.resolve_used_update_types()
    assert isinstance(application.http.connector._resolver, PublicOnlyResolver)
    await application.http.close()
    await application.local_http.close()
    await application.bot.session.close()
    await application.engine.dispose()


async def test_runtime_default_and_interval_survive_channel_resync():
    settings = Settings(
        _config_file="missing.toml",
        _env_file=None,
        bot_token="123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
        default_channel_alias="artwork",
        channels_json={
            "artwork": {
                "chat_id": "-1001", "title": "Artwork",
                "publish_interval_seconds": 60,
            },
            "archive": {"chat_id": "-1002", "title": "Archive"},
        },
    )
    engine, sessions = create_database("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    await _sync_channels(sessions, settings)

    async with sessions() as session, session.begin():
        artwork = await session.scalar(select(Channel).where(Channel.alias == "artwork"))
        archive = await session.scalar(select(Channel).where(Channel.alias == "archive"))
        artwork.is_default = False
        artwork.publish_interval_seconds = 900
        archive.is_default = True

    await _sync_channels(sessions, settings)

    async with sessions() as session:
        artwork = await session.scalar(select(Channel).where(Channel.alias == "artwork"))
        archive = await session.scalar(select(Channel).where(Channel.alias == "archive"))
        assert artwork.publish_interval_seconds == 900
        assert artwork.is_default is False
        assert archive.is_default is True
    await engine.dispose()
