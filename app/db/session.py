from pathlib import Path

from sqlalchemy import inspect, text, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base


def create_database(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    if url.startswith("sqlite") and "///" in url:
        path = url.split("///", 1)[1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    from app.db.migrations import backup_before_upgrade, upgrade
    backup_before_upgrade(engine.url)
    async with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            await connection.execute(text("BEGIN IMMEDIATE"))
        await connection.run_sync(upgrade)


def _upgrade_schema(connection) -> None:
    """Apply additive MVP migrations to databases created by older versions."""
    user_columns = {column["name"] for column in inspect(connection).get_columns("users")}
    if "last_selected_channel_id" not in user_columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN last_selected_channel_id INTEGER NULL"))
    columns = {column["name"] for column in inspect(connection).get_columns("channels")}
    if "default_tags" not in columns:
        connection.execute(text("ALTER TABLE channels ADD COLUMN default_tags JSON NOT NULL DEFAULT '[]'"))
    if "publish_interval_seconds" not in columns:
        connection.execute(text("ALTER TABLE channels ADD COLUMN publish_interval_seconds INTEGER NOT NULL DEFAULT 0"))
    if "next_publish_at" not in columns:
        connection.execute(text("ALTER TABLE channels ADD COLUMN next_publish_at TIMESTAMP NULL"))
    if "active_job_id" not in columns:
        connection.execute(text("ALTER TABLE channels ADD COLUMN active_job_id INTEGER NULL"))
    if "is_paused" not in columns:
        connection.execute(text("ALTER TABLE channels ADD COLUMN is_paused BOOLEAN NOT NULL DEFAULT FALSE"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_channels_active_job_id ON channels (active_job_id)"))
    job_columns = {column["name"] for column in inspect(connection).get_columns("jobs")}
    if "content_kind" not in job_columns:
        connection.execute(text(
            "ALTER TABLE jobs ADD COLUMN content_kind VARCHAR(32) "
            "NOT NULL DEFAULT 'artwork'"
        ))
    if "force_publish" not in job_columns:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN force_publish BOOLEAN NOT NULL DEFAULT FALSE"))
    if "caption_override" not in job_columns:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN caption_override TEXT NULL"))
    if "queue_position" not in job_columns:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN queue_position INTEGER NULL"))
        connection.execute(text("UPDATE jobs SET queue_position = id WHERE queue_position IS NULL"))
    if "scheduled_at" not in job_columns:
        connection.execute(text("ALTER TABLE jobs ADD COLUMN scheduled_at TIMESTAMP NULL"))
    connection.execute(text(
        "UPDATE jobs SET status = 'scheduled', queue_position = NULL "
        "WHERE status = 'queued' AND scheduled_at IS NOT NULL AND force_publish = FALSE"
    ))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_queue_position ON jobs (queue_position)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_scheduled_at ON jobs (scheduled_at)"))
    connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_content_kind ON jobs (content_kind)"))
