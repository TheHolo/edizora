from datetime import timedelta

from sqlalchemy import select

from app.db.models import Channel, Job, User, utcnow
from app.db.session import create_database, create_schema
from app.domain.enums import JobStatus
from app.domain.models import SourcePost
from app.services.job_service import JobService, serialize_post


async def create_context(tmp_path, name: str):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / name}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(
            alias="artwork", telegram_chat_id="-1001", title="Artwork", is_default=True,
        )
        session.add_all([user, channel])
        await session.flush()
        user_id, channel_id = user.id, channel.id
    return engine, sessions, user_id, channel_id


def queued_job(user_id: int, channel_id: int, source_id: str, position: int) -> Job:
    return Job(
        created_by_user_id=user_id,
        provider="direct",
        source_id=source_id,
        source_url=f"https://example.com/{source_id}.jpg",
        normalized_url=f"https://example.com/{source_id}.jpg",
        target_channel_id=channel_id,
        status=JobStatus.QUEUED,
        post_data={},
        user_tags=[],
        source_tags=[],
        queue_position=position,
    )


async def test_paused_channel_blocks_automatic_claim_but_manual_publish_bypasses_pause(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "pause.db")
    async with sessions() as session, session.begin():
        channel = await session.get(Channel, channel_id)
        channel.is_paused = True
        session.add(queued_job(user_id, channel_id, "paused", 1))

    jobs = JobService(sessions)
    assert await jobs.claim_next() is None

    forced = await jobs.force_next_publish(channel_id)
    assert forced is not None
    claimed = await jobs.claim_next()
    assert claimed is not None and claimed.id == forced.id
    await engine.dispose()


async def test_queued_jobs_can_be_moved_up_and_down_atomically(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "move.db")
    async with sessions() as session, session.begin():
        rows = [
            queued_job(user_id, channel_id, "first", 1),
            queued_job(user_id, channel_id, "second", 2),
            queued_job(user_id, channel_id, "third", 3),
        ]
        session.add_all(rows)
        await session.flush()
        third_id = rows[2].id

    jobs = JobService(sessions)
    assert await jobs.move_queued(third_id, "up") is not None
    assert await jobs.move_queued(third_id, "up") is not None

    ordered = await jobs.managed_queue(channel_id, "queued", limit=None)
    assert [job.source_id for job in ordered] == ["third", "first", "second"]
    assert [job.queue_position for job in ordered] == [1, 2, 3]
    await engine.dispose()


async def test_future_exact_time_blocks_claim_until_forced(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "schedule.db")
    async with sessions() as session, session.begin():
        job = queued_job(user_id, channel_id, "scheduled", 1)
        job.status = JobStatus.SCHEDULED
        job.queue_position = None
        job.scheduled_at = utcnow() + timedelta(hours=1)
        session.add(job)
        await session.flush()
        job_id = job.id

    jobs = JobService(sessions)
    assert await jobs.claim_next() is None
    assert await jobs.force_publish(job_id) is not None
    claimed = await jobs.claim_next()
    assert claimed is not None and claimed.id == job_id
    await engine.dispose()


async def test_scheduled_job_bypasses_channel_interval_when_due(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "due-schedule.db")
    async with sessions() as session, session.begin():
        channel = await session.get(Channel, channel_id)
        channel.next_publish_at = utcnow() + timedelta(hours=2)
        job = queued_job(user_id, channel_id, "due", 1)
        job.status = JobStatus.SCHEDULED
        job.queue_position = None
        job.scheduled_at = utcnow() - timedelta(seconds=1)
        session.add(job)
        await session.flush()
        job_id = job.id

    claimed = await JobService(sessions).claim_next()

    assert claimed is not None and claimed.id == job_id
    await engine.dispose()


async def test_setting_exact_time_moves_job_out_of_regular_queue_and_clear_restores_it(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "set-schedule.db")
    async with sessions() as session, session.begin():
        first = queued_job(user_id, channel_id, "first", 1)
        second = queued_job(user_id, channel_id, "second", 2)
        session.add_all([first, second])
        await session.flush()
        first_id = first.id

    jobs = JobService(sessions)
    scheduled = await jobs.set_schedule(first_id, utcnow() + timedelta(hours=1))

    assert scheduled is not None and scheduled.status == JobStatus.SCHEDULED
    assert scheduled.queue_position is None
    assert [job.source_id for job in await jobs.managed_queue(channel_id, "queued", None)] == ["second"]
    assert [job.source_id for job in await jobs.managed_queue(channel_id, "scheduled", None)] == ["first"]

    restored = await jobs.set_schedule(first_id, None)
    assert restored is not None and restored.status == JobStatus.QUEUED
    assert [job.source_id for job in await jobs.managed_queue(channel_id, "queued", None)] == [
        "second", "first",
    ]
    await engine.dispose()


async def test_preview_with_exact_time_enters_scheduled_status_on_confirmation(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "preview-schedule.db")
    async with sessions() as session, session.begin():
        preview = queued_job(user_id, channel_id, "preview", 1)
        preview.post_data = {"media_items": [{"url": "https://example.com/a.jpg", "filename": "a.jpg", "order": 0}]}
        preview.status = JobStatus.WAITING_CONFIRMATION
        preview.queue_position = None
        session.add(preview)
        await session.flush()
        preview_id = preview.id

    jobs = JobService(sessions)
    scheduled_at = utcnow() + timedelta(hours=1)
    edited = await jobs.set_schedule(preview_id, scheduled_at)
    assert edited is not None and edited.status == JobStatus.WAITING_CONFIRMATION
    assert await jobs.enqueue(preview_id)

    stored = await jobs.get(preview_id)
    assert stored is not None and stored.status == JobStatus.SCHEDULED
    assert stored.queue_position is None
    assert stored.scheduled_at == scheduled_at.replace(tzinfo=None)
    await engine.dispose()


