"""Explicit, serialized SQLite migration journal; does not stamp unknown schemas."""
import sqlite3
from pathlib import Path
from datetime import UTC, datetime
from sqlalchemy import inspect, text
from app.db.models import Base

SCHEMA_VERSION = 3


def backup_before_upgrade(url):
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    path = Path(url.database)
    if not path.exists() or not path.stat().st_size:
        return
    with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" in tables:
            version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
            if version >= SCHEMA_VERSION:
                return
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + f".pre-web-{suffix}.bak")
        with sqlite3.connect(backup) as target:
            db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Pre-upgrade backup integrity check failed")


def upgrade(connection):
    from app.db.session import _upgrade_schema
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    legacy = {"users": {"id", "telegram_user_id"}, "channels": {"id", "alias", "telegram_chat_id"},
              "jobs": {"id", "created_by_user_id", "post_data", "status", "target_channel_id"}}
    if tables:
        if not set(legacy) <= tables:
            raise RuntimeError("Unknown database schema: Publisher base tables are missing")
        for table, required in legacy.items():
            if not required <= {c["name"] for c in inspector.get_columns(table)}:
                raise RuntimeError(f"Unknown legacy table layout: {table}")
    if "schema_migrations" in tables:
        current = connection.execute(text("SELECT COALESCE(MAX(version),0) FROM schema_migrations")).scalar()
        if current > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this release")
    else:
        current = 0
    if current == SCHEMA_VERSION:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                raise RuntimeError(f"Current schema is incomplete: {table.name}")
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            if not {column.name for column in table.columns} <= actual:
                raise RuntimeError(f"Current schema has missing columns: {table.name}")
    # Transaction-level write lock is taken before inspecting/upgrading mutable schema.
    if connection.dialect.name == "sqlite":
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"))
    else:
        raise RuntimeError("This managed release supports SQLite only")
    if current < 1:
        # Existing additive schemas are normalized before their baseline is recorded.
        Base.metadata.create_all(connection)
        _upgrade_schema(connection)
        connection.execute(text("INSERT INTO schema_migrations VALUES (1, :at)"), {"at": datetime.now(UTC).isoformat()})
    if current < 2:
        additions = {
            "jobs": {"version": "INTEGER NOT NULL DEFAULT 1", "origin_ai_task_id": "INTEGER NULL", "render_plan": "JSON NULL"},
            "channels": {"version": "INTEGER NOT NULL DEFAULT 1"},
            "news_tasks": {"kind": "VARCHAR(32) NOT NULL DEFAULT 'news'", "operation_id": "VARCHAR(36) NULL", "lease_generation": "INTEGER NOT NULL DEFAULT 1", "source_job_version": "INTEGER NULL"},
        }
        for table, fields in additions.items():
            columns = {c["name"] for c in inspect(connection).get_columns(table)}
            for name, declaration in fields.items():
                if name not in columns:
                    connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}'))
        # Rebuild only the legacy NOT NULL column; preserve all known and extra columns/indexes.
        columns = inspect(connection).get_columns("news_tasks")
        if any(c["name"] == "origin_chat_id" and not c["nullable"] for c in columns):
            sql = connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='news_tasks'")).scalar()
            import re
            sql = re.sub(r'CREATE TABLE\s+(?:"news_tasks"|news_tasks)', 'CREATE TABLE news_tasks__upgrade', sql, count=1, flags=re.I)
            sql = re.sub(r'(\borigin_chat_id\b\s+VARCHAR\(64\))\s+NOT NULL', r'\1', sql, flags=re.I)
            indexes = list(connection.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='news_tasks' AND sql IS NOT NULL")).scalars())
            connection.execute(text(sql))
            names = ",".join('"' + c["name"] + '"' for c in columns)
            connection.execute(text(f"INSERT INTO news_tasks__upgrade ({names}) SELECT {names} FROM news_tasks"))
            connection.execute(text("DROP TABLE news_tasks"))
            connection.execute(text("ALTER TABLE news_tasks__upgrade RENAME TO news_tasks"))
            for index in indexes:
                connection.execute(text(index))
        Base.metadata.create_all(connection)
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_origin_ai_task ON jobs(origin_ai_task_id) WHERE origin_ai_task_id IS NOT NULL"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_list ON jobs(status,target_channel_id,updated_at,id)"))
        for table in ("jobs", "channels"):
            # Covers direct SQL writes in legacy services as well as ORM updates.
            connection.execute(text(f"CREATE TRIGGER IF NOT EXISTS {table}_version AFTER UPDATE ON {table} WHEN NEW.version = OLD.version BEGIN UPDATE {table} SET version=OLD.version+1 WHERE id=NEW.id; END"))
        for action, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            connection.execute(text(f"CREATE TRIGGER IF NOT EXISTS jobs_queue_{action.lower()} AFTER {action} ON jobs BEGIN INSERT INTO queue_revisions(channel_id,revision) VALUES ({row}.target_channel_id,1) ON CONFLICT(channel_id) DO UPDATE SET revision=revision+1; END"))
        connection.execute(text("INSERT INTO schema_migrations VALUES (2, :at)"), {"at": datetime.now(UTC).isoformat()})
    if current < 3:
        Base.metadata.create_all(connection)
        connection.execute(text("INSERT INTO schema_migrations VALUES (3, :at)"), {"at": datetime.now(UTC).isoformat()})
    violations = list(connection.execute(text("PRAGMA foreign_key_check")))
    if violations:
        raise RuntimeError("Database contains broken foreign-key references; restore or repair the isolated copy")
