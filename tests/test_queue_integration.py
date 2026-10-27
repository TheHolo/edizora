import asyncio
from datetime import timedelta

from app.db.models import Channel, Job, Publication, User, utcnow
from app.db.session import create_database, create_schema
from app.domain.enums import JobStatus
from app.domain.models import MediaItem, SourcePost
from app.services.job_service import JobService
from app.utils.queue_schedule import next_queued_by_schedule


async def test_claim_is_atomic(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="main", telegram_chat_id="-1001", title="Main", is_default=True)
        session.add_all([user, channel])
        await session.flush()
        session.add(Job(
            created_by_user_id=user.id, provider="direct", source_id="x", source_url="https://x/a.jpg",
            normalized_url="https://x/a.jpg", target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[],
        ))
    jobs = JobService(sessions)
    first, second = await asyncio.gather(jobs.claim_next(), jobs.claim_next())
    assert sum(item is not None for item in (first, second)) == 1
    claimed = first or second
    assert claimed.status == JobStatus.DOWNLOADING
    assert claimed.attempts == 1
    await engine.dispose()


async def test_only_one_job_per_channel_can_be_active(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'channel-lease.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(
            alias="main", telegram_chat_id="-1001", title="Main",
            publish_interval_seconds=900,
        )
        session.add_all([user, channel])
        await session.flush()
        common = {
            "created_by_user_id": user.id, "provider": "direct",
            "source_url": "https://x/a.jpg", "normalized_url": "https://x/a.jpg",
            "target_channel_id": channel.id, "status": JobStatus.QUEUED,
            "post_data": {}, "user_tags": [], "source_tags": [],
        }
        session.add_all([
            Job(source_id="first", **common),
            Job(source_id="second", **common),
        ])

    jobs = JobService(sessions)
    first = await jobs.claim_next()
    assert first is not None
    assert await jobs.claim_next() is None

    await jobs.transition(first.id, JobStatus.COMPLETED)
    second = await jobs.claim_next()
    assert second is not None and second.id != first.id
    await engine.dispose()


async def test_job_waits_for_its_channel_interval(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'delayed.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(
            alias="delayed", telegram_chat_id="-1001", title="Delayed",
            is_default=True, publish_interval_seconds=900,
            next_publish_at=utcnow() + timedelta(minutes=15),
        )
        session.add_all([user, channel])
        await session.flush()
        session.add(Job(
            created_by_user_id=user.id, provider="direct", source_id="x", source_url="https://x/a.jpg",
            normalized_url="https://x/a.jpg", target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[],
        ))
    assert await JobService(sessions).claim_next() is None
    await engine.dispose()


