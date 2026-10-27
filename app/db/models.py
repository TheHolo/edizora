from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session
from sqlalchemy import event, inspect as orm_inspect

from app.domain.enums import ContentKind, JobStatus, NewsTaskStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    last_selected_channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Channel(Base):
    __tablename__ = "channels"
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __mapper_args__ = {"version_id_col":version,"version_id_generator":False}
    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(255))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_mode: Mapped[str] = mapped_column(String(16), default="auto")
    publish_interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    next_publish_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_job_id: Mapped[int | None] = mapped_column(Integer, index=True)
    caption_template: Mapped[str | None] = mapped_column(Text)
    default_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChannelMemberSnapshot(Base):
    __tablename__ = "channel_member_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "channel_id", "source", "snapshot_date",
            name="uq_channel_member_snapshot_daily_source",
        ),
        Index(
            "ix_channel_member_snapshots_channel_captured",
            "channel_id", "captured_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    member_count: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))
    snapshot_date: Mapped[date | None] = mapped_column(Date)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Job(Base):
    __tablename__ = "jobs"
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __mapper_args__ = {"version_id_col":version,"version_id_generator":False}
    origin_ai_task_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    render_plan: Mapped[dict | None] = mapped_column(JSON)
    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    content_kind: Mapped[str] = mapped_column(
        String(32), default=ContentKind.ARTWORK, index=True,
    )
    source_id: Mapped[str] = mapped_column(String(255), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    target_channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.WAITING_CONFIRMATION, index=True)
    user_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    post_data: Mapped[dict] = mapped_column(JSON, default=dict)
    caption_override: Mapped[str | None] = mapped_column(Text)
    queue_position: Mapped[int | None] = mapped_column(Integer, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    force_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    channel: Mapped[Channel] = relationship()
    media_items: Mapped[list["MediaRecord"]] = relationship(cascade="all, delete-orphan")


class NewsTask(Base):
    __tablename__ = "news_tasks"
    __table_args__ = (
        Index("ix_news_tasks_claim", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    target_channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    origin_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="news", server_default="news")
    operation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    lease_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    source_job_version: Mapped[int | None] = mapped_column(Integer)
    status_message_id: Mapped[int | None] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    user_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(
        String(32), default=NewsTaskStatus.QUEUED, index=True,
    )
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    stage_message: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(String(128))
    lease_owner: Mapped[str | None] = mapped_column(String(128), index=True)
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[Channel] = relationship()
    job: Mapped[Job | None] = relationship()


class MediaRecord(Base):
    __tablename__ = "media_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    prepared_path: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    media_type: Mapped[str] = mapped_column(String(32), default="image")
    size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer)
    download_status: Mapped[str] = mapped_column(String(32), default="pending")
    publish_status: Mapped[str] = mapped_column(String(32), default="pending")
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("job_id", "channel_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(64))
    telegram_message_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    caption: Mapped[str] = mapped_column(Text)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    old_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApplicationState(Base):
    __tablename__ = "application_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    system_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, default=1)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    publication_blocks: Mapped[dict] = mapped_column(JSON, default=dict)
    maintenance_state: Mapped[str] = mapped_column(String(32), default="normal")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class QueueRevision(Base):
    __tablename__ = "queue_revisions"
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    stage_key: Mapped[str] = mapped_column(String(64), default="accepted")
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    stages: Mapped[list] = mapped_column(JSON, default=list)
    message: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_event_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationEvent(Base):
    __tablename__ = "operation_events"
    __table_args__ = (UniqueConstraint("operation_id", "sequence"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("operations.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stage_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str | None] = mapped_column(Text)
    progress: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (UniqueConstraint("principal_id", "scope", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(128))
    key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(32))
    source_reference: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="remote")
    thumbnail_key: Mapped[str | None] = mapped_column(Text)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobAsset(Base):
    __tablename__ = "job_assets"
    __table_args__ = (UniqueConstraint("job_id", "asset_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id"), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(32), default="original")


class TelegramInbox(Base):
    __tablename__ = "telegram_inbox"
    __table_args__ = (UniqueConstraint("bot_id", "update_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bot_id: Mapped[str] = mapped_column(String(64))
    update_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelegramState(Base):
    __tablename__ = "telegram_states"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class PublicationAttempt(Base):
    __tablename__ = "publication_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job_version: Mapped[int] = mapped_column(Integer)
    node_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    render_plan: Mapped[dict] = mapped_column(JSON)
    stage: Mapped[str] = mapped_column(String(32), default="sending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublicationPart(Base):
    __tablename__ = "publication_parts"
    __table_args__ = (UniqueConstraint("attempt_id", "sequence"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("publication_attempts.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    request_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    message_ids: Mapped[list] = mapped_column(JSON, default=list)
    before_send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIWorker(Base):
    __tablename__ = "ai_workers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    models: Mapped[list] = mapped_column(JSON, default=list)
    selected_model: Mapped[str | None] = mapped_column(String(255))
    credential_hash: Mapped[str] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


@event.listens_for(Session, "before_flush")
def _bump_versions(session, flush_context, instances):
    # Includes ORM writes from existing Telegram, AI and worker clients.
    for record in session.dirty:
        if isinstance(record, (Job, Channel)) and session.is_modified(record, include_collections=False):
            state = orm_inspect(record)
            if not state.attrs.version.history.has_changes():
                record.version = (record.version or 1) + 1
            if isinstance(record, Job):
                content_changed = any(state.attrs[name].history.has_changes() for name in ("user_tags", "source_tags", "target_channel_id", "caption_override"))
                post_history=state.attrs.post_data.history
                if post_history.has_changes():
                    def render_input(value):
                        if not isinstance(value,dict):return value
                        return {**value,"metadata":{k:v for k,v in value.get("metadata",{}).items() if k not in {"overdue_hold","overdue_catchup","overdue_policy_applied"}}}
                    content_changed = content_changed or not post_history.deleted or render_input(post_history.deleted[0])!=render_input(record.post_data)
                if content_changed and not state.attrs.render_plan.history.has_changes():
                    record.render_plan = None
                elif record.render_plan is not None:
                    record.render_plan = {**record.render_plan, "job_version": record.version}
