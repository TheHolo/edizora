import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.models import Channel, User
from app.db.session import create_database, create_schema
from app.domain.album import initialize_album, read_album, select_album
from app.domain.enums import ContentKind, JobStatus
from app.domain.exceptions import InvalidMediaSelectionError
from app.services.album_service import AlbumEditor
from app.services.job_service import JobService, serialize_post
from app.services.preview_service import deserialize_post
from test_ingest_service import _post


@pytest.fixture
async def context(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'album.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="art", telegram_chat_id="-1001", title="Art")
        session.add_all([user, channel])
        await session.flush()
        user_id, channel_id = user.id, channel.id
    jobs = JobService(sessions)
    post = _post("pixiv", 15)
    initialize_album(post, list(range(1, 11)), automatic=True)
    job = await jobs.create_preview(user_id, post, channel_id, [], 3)
    yield jobs, AlbumEditor(jobs), job.id, sessions
    await engine.dispose()


async def test_selection_reorder_and_restore_survive_new_service_instance(context):
    jobs, editor, job_id, sessions = context
    original = (await jobs.get(job_id)).post_data
    job = await editor.change(job_id, 0, "order", order="15,3,7")
    assert [m["filename"] for m in job.post_data["media_items"]] == ["15.jpg", "3.jpg", "7.jpg"]
    assert [m["order"] for m in job.post_data["media_items"]] == [0, 1, 2]
    editor = AlbumEditor(JobService(sessions))
    job = await editor.change(job_id, 1, "first", 7)
    assert read_album(job.post_data).selection == [7, 15, 3]
    job = await editor.change(job_id, 2, "down", 7)
    assert read_album(job.post_data).selection == [15, 7, 3]
    job = await editor.change(job_id, 3, "up", 3)
    assert read_album(job.post_data).selection == [15, 3, 7]
    job = await editor.change(job_id, 4, "toggle", 15)
    job = await editor.change(job_id, 5, "toggle", 15)
    assert read_album(job.post_data).selection == [3, 7, 15]
    job = await editor.change(job_id, 6, "restore")
    assert read_album(job.post_data).selection == list(range(1, 11))
    assert read_album(job.post_data).catalog == read_album(original).catalog
    assert len(deserialize_post(job.post_data).media_items) == 10


@pytest.mark.parametrize("order", ["0,1", "1,16", "1,1", "1-3", "", "1,2,3,4,5,6,7,8,9,10,11"])
async def test_invalid_manual_selection_is_not_saved(context, order):
    jobs, editor, job_id, _ = context
    with pytest.raises(InvalidMediaSelectionError):
        await editor.change(job_id, 0, "order", order=order)
    assert read_album((await jobs.get(job_id)).post_data).revision == 0


async def test_limit_applies_to_buttons_and_first_ten_shortcut(context):
    jobs, editor, job_id, _ = context
    with pytest.raises(InvalidMediaSelectionError, match="максимум 10"):
        await editor.change(job_id, 0, "toggle", 11)
    job = await editor.change(job_id, 0, "clear")
    job = await editor.change(job_id, 1, "first10")
    assert read_album(job.post_data).selection == list(range(1, 11))


async def test_empty_album_cannot_be_queued_even_by_duplicate_button(context):
    jobs, editor, job_id, _ = context
    await editor.change(job_id, 0, "clear")
    assert await jobs.enqueue(job_id) is False
    assert await jobs.allow_duplicate_and_enqueue(job_id) is False
    await editor.change(job_id, 1, "toggle", 15)
    assert await jobs.enqueue(job_id) is True
    with pytest.raises(InvalidMediaSelectionError, match="только для черновика"):
        await editor.change(job_id, 2, "clear")


async def test_old_keyboard_and_concurrent_writes_cannot_overwrite(context):
    jobs, editor, job_id, _ = context
    results = await asyncio.gather(
        editor.change(job_id, 0, "order", order="1,3"),
        editor.change(job_id, 0, "order", order="2,4"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, InvalidMediaSelectionError) for r in results) == 1
    assert read_album((await jobs.get(job_id)).post_data).revision == 1
    with pytest.raises(InvalidMediaSelectionError, match="уже изменён"):
        await editor.change(job_id, 0, "clear")


async def test_album_snapshot_cannot_overwrite_title_and_translation_preserves_album(context):
    jobs, editor, job_id, _ = context
    snapshot = await jobs.get(job_id)
    await jobs.set_post_field(job_id, "title", "New title")
    assert await jobs.replace_draft_post(snapshot, select_album(snapshot.post_data, [15], limit=10)) is None
    await editor.change(job_id, 0, "order", order="15,7")
    metadata = {"title_translation": "English title", "title_translation_provider": "lmstudio"}
    await jobs.save_title_translation(job_id, "New title", metadata)
    restored = await jobs.get(job_id)
    assert read_album(restored.post_data).selection == [15, 7]
    assert restored.post_data["metadata"]["title_translation"] == "English title"
    await jobs.set_post_field(job_id, "title", "Another title")
    await jobs.save_title_translation(job_id, "New title", metadata)
    assert "title_translation" not in (await jobs.get(job_id)).post_data["metadata"]
    assert "title_translation_provider" not in (await jobs.get(job_id)).post_data["metadata"]


async def test_race_between_clear_and_enqueue_never_queues_empty_album(context):
    jobs, editor, job_id, _ = context
    await asyncio.gather(editor.change(job_id, 0, "clear"), jobs.enqueue(job_id), return_exceptions=True)
    job = await jobs.get(job_id)
    assert job.status == JobStatus.WAITING_CONFIRMATION or len(job.post_data["media_items"]) > 0


async def test_disabled_limit_allows_multiple_telegram_groups(context):
    jobs, _, job_id, _ = context
    editor = AlbumEditor(jobs, limit=None)
    job = await editor.change(job_id, 0, "order", order=",".join(map(str, range(1, 16))))
    assert len(job.post_data["media_items"]) == 15


async def test_news_is_not_editable_by_artwork_editor():
    jobs = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
        status=JobStatus.WAITING_CONFIRMATION, content_kind=ContentKind.NEWS,
    )))
    with pytest.raises(InvalidMediaSelectionError):
        await AlbumEditor(jobs).get(1)


def test_legacy_subset_keeps_source_numbers_and_round_trips_to_new_format():
    post = _post("pixiv", 3)
    post.metadata.update(original_media_count=12, selected_media_numbers=[2, 7, 9])
    data = select_album(serialize_post(post), [9, 2], limit=10)
    assert read_album(data).selection == [9, 2]
    assert read_album(data).original_count == 12
    assert sorted(read_album(data).catalog) == [2, 7, 9]