async def test_manual_publish_bypasses_channel_interval(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'manual.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(
            alias="delayed", telegram_chat_id="-1001", title="Delayed",
            publish_interval_seconds=3600, next_publish_at=utcnow() + timedelta(hours=1),
        )
        session.add_all([user, channel])
        await session.flush()
        job = Job(
            created_by_user_id=user.id, provider="direct", source_id="manual",
            source_url="https://x/a.jpg", normalized_url="https://x/a.jpg",
            target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[],
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    jobs = JobService(sessions)
    forced = await jobs.force_publish(job_id)
    assert forced is not None and forced.force_publish
    claimed = await jobs.claim_next()
    assert claimed is not None and claimed.id == job_id
    await engine.dispose()


async def test_manual_publish_without_id_selects_oldest_queued_job(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'manual-next.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(
            alias="delayed", telegram_chat_id="-1001", title="Delayed",
            publish_interval_seconds=3600, next_publish_at=utcnow() + timedelta(hours=1),
        )
        session.add_all([user, channel])
        await session.flush()
        older = Job(
            created_by_user_id=user.id, provider="direct", source_id="older",
            source_url="https://x/older.jpg", normalized_url="https://x/older.jpg",
            target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[], created_at=utcnow() - timedelta(minutes=1),
        )
        newer = Job(
            created_by_user_id=user.id, provider="direct", source_id="newer",
            source_url="https://x/newer.jpg", normalized_url="https://x/newer.jpg",
            target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[], created_at=utcnow(),
        )
        session.add_all([newer, older])
        await session.flush()
        older_id = older.id

    jobs = JobService(sessions)
    forced = await jobs.force_next_publish()
    assert forced is not None and forced.id == older_id and forced.force_publish
    claimed = await jobs.claim_next()
    assert claimed is not None and claimed.id == older_id
    await engine.dispose()


async def test_preview_without_id_reads_oldest_queued_job_without_changing_it(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'preview-next.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="main", telegram_chat_id="-1001", title="Main", is_default=True)
        session.add_all([user, channel])
        await session.flush()
        older = Job(
            created_by_user_id=user.id, provider="direct", source_id="older-preview",
            source_url="https://x/older.jpg", normalized_url="https://x/older.jpg",
            target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[], created_at=utcnow() - timedelta(minutes=1),
        )
        newer = Job(
            created_by_user_id=user.id, provider="direct", source_id="newer-preview",
            source_url="https://x/newer.jpg", normalized_url="https://x/newer.jpg",
            target_channel_id=channel.id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[], created_at=utcnow(),
        )
        session.add_all([newer, older])
        await session.flush()
        older_id = older.id

    jobs = JobService(sessions)
    rows = await jobs.queue(limit=None)
    selected = next_queued_by_schedule(rows)
    assert selected is not None
    previewed = selected[0]
    assert previewed.id == older_id
    unchanged = await jobs.get(older_id)
    assert unchanged is not None and unchanged.status == JobStatus.QUEUED
    assert unchanged.force_publish is False
    await engine.dispose()


async def test_queue_can_be_filtered_by_channel_alias(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'queue-alias.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        first = Channel(alias="first", telegram_chat_id="-1001", title="First", is_default=True)
        second = Channel(alias="second", telegram_chat_id="-1002", title="Second")
        empty = Channel(alias="empty", telegram_chat_id="-1003", title="Empty")
        session.add_all([user, first, second, empty])
        await session.flush()
        session.add_all([
            Job(
                created_by_user_id=user.id, provider="direct", source_id="first-job",
                source_url="https://x/first.jpg", normalized_url="https://x/first.jpg",
                target_channel_id=first.id, status=JobStatus.QUEUED,
                post_data={}, user_tags=[], source_tags=[],
            ),
            Job(
                created_by_user_id=user.id, provider="direct", source_id="second-job",
                source_url="https://x/second.jpg", normalized_url="https://x/second.jpg",
                target_channel_id=second.id, status=JobStatus.QUEUED,
                post_data={}, user_tags=[], source_tags=[],
            ),
        ])

    jobs = JobService(sessions)
    filtered = await jobs.queue("second")

    assert filtered is not None and len(filtered) == 1
    assert filtered[0].source_id == "second-job"
    assert filtered[0].channel.alias == "second"
    assert await jobs.queue("empty") == []
    assert await jobs.queue("missing") is None
    await engine.dispose()


async def test_last_selected_channel_is_reused_and_can_be_changed(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'preferred-channel.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        default = Channel(alias="artwork", telegram_chat_id="-1001", title="Artwork", is_default=True)
        arknights = Channel(alias="arknights", telegram_chat_id="-1002", title="Arknights")
        session.add_all([user, default, arknights])
        await session.flush()
        user_id, default_id, arknights_id = user.id, default.id, arknights.id

    jobs = JobService(sessions)
    preferred = await jobs.get_preferred_channel(user_id, "artwork")
    assert preferred is not None and preferred.id == default_id

    post = SourcePost(
        provider="direct", source_id="preferred", source_url="https://x/a.jpg",
        normalized_url="https://x/a.jpg", title="A", author_name="Unknown",
        author_url="https://x", media_items=[],
    )
    job = await jobs.create_preview(user_id, post, default_id, [], 3)
    changed = await jobs.change_channel(job.id, arknights_id)
    assert changed is not None and changed.alias == "arknights"

    preferred = await jobs.get_preferred_channel(user_id, "artwork")
    assert preferred is not None and preferred.id == arknights_id
    await engine.dispose()


async def test_caption_override_can_be_changed_before_and_after_enqueue(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'caption.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="artwork", telegram_chat_id="-1001", title="Artwork")
        session.add_all([user, channel])
        await session.flush()
        user_id, channel_id = user.id, channel.id

    post = SourcePost(
        provider="direct", source_id="caption", source_url="https://x/a.jpg",
        normalized_url="https://x/a.jpg", title="A", author_name="Unknown",
        author_url="https://x", media_items=[MediaItem(url="https://x/a.jpg", filename="a.jpg", order=0)],
    )
    jobs = JobService(sessions)
    job = await jobs.create_preview(user_id, post, channel_id, [], 3)

    assert await jobs.set_caption_override(job.id, "Custom caption") is not None
    stored = await jobs.get(job.id)
    assert stored is not None and stored.caption_override == "Custom caption"

    assert await jobs.enqueue(job.id)
    assert await jobs.set_caption_override(job.id, None) is not None
    stored = await jobs.get(job.id)
    assert stored is not None and stored.caption_override is None
    await engine.dispose()


async def test_disabled_last_selected_channel_falls_back_to_default(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'preferred-fallback.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        default = Channel(alias="artwork", telegram_chat_id="-1001", title="Artwork", is_default=True)
        disabled = Channel(alias="old", telegram_chat_id="-1002", title="Old", is_enabled=False)
        session.add_all([default, disabled])
        await session.flush()
        user = User(telegram_user_id=1, last_selected_channel_id=disabled.id)
        session.add(user)
        await session.flush()
        user_id, default_id = user.id, default.id

    preferred = await JobService(sessions).get_preferred_channel(user_id, "artwork")
    assert preferred is not None and preferred.id == default_id
    await engine.dispose()


async def test_duplicate_check_includes_active_jobs_but_excludes_current_preview(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'active-duplicate.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="artwork", telegram_chat_id="-1001", title="Artwork")
        session.add_all([user, channel])
        await session.flush()
        current = Job(
            created_by_user_id=user.id, provider="pixiv", source_id="123",
            source_url="https://x/current", normalized_url="https://x/current",
            target_channel_id=channel.id, status=JobStatus.WAITING_CONFIRMATION,
            post_data={}, user_tags=[], source_tags=[],
        )
        session.add(current)
        await session.flush()
        user_id, channel_id, current_id = user.id, channel.id, current.id

    jobs = JobService(sessions)
    assert await jobs.duplicate_state_for("pixiv", "123", channel_id, current_id) is None

    async with sessions() as session, session.begin():
        duplicate = Job(
            created_by_user_id=user_id, provider="pixiv", source_id="123",
            source_url="https://x/queued", normalized_url="https://x/queued",
            target_channel_id=channel_id, status=JobStatus.QUEUED,
            post_data={}, user_tags=[], source_tags=[],
        )
        session.add(duplicate)

    assert await jobs.duplicate_state_for("pixiv", "123", channel_id, current_id) == "active"
    await engine.dispose()


async def test_duplicate_check_prioritizes_completed_publication(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'published-duplicate.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="artwork", telegram_chat_id="-1001", title="Artwork")
        session.add_all([user, channel])
        await session.flush()
        published = Job(
            created_by_user_id=user.id, provider="pixiv", source_id="123",
            source_url="https://x/published", normalized_url="https://x/published",
            target_channel_id=channel.id, status=JobStatus.COMPLETED,
            post_data={}, user_tags=[], source_tags=[],
        )
        current = Job(
            created_by_user_id=user.id, provider="pixiv", source_id="123",
            source_url="https://x/current", normalized_url="https://x/current",
            target_channel_id=channel.id, status=JobStatus.WAITING_CONFIRMATION,
            post_data={}, user_tags=[], source_tags=[],
        )
        session.add_all([published, current])
        await session.flush()
        session.add(Publication(
            job_id=published.id, channel_id=channel.id, telegram_chat_id=channel.telegram_chat_id,
            telegram_message_ids=[1], caption="caption",
        ))
        channel_id, current_id = channel.id, current.id

    assert await JobService(sessions).duplicate_state_for("pixiv", "123", channel_id, current_id) == "published"
    await engine.dispose()