async def test_shuffle_changes_only_regular_waiting_jobs(tmp_path, monkeypatch):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "shuffle.db")
    async with sessions() as session, session.begin():
        regular = [
            queued_job(user_id, channel_id, source_id, position)
            for position, source_id in enumerate(("first", "second", "third"), start=1)
        ]
        scheduled = queued_job(user_id, channel_id, "scheduled", 4)
        scheduled.status = JobStatus.SCHEDULED
        scheduled.queue_position = None
        scheduled.scheduled_at = utcnow() + timedelta(hours=1)
        cancelled = queued_job(user_id, channel_id, "cancelled", 5)
        cancelled.status = JobStatus.CANCELLED
        completed = queued_job(user_id, channel_id, "completed", 6)
        completed.status = JobStatus.COMPLETED
        session.add_all([*regular, scheduled, cancelled, completed])
        await session.flush()
        untouched = {
            scheduled.id: (
                str(scheduled.status), scheduled.queue_position,
                scheduled.scheduled_at.replace(tzinfo=None),
            ),
            cancelled.id: (str(cancelled.status), cancelled.queue_position, cancelled.scheduled_at),
            completed.id: (str(completed.status), completed.queue_position, completed.scheduled_at),
        }

    monkeypatch.setattr("app.services.job_service.random.shuffle", lambda rows: rows.reverse())
    jobs = JobService(sessions)
    assert await jobs.shuffle_queued(channel_id) == 3
    assert [job.source_id for job in await jobs.managed_queue(channel_id, "queued", None)] == [
        "third", "second", "first",
    ]
    async with sessions() as session:
        stored = list((await session.scalars(select(Job).where(Job.id.in_(untouched)))).all())
    assert {
        job.id: (str(job.status), job.queue_position, job.scheduled_at) for job in stored
    } == untouched
    await engine.dispose()


async def test_bulk_retry_skips_uncertain_publications_and_bulk_cancel_uses_filter(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "bulk.db")
    async with sessions() as session, session.begin():
        retryable = queued_job(user_id, channel_id, "retryable", 1)
        retryable.status = JobStatus.FAILED
        retryable.error_code = "download_error"
        uncertain = queued_job(user_id, channel_id, "uncertain", 2)
        uncertain.status = JobStatus.FAILED
        uncertain.error_code = "uncertain_publish"
        already_queued = queued_job(user_id, channel_id, "queued", 3)
        session.add_all([retryable, uncertain, already_queued])
        await session.flush()
        retryable_id, uncertain_id = retryable.id, uncertain.id

    jobs = JobService(sessions)
    result = await jobs.retry_failed(channel_id)
    assert result.retried == 1
    assert result.skipped_uncertain == 1
    assert (await jobs.get(retryable_id)).status == JobStatus.QUEUED
    assert (await jobs.get(uncertain_id)).status == JobStatus.FAILED

    assert await jobs.cancel_filtered(channel_id, "queued") == 2
    assert (await jobs.get(retryable_id)).status == JobStatus.CANCELLED
    await engine.dispose()


async def test_channel_runtime_settings_and_edited_post_metadata_are_persisted(tmp_path):
    engine, sessions, user_id, channel_id = await create_context(tmp_path, "channel.db")
    async with sessions() as session, session.begin():
        archive = Channel(
            alias="archive", telegram_chat_id="-1002", title="Archive", is_default=False,
        )
        session.add(archive)
        await session.flush()
        archive_id = archive.id

    jobs = JobService(sessions)
    assert (await jobs.set_channel_paused(channel_id, True)).is_paused is True
    assert (await jobs.set_channel_interval_by_id(channel_id, 900)).publish_interval_seconds == 900
    assert (await jobs.set_default_channel(archive_id)).is_default is True
    assert (await jobs.get_channel_by_id(channel_id)).is_default is False
    assert (await jobs.get_preferred_channel(user_id, "artwork")).id == archive_id

    post = SourcePost(
        provider="direct",
        source_id="metadata",
        source_url="https://example.com/a.jpg",
        normalized_url="https://example.com/a.jpg",
        title="Old title",
        description="Old description",
        author_name="Source",
        author_url="https://example.com",
        media_items=[],
        metadata={"title_translation": "Old translation"},
    )
    async with sessions() as session, session.begin():
        job = Job(
            created_by_user_id=user_id,
            provider=post.provider,
            source_id=post.source_id,
            source_url=post.source_url,
            normalized_url=post.normalized_url,
            target_channel_id=channel_id,
            status=JobStatus.WAITING_CONFIRMATION,
            post_data=serialize_post(post),
            user_tags=[],
            source_tags=[],
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    assert await jobs.set_post_field(job_id, "title", "New title") is not None
    assert await jobs.set_post_field(job_id, "description", "New description") is not None
    stored = await jobs.get(job_id)
    assert stored.post_data["title"] == "New title"
    assert stored.post_data["description"] == "New description"
    assert "title_translation" not in stored.post_data["metadata"]
    await engine.dispose()
