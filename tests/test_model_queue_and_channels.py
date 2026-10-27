from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, text

from app.bootstrap import _sync_channels
from app.config import Settings
from app.db.models import Channel, User
from app.db.session import create_database, create_schema
from app.domain.enums import NewsTaskStatus
from app.news.models import NewsSourceKind, NewsSourceRequest
from app.services.caption_service import CaptionService
from app.services.news_submission_service import NewsSubmissionService
from app.services.news_task_service import NewsTaskService
from app.services.preview_service import PreviewService
from app.utils.tags import publication_tags
from test_title_translation import make_post


async def test_local_model_changes_retarget_waiting_and_retried_tasks_but_preserve_active_lease():
    engine, sessions = create_database("sqlite+aiosqlite:///:memory:")
    try:
        await create_schema(engine)
        async with sessions() as session, session.begin():
            user = User(telegram_user_id=1)
            channel = Channel(alias="art", telegram_chat_id="-1001", title="Art")
            session.add_all([user, channel])
            await session.flush()
        service = NewsTaskService(sessions)

        async def create():
            return await service.create(
                user_id=user.id, channel_id=channel.id, origin_chat_id=1,
                source_kind="manual", input_payload={"source_text": "Source text"},
                user_tags=[], model_name="old", max_attempts=3,
            )

        first, second = await create(), await create()
        active = await service.lease(worker_id="pc", lease_seconds=600, model_name="old", use_worker_model=True)
        assert active.id == first.id
        next_task = await service.lease(worker_id="pc2", lease_seconds=600, model_name="new", use_worker_model=True)
        assert next_task.id == second.id and next_task.model_name == "new"
        assert (await service.get(first.id)).model_name == "old"
        assert (await service.get(first.id)).status == NewsTaskStatus.LEASED
        await service.fail(first.id, lease_token=active.lease_token, error="temporary failure", retryable=True)
        retry = await service.lease(worker_id="pc", lease_seconds=600, model_name="new", use_worker_model=True)
        assert retry.id == first.id and retry.model_name == "new"
        assert (await service.get(second.id)).lease_token == next_task.lease_token
        assert await service.lease(worker_id="pc", lease_seconds=600, use_worker_model=True) is None

        third = await create()
        # The separate VPS/Ollama setup retains strict model routing.
        assert await service.lease(worker_id="remote", lease_seconds=600, model_name="new") is None
        assert (await service.get(third.id)).model_name == "old"
    finally:
        await engine.dispose()


async def test_channel_migration_and_resync_preserve_identity_and_runtime_settings():
    settings = Settings(
        _config_file="missing.toml", _env_file=None, bot_token="",
        default_channel_alias="spice_and_wolf", channels_json={
            "spice_and_wolf": {"chat_id": "@old", "title": "Spice", "publish_interval_seconds": 7200},
        },
    )
    engine, sessions = create_database("sqlite+aiosqlite:///:memory:")
    try:
        await create_schema(engine)
        await _sync_channels(sessions, settings)
        async with sessions() as session, session.begin():
            old = await session.scalar(select(Channel))
            channel_id = old.id
            old.is_paused = True
            old.next_publish_at = None
        # Exercise the upgrade path of an existing database without this column.
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE schema_migrations"))
            await connection.execute(text("ALTER TABLE channels DROP COLUMN default_tags"))
        await create_schema(engine)
        updated = settings.model_copy(update={"channels_json": {
            "spice_and_wolf": {"chat_id": "@spiceandwolfarts", "title": "Spice and Wolf Arts"},
            "arknights": {"chat_id": "@arknights_endfield_art", "tags": ["arknights"]},
        }})
        await _sync_channels(sessions, updated)
        await _sync_channels(sessions, updated)
        async with sessions() as session:
            channels = list((await session.scalars(select(Channel).order_by(Channel.id))).all())
        assert len(channels) == 1  # CHN-01: new aliases also require explicit config import after seed.
        spice = channels[0]
        assert spice.id == channel_id and spice.is_default and spice.is_paused
        assert spice.telegram_chat_id == "@old"  # CHN-01: restart never overwrites SQLite channel identity.
        assert spice.publish_interval_seconds == 7200 and spice.default_tags == []
        assert all(channel.alias != "arknights" for channel in channels)
    finally:
        await engine.dispose()


def test_preview_channel_tags_follow_destination_and_do_not_modify_manual_tags(tmp_path):
    post = make_post("Garden")
    service = PreviewService(
        downloader=None, media=None, captions=CaptionService(), publisher=None,
        storage=tmp_path, auto_add_source_tags=False, max_tags=20, max_tag_length=64,
        translator=None,
    )
    job = SimpleNamespace(
        user_tags=["art"], source_tags=["ignored"], caption_override=None,
        channel=SimpleNamespace(default_tags=["arknights"], caption_template=None),
    )
    caption = service._build_caption(job, post)
    assert "#arknights #art" in caption and "#ignored" not in caption
    assert job.user_tags == ["art"]
    job.channel = SimpleNamespace(default_tags=["spice_and_wolf"], caption_template=None)
    caption = service._build_caption(job, post)
    assert "#spice_and_wolf #art" in caption and "#arknights" not in caption
    job.caption_override = "Manual caption"
    assert service._build_caption(job, post) == "Manual caption"


def test_channel_tags_are_normalized_deduplicated_and_reserved_within_limits():
    assert publication_tags(["art", "arknights"], ["source"], ["#Arknights"], max_tags=2) == ["arknights", "art"]
    settings = Settings(
        _config_file="missing.toml", _env_file=None, bot_token="",
        channels_json={"arknights": {"chat_id": "@arknights_endfield_art", "tags": ["#Arknights", "arknights"]}},
    )
    assert settings.channels_json["arknights"]["tags"] == ["arknights"]


async def test_new_submissions_resolve_current_model_and_accept_initially_empty_selection():
    jobs = SimpleNamespace(
        ensure_user=AsyncMock(return_value=SimpleNamespace(id=1)),
        get_preferred_channel=AsyncMock(return_value=SimpleNamespace(id=2)),
    )
    tasks = SimpleNamespace(create=AsyncMock())
    current = [""]
    submissions = NewsSubmissionService(
        jobs=jobs, tasks=tasks, default_channel_alias="art", model_name="old",
        max_attempts=3, model_resolver=lambda: current[0],
    )
    for model in ("", "new"):
        current[0] = model
        await submissions.create(
            telegram_user_id=1, username=None, display_name="Admin", origin_chat_id=1,
            request=NewsSourceRequest(kind=NewsSourceKind.MANUAL, value="Source text"),
        )
    assert [call.kwargs["model_name"] for call in tasks.create.await_args_list] == [None, "new"]
