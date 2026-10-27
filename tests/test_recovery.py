from datetime import timedelta

from sqlalchemy import inspect, select, text

from app.db.models import Channel, Job, User, utcnow
from app.db.session import create_database, create_schema
from app.domain.enums import JobStatus
from app.services.job_service import JobService


async def test_existing_database_gets_additive_columns(tmp_path):
    engine, _ = create_database(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    await create_schema(engine)
    async with engine.begin() as connection:
        # A real pre-web schema has no version journal.
        await connection.execute(text("DROP TABLE schema_migrations"))
        await connection.execute(text("DROP INDEX ix_channels_active_job_id"))
        await connection.execute(text("ALTER TABLE channels DROP COLUMN active_job_id"))
        await connection.execute(text("ALTER TABLE channels DROP COLUMN is_paused"))
        await connection.execute(text("DROP INDEX ix_jobs_queue_position"))
        await connection.execute(text("DROP INDEX ix_jobs_scheduled_at"))
        await connection.execute(text("ALTER TABLE jobs DROP COLUMN caption_override"))
        await connection.execute(text("ALTER TABLE jobs DROP COLUMN queue_position"))
        await connection.execute(text("ALTER TABLE jobs DROP COLUMN scheduled_at"))

    await create_schema(engine)

    async with engine.begin() as connection:
        channel_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns("channels")
            }
        )
        job_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns("jobs")
            }
        )
        tables = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
    assert "active_job_id" in channel_columns
    assert "is_paused" in channel_columns
    assert "caption_override" in job_columns
    assert "queue_position" in job_columns
    assert "scheduled_at" in job_columns
    assert "content_kind" in job_columns
    assert "news_tasks" in tables
    await engine.dispose()


async def test_startup_migrates_legacy_queued_exact_times_to_scheduled(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'scheduled-status.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="main", telegram_chat_id="-1001", title="Main")
        session.add_all([user, channel])
        await session.flush()
        job = Job(
            created_by_user_id=user.id, provider="direct", source_id="legacy-scheduled",
            source_url="https://x/scheduled.jpg", normalized_url="https://x/scheduled.jpg",
            target_channel_id=channel.id, status=JobStatus.QUEUED,
            scheduled_at=utcnow() + timedelta(hours=1), queue_position=7,
            post_data={}, user_tags=[], source_tags=[],
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE schema_migrations"))

    await create_schema(engine)

    async with sessions() as session:
        stored = await session.get(Job, job_id)
    assert stored.status == JobStatus.SCHEDULED
    assert stored.queue_position is None
    await engine.dispose()


async def test_startup_recovery_handles_fresh_jobs_and_releases_channel_leases(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channels = [
            Channel(alias=f"channel-{index}", telegram_chat_id=f"-100{index}", title=f"Channel {index}")
            for index in range(4)
        ]
        session.add_all([user, *channels])
        await session.flush()
        jobs = [
            Job(
                created_by_user_id=user.id, provider="direct", source_id=f"job-{index}",
                source_url=f"https://x/{index}.jpg", normalized_url=f"https://x/{index}.jpg",
                target_channel_id=channels[index].id, status=status,
                post_data={}, user_tags=[], source_tags=[], cancel_requested=cancel_requested,
            )
            for index, (status, cancel_requested) in enumerate(
                [
                    (JobStatus.DOWNLOADING, False),
                    (JobStatus.PROCESSING, False),
                    (JobStatus.PUBLISHING, False),
                    (JobStatus.DOWNLOADING, True),
                ]
            )
        ]
        jobs[1].scheduled_at = utcnow() + timedelta(hours=1)
        session.add_all(jobs)
        await session.flush()
        for channel, job in zip(channels, jobs, strict=True):
            channel.active_job_id = job.id
        job_ids = [job.id for job in jobs]

    recovered = await JobService(sessions).recover()

    async with sessions() as session:
        stored_jobs = list(
            (await session.scalars(select(Job).where(Job.id.in_(job_ids)).order_by(Job.id))).all()
        )
        stored_channels = list(
            (await session.scalars(select(Channel).order_by(Channel.id))).all()
        )

    assert recovered == 4
    assert [job.status for job in stored_jobs] == [
        JobStatus.QUEUED,
        JobStatus.SCHEDULED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    ]
    assert stored_jobs[0].error_code == "recovered_after_restart"
    assert stored_jobs[1].error_code == "recovered_after_restart"
    assert stored_jobs[2].error_code == "uncertain_publish"
    assert stored_jobs[2].finished_at is not None
    assert stored_jobs[3].finished_at is not None
    assert all(channel.active_job_id is None for channel in stored_channels)
    await engine.dispose()


async def test_timed_recovery_does_not_take_over_fresh_live_job(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'watchdog.db'}")
    await create_schema(engine)
    async with sessions() as session, session.begin():
        user = User(telegram_user_id=1)
        channel = Channel(alias="main", telegram_chat_id="-1001", title="Main")
        session.add_all([user, channel])
        await session.flush()
        job = Job(
            created_by_user_id=user.id, provider="direct", source_id="fresh",
            source_url="https://x/fresh.jpg", normalized_url="https://x/fresh.jpg",
            target_channel_id=channel.id, status=JobStatus.DOWNLOADING,
            post_data={}, user_tags=[], source_tags=[],
        )
        session.add(job)
        await session.flush()
        channel.active_job_id = job.id
        job_id = job.id

    assert await JobService(sessions).recover(stale_minutes=15) == 0

    async with sessions() as session:
        stored = await session.get(Job, job_id)
        stored_channel = await session.get(Channel, channel.id)
    assert stored.status == JobStatus.DOWNLOADING
    assert stored_channel.active_job_id == job_id
    await engine.dispose()
