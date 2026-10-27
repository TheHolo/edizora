import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql import ColumnElement

from app.db.models import Channel, Job, JobEvent, Publication, User, ApplicationState, CommandReceipt, utcnow
from app.domain.enums import ACTIVE_JOB_STATUSES, ContentKind, JobStatus
from app.domain.models import MediaItem, SourcePost
from app.services.translation_service import TRANSLATION_FIELDS

QUEUE_FILTER_STATUSES = {
    "active": ACTIVE_JOB_STATUSES,
    "queued": {JobStatus.QUEUED},
    "scheduled": {JobStatus.SCHEDULED},
    "processing": {JobStatus.DOWNLOADING, JobStatus.PROCESSING, JobStatus.PUBLISHING},
    "failed": {JobStatus.FAILED},
    "completed": {JobStatus.COMPLETED},
    "cancelled": {JobStatus.CANCELLED},
}


@dataclass(frozen=True)
class BulkRetryResult:
    retried: int
    skipped_uncertain: int


def serialize_post(post: SourcePost) -> dict:
    return {
        "schema_version": 1,
        "content_kind": post.content_kind.value,
        "provider": post.provider,
        "source_id": post.source_id,
        "source_url": post.source_url,
        "normalized_url": post.normalized_url,
        "title": post.title,
        "description": post.description,
        "body": post.body,
        "content_warning": post.content_warning,
        "author_id": post.author_id,
        "author_name": post.author_name,
        "author_url": post.author_url,
        "source_tags": post.source_tags,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "media_items": [_serialize_media_item(item) for item in post.media_items],
        "metadata": post.metadata,
    }


def _serialize_media_item(item: MediaItem) -> dict:
    return {
        "url": item.url,
        "preview_url": item.preview_url,
        "filename": item.filename,
        "mime_type": item.mime_type,
        "media_type": item.media_type.value,
        "width": item.width,
        "height": item.height,
        "size": item.size,
        "telegram_file_id": item.telegram_file_id,
        "telegram_file_unique_id": item.telegram_file_unique_id,
        "order": item.order,
        "headers": item.headers,
    }


class JobService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, gate=None) -> None:
        self.sessions = sessions
        self.gate = gate

    async def ensure_user(self, telegram_id: int, username: str | None, display_name: str) -> User:
        async with self.sessions() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_user_id == telegram_id))
            if user is None:
                user = User(telegram_user_id=telegram_id, username=username, display_name=display_name)
                session.add(user)
                await session.flush()
            else:
                user.username = username
                user.display_name = display_name
                user.last_seen_at = utcnow()
            return user

    async def get_channel(self, alias: str | None) -> Channel | None:
        async with self.sessions() as session:
            statement = select(Channel).where(Channel.is_enabled.is_(True))
            if alias:
                statement = statement.where(Channel.alias == alias)
            else:
                statement = statement.where(Channel.is_default.is_(True))
            return await session.scalar(statement)

    async def get_preferred_channel(self, user_id: int, default_alias: str) -> Channel | None:
        async with self.sessions() as session:
            user = await session.get(User, user_id)
            if user and user.last_selected_channel_id is not None:
                channel = await session.scalar(
                    select(Channel).where(
                        Channel.id == user.last_selected_channel_id,
                        Channel.is_enabled.is_(True),
                    )
                )
                if channel:
                    return channel
            database_default = await session.scalar(
                select(Channel).where(
                    Channel.is_default.is_(True),
                    Channel.is_enabled.is_(True),
                )
            )
            if database_default is not None:
                return database_default
            return await session.scalar(
                select(Channel).where(
                    Channel.alias == default_alias,
                    Channel.is_enabled.is_(True),
                )
            )

    async def channels(self) -> list[Channel]:
        async with self.sessions() as session:
            return list((await session.scalars(select(Channel).order_by(Channel.alias))).all())

    async def get_channel_by_id(self, channel_id: int) -> Channel | None:
        async with self.sessions() as session:
            return await session.get(Channel, channel_id)

    async def set_channel_interval(self, alias: str, seconds: int) -> Channel | None:
        async with self.sessions() as session, session.begin():
            channel = await session.scalar(select(Channel).where(Channel.alias == alias, Channel.is_enabled.is_(True)))
            if not channel:
                return None
            await self._set_channel_interval(session, channel, seconds)
            return channel

    async def set_channel_interval_by_id(self, channel_id: int, seconds: int) -> Channel | None:
        async with self.sessions() as session, session.begin():
            channel = await session.get(Channel, channel_id)
            if channel is None or not channel.is_enabled:
                return None
            await self._set_channel_interval(session, channel, seconds)
            return channel

    async def set_channel_paused(self, channel_id: int, paused: bool) -> Channel | None:
        async with self.sessions() as session, session.begin():
            channel = await session.get(Channel, channel_id)
            if channel is None or not channel.is_enabled:
                return None
            channel.is_paused = paused
            return channel

    async def set_default_channel(self, channel_id: int) -> Channel | None:
        async with self.sessions() as session, session.begin():
            channel = await session.get(Channel, channel_id)
            if channel is None or not channel.is_enabled:
                return None
            for other in (await session.scalars(select(Channel).where(Channel.id!=channel.id,Channel.is_default.is_(True)))).all():
                other.is_default=False
            channel.is_default = True
            return channel

    async def create_preview(
        self, user_id: int, post: SourcePost, channel_id: int, tags: list[str], max_attempts: int,
        caption_override: str | None = None,
    ) -> Job:
        if self.gate:self.gate.assert_mutation_allowed()
        async with self.sessions() as session, session.begin():
            from app.bot.durable import current_update
            import hashlib
            identity=current_update.get()
            scope="telegram:preview:"+hashlib.sha256((post.provider+":"+post.source_id).encode()).hexdigest()[:40]
            receipt_key=str(identity) if identity else None
            if receipt_key:
                receipt=await session.scalar(select(CommandReceipt).where(CommandReceipt.principal_id==user_id,CommandReceipt.scope==scope,CommandReceipt.key==receipt_key))
                if receipt:return await session.get(Job,receipt.result["job_id"])
            job = Job(
                created_by_user_id=user_id,
                provider=post.provider,
                content_kind=post.content_kind,
                source_id=post.source_id,
                source_url=post.source_url,
                normalized_url=post.normalized_url,
                target_channel_id=channel_id,
                status=JobStatus.WAITING_CONFIRMATION,
                user_tags=tags,
                source_tags=post.source_tags,
                post_data=serialize_post(post),
                caption_override=caption_override,
                max_attempts=max_attempts,
            )
            session.add(job)
            user = await session.get(User, user_id)
            if user:
                user.last_selected_channel_id = channel_id
            await session.flush()
            if receipt_key:session.add(CommandReceipt(principal_id=user_id,scope=scope,key=receipt_key,payload_hash="telegram",result={"job_id":job.id}))
            return job

    async def set_caption_override(self, job_id: int, caption: str | None) -> Job | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job or job.status not in {
                JobStatus.WAITING_CONFIRMATION, JobStatus.QUEUED, JobStatus.SCHEDULED,
            }:
                return None
            if (
                job.content_kind == ContentKind.NEWS
                and job.status != JobStatus.WAITING_CONFIRMATION
            ):
                return None
            job.caption_override = caption
            return job

    async def set_post_field(self, job_id: int, field: str, value: str) -> Job | None:
        if field not in {"title", "description", "body"}:
            raise ValueError(f"Unsupported post field: {field}")
        job = await self.get(job_id)
        if not job or job.status != JobStatus.WAITING_CONFIRMATION:
            return None
        post_data = dict(job.post_data)
        post_data[field] = value
        if field == "title":
            metadata = dict(post_data.get("metadata", {}))
            for key in TRANSLATION_FIELDS:
                metadata.pop(key, None)
            post_data["metadata"] = metadata
        return await self.replace_draft_post(job, post_data)

    async def replace_draft_post(self, snapshot: Job, post_data: dict) -> Job | None:
        """Compare-and-swap: stale editor actions must not overwrite newer edits or a queued job."""
        async with self.sessions() as session, session.begin():
            result = await session.execute(
                update(Job).where(
                    Job.id == snapshot.id, Job.status == JobStatus.WAITING_CONFIRMATION,
                    Job.version == snapshot.version,
                ).values(post_data=post_data, updated_at=utcnow(), version=Job.version + 1, render_plan=None)
            )
            changed = result.rowcount == 1
        return await self.get(snapshot.id) if changed else None

    async def save_title_translation(self, job_id: int, title: str, metadata: dict) -> None:
        for _ in range(2):
            job = await self.get(job_id)
            if not job or job.status != JobStatus.WAITING_CONFIRMATION or job.post_data.get("title") != title:
                return
            data = dict(job.post_data)
            merged = dict(data.get("metadata", {}))
            for key in TRANSLATION_FIELDS:
                if key in metadata:
                    merged[key] = metadata[key]
            if merged == data.get("metadata", {}):
                return
            data["metadata"] = merged
            if await self.replace_draft_post(job, data) is not None:
                return

    async def add_media(
        self, job_id: int, item: MediaItem, *, replace: bool = False, limit: int = 10,
    ) -> Job | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not self._can_edit_news(job):
                return None
            post_data = dict(job.post_data)
            current = [] if replace else [dict(row) for row in post_data.get("media_items", [])]
            if len(current) >= limit:
                raise ValueError(f"Допустимо не более {limit} медиафайлов")
            item.order = len(current)
            current.append(_serialize_media_item(item))
            post_data["media_items"] = current
            job.post_data = post_data
            return job

    async def remove_media(self, job_id: int, index: int) -> Job | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not self._can_edit_news(job):
                return None
            post_data = dict(job.post_data)
            current = [dict(row) for row in post_data.get("media_items", [])]
            if index < 0 or index >= len(current):
                raise IndexError("Медиафайл не найден")
            current.pop(index)
            for order, row in enumerate(current):
                row["order"] = order
            post_data["media_items"] = current
            job.post_data = post_data
            return job

    async def clear_media(self, job_id: int) -> Job | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not self._can_edit_news(job):
                return None
            post_data = dict(job.post_data)
            post_data["media_items"] = []
            job.post_data = post_data
            return job

    @staticmethod
    def _can_edit_news(job: Job | None) -> bool:
        return bool(
            job
            and job.content_kind == ContentKind.NEWS
            and job.status == JobStatus.WAITING_CONFIRMATION
        )

    async def get(self, job_id: int) -> Job | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(Job).options(selectinload(Job.channel), selectinload(Job.media_items)).where(Job.id == job_id)
            )

    async def transition(self, job_id: int, status: JobStatus, message: str | None = None) -> Job | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job:
                return None
            old = job.status
            job.status = status
            if status == JobStatus.DOWNLOADING and not job.started_at:
                job.started_at = utcnow()
            if status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                job.finished_at = utcnow()
                await self._release_channel(session, job)
            session.add(JobEvent(job_id=job_id, event_type="status", old_status=old, new_status=status, message=message))
            return job

    async def enqueue(self, job_id: int) -> bool:
        platform=getattr(self,"platform",None)
        if self.gate and platform:
            from uuid import uuid4
            from app.application.platform import PlatformError
            job=await self.get(job_id)
            if not job:return False
            try:
                await platform.publication_action(job.created_by_user_id,job.id,"retry" if job.status==JobStatus.FAILED else "enqueue",{"expected_version":job.version},str(uuid4()))
                return True
            except PlatformError:return False
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job or job.status not in {JobStatus.WAITING_CONFIRMATION, JobStatus.FAILED}:
                return False
            if job.content_kind == ContentKind.ARTWORK and not job.post_data.get("media_items"):
                return False
            # Reserve the exact version validated above before changing its queue state.
            reserved = await session.execute(
                update(Job).where(Job.id == job.id, Job.updated_at == job.updated_at, Job.status == job.status)
                .values(updated_at=utcnow()).execution_options(synchronize_session=False)
            )
            if reserved.rowcount != 1:
                return False
            await session.refresh(job)
            if job.status == JobStatus.FAILED:
                job.attempts = 0
                job.scheduled_at = None
                job.force_publish = False
            job.status = (
                JobStatus.SCHEDULED if job.scheduled_at is not None else JobStatus.QUEUED
            )
            job.cancel_requested = False
            job.error_code = job.error_message = None
            job.finished_at = None
            job.queue_position = (
                None
                if job.status == JobStatus.SCHEDULED
                else await self._next_queue_position(session, job.target_channel_id)
            )
            return True

    async def claim_next(self) -> Job | None:
        if self.gate:
            try:
                self.gate.assert_publication_allowed()
            except RuntimeError:
                return None
        async with self.sessions() as session, session.begin():
            state = await session.get(ApplicationState, 1)
            if state and (state.publication_blocks or state.maintenance_state != "normal"):
                return None
            from app.application.scheduling import apply_overdue_in_session
            await apply_overdue_in_session(session)
            await session.flush()
            while True:
                now = utcnow()
                candidate = await session.execute(
                    select(Job.id, Job.target_channel_id, Job.status)
                    .join(Channel, Job.target_channel_id == Channel.id)
                    .where(
                        Job.status.in_({JobStatus.QUEUED, JobStatus.SCHEDULED}),
                        or_(self.gate is None,Job.render_plan["job_version"].as_integer().is_not(None)),
                        Job.cancel_requested.is_(False),
                        or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now),
                        Channel.is_enabled.is_(True),
                        Channel.active_job_id.is_(None),
                        or_(Channel.is_paused.is_(False), Job.force_publish.is_(True)),
                        or_(
                            Job.force_publish.is_(True),
                            and_(
                                Job.status == JobStatus.SCHEDULED,
                                Job.scheduled_at.is_not(None),
                                Job.scheduled_at <= now,
                            ),
                            and_(
                                Job.status == JobStatus.QUEUED,
                                Job.scheduled_at.is_(None),
                                or_(
                                    Channel.next_publish_at.is_(None),
                                    Channel.next_publish_at <= now,
                                ),
                            ),
                        ),
                    )
                    .order_by(
                        Job.force_publish.desc(),
                        case((Job.status == JobStatus.SCHEDULED, 0), else_=1),
                        Job.scheduled_at,
                        Job.queue_position.is_(None),
                        Job.queue_position,
                        Job.created_at,
                        Job.id,
                    )
                    .limit(1)
                )
                row = candidate.first()
                if row is None:
                    return None
                candidate_id, channel_id, candidate_status = row
                reserved_channel_id = await session.scalar(
                    update(Channel)
                    .where(Channel.id == channel_id, Channel.active_job_id.is_(None))
                    .values(active_job_id=candidate_id)
                    .returning(Channel.id)
                )
                if reserved_channel_id is None:
                    continue
                claimed_id = await session.scalar(
                    update(Job)
                    .where(Job.id == candidate_id, Job.status == candidate_status)
                    .values(
                        status=JobStatus.DOWNLOADING, version=Job.version + 1, attempts=Job.attempts + 1,
                        started_at=func.coalesce(Job.started_at, utcnow()), next_attempt_at=None,
                    )
                    .returning(Job.id)
                )
                if claimed_id is None:
                    await session.execute(
                        update(Channel)
                        .where(Channel.id == channel_id, Channel.active_job_id == candidate_id)
                        .values(active_job_id=None)
                    )
                    continue
                return await session.scalar(
                    select(Job).options(selectinload(Job.channel)).where(Job.id == claimed_id)
                )

    async def force_publish(self, job_id: int) -> Job | None:
        platform=getattr(self,"platform",None)
        if self.gate and platform:
            from uuid import uuid4
            from app.application.platform import PlatformError
            job=await self.get(job_id)
            if not job:return None
            try:
                await platform.publication_action(job.created_by_user_id,job.id,"publish-now",{"expected_version":job.version},str(uuid4()))
                return await self.get(job_id)
            except PlatformError:return None
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if (
                not job
                or job.status not in {JobStatus.QUEUED, JobStatus.SCHEDULED}
                or job.cancel_requested
            ):
                return None
            old_status = job.status
            job.force_publish = True
            job.scheduled_at = None
            job.status = JobStatus.QUEUED
            session.add(JobEvent(
                job_id=job.id, event_type="manual_publish", old_status=old_status,
                new_status=job.status, message="Manual publication requested",
            ))
            return job

    async def force_next_publish(self, channel_id: int | None = None) -> Job | None:
        if self.gate and getattr(self,"platform",None):
            async with self.sessions() as session:
                query=select(Job.id).join(Channel,Job.target_channel_id==Channel.id).where(Job.status==JobStatus.QUEUED,Job.scheduled_at.is_(None),Job.cancel_requested.is_(False),Channel.is_enabled.is_(True))
                if channel_id is not None:query=query.where(Job.target_channel_id==channel_id)
                job_id=await session.scalar(query.order_by(Job.queue_position.is_(None),Job.queue_position,Job.created_at,Job.id).limit(1))
            return await self.force_publish(job_id) if job_id else None
        async with self.sessions() as session, session.begin():
            candidate = (
                select(Job.id)
                .join(Channel, Job.target_channel_id == Channel.id)
                .where(
                    Job.status == JobStatus.QUEUED,
                    Job.scheduled_at.is_(None),
                    Job.cancel_requested.is_(False),
                    Channel.is_enabled.is_(True),
                )
                .order_by(
                    Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id,
                )
                .limit(1)
            )
            if channel_id is not None:
                candidate = candidate.where(Job.target_channel_id == channel_id)
            forced_id = await session.scalar(
                update(Job)
                .where(Job.id == candidate.scalar_subquery(), Job.status == JobStatus.QUEUED)
                .values(force_publish=True, scheduled_at=None)
                .returning(Job.id)
            )
            if forced_id is None:
                return None
            job = await session.get(Job, forced_id)
            if job is None:
                return None
            session.add(JobEvent(
                job_id=job.id, event_type="manual_publish", old_status=job.status,
                new_status=job.status, message="Manual publication requested for next queued job",
            ))
            return job

    async def request_cancel(self, job_id: int) -> bool:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job or job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
                return False
            if job.status in {
                JobStatus.WAITING_CONFIRMATION,
                JobStatus.QUEUED,
                JobStatus.SCHEDULED,
                JobStatus.FAILED,
            }:
                job.status = JobStatus.CANCELLED
                job.finished_at = utcnow()
            else:
                job.cancel_requested = True
            return True

    async def is_cancelled(self, job_id: int) -> bool:
        async with self.sessions() as session:
            job = await session.get(Job, job_id)
            return not job or job.cancel_requested or job.status == JobStatus.CANCELLED

    async def fail(self, job_id: int, error: Exception, retry: bool) -> None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job:
                return
            job.error_code = getattr(error, "code", type(error).__name__)
            job.error_message = str(error)[:2000]
            retry_status = (
                JobStatus.SCHEDULED
                if job.scheduled_at is not None else JobStatus.QUEUED
            )
            job.status = retry_status if retry and job.attempts < job.max_attempts else JobStatus.FAILED
            if job.status in {JobStatus.QUEUED, JobStatus.SCHEDULED}:
                delays = (5, 30, 120)
                job.next_attempt_at = utcnow() + timedelta(seconds=delays[min(job.attempts - 1, len(delays) - 1)])
            if job.status == JobStatus.FAILED:
                job.finished_at = utcnow()
            await self._release_channel(session, job)

    async def defer(self,job_id: int) -> None:
        """A closed permit before the first send is a pause, not a failed attempt."""
        async with self.sessions() as session,session.begin():
            job=await session.get(Job,job_id)
            if not job or job.status not in {JobStatus.DOWNLOADING,JobStatus.PROCESSING,JobStatus.PUBLISHING}:return
            job.status=JobStatus.SCHEDULED if job.scheduled_at else JobStatus.QUEUED
            job.attempts=max(0,job.attempts-1)
            job.error_code=job.error_message=None
            await self._release_channel(session,job)

    async def change_channel(self, job_id: int, channel_id: int) -> Channel | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            channel = await session.scalar(
                select(Channel).where(Channel.id == channel_id, Channel.is_enabled.is_(True))
            )
            if not job or not channel or job.status != JobStatus.WAITING_CONFIRMATION:
                return None
            job.target_channel_id = channel.id
            user = await session.get(User, job.created_by_user_id)
            if user:
                user.last_selected_channel_id = channel.id
            return channel

    async def set_schedule(self, job_id: int, scheduled_at: datetime | None) -> Job | None:
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job or job.status not in {
                JobStatus.WAITING_CONFIRMATION, JobStatus.QUEUED, JobStatus.SCHEDULED,
            }:
                return None
            old_status = job.status
            job.scheduled_at = scheduled_at
            metadata=dict(job.post_data.get("metadata",{}))
            for key in ("overdue_hold","overdue_catchup","overdue_policy_applied"):metadata.pop(key,None)
            job.post_data={**job.post_data,"metadata":metadata}
            job.next_attempt_at=None
            if scheduled_at is not None:
                job.force_publish = False
                if job.status == JobStatus.QUEUED:
                    job.status = JobStatus.SCHEDULED
                    job.queue_position = None
            elif job.status == JobStatus.SCHEDULED:
                job.status = JobStatus.QUEUED
                job.queue_position = await self._next_queue_position(
                    session, job.target_channel_id,
                )
            session.add(JobEvent(
                job_id=job.id,
                event_type="schedule_changed",
                old_status=old_status,
                new_status=job.status,
                message=scheduled_at.isoformat() if scheduled_at is not None else "Schedule cleared",
            ))
            return job

    async def shuffle_queued(self, channel_id: int) -> int:
        """Randomize only regular queued jobs of one channel in a single transaction."""
        async with self.sessions() as session, session.begin():
            rows = list((await session.scalars(
                select(Job)
                .where(
                    Job.target_channel_id == channel_id,
                    Job.status == JobStatus.QUEUED,
                    Job.scheduled_at.is_(None),
                )
                .order_by(
                    Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id,
                )
                .with_for_update()
            )).all())
            if len(rows) < 2:
                return len(rows)
            original_order = [job.id for job in rows]
            random.shuffle(rows)
            if [job.id for job in rows] == original_order:
                rows.append(rows.pop(0))
            for position, job in enumerate(rows, start=1):
                job.queue_position = position
            return len(rows)

    async def move_queued(self, job_id: int, direction: str) -> Job | None:
        if direction not in {"up", "down"}:
            raise ValueError(f"Unsupported direction: {direction}")
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if (
                not job
                or job.status != JobStatus.QUEUED
                or job.scheduled_at is not None
            ):
                return None
            rows = list((await session.scalars(
                select(Job)
                .where(
                    Job.target_channel_id == job.target_channel_id,
                    Job.status == JobStatus.QUEUED,
                    Job.scheduled_at.is_(None),
                )
                .order_by(
                    Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id,
                )
            )).all())
            for position, row in enumerate(rows, start=1):
                row.queue_position = position
            index = next((index for index, row in enumerate(rows) if row.id == job_id), None)
            if index is None:
                return None
            neighbor_index = index - 1 if direction == "up" else index + 1
            if neighbor_index < 0 or neighbor_index >= len(rows):
                return job
            neighbor = rows[neighbor_index]
            job.queue_position, neighbor.queue_position = neighbor.queue_position, job.queue_position
            return job

    async def duplicate(self, job: Job) -> Publication | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(Publication)
                .join(Job, Publication.job_id == Job.id)
                .where(Job.provider == job.provider, Job.source_id == job.source_id, Publication.channel_id == job.target_channel_id)
                .limit(1)
            )

    async def duplicate_state_for(
        self, provider: str, source_id: str, channel_id: int, exclude_job_id: int,
    ) -> str | None:
        async with self.sessions() as session:
            publication = await session.scalar(
                select(Publication)
                .join(Job, Publication.job_id == Job.id)
                .where(Job.provider == provider, Job.source_id == source_id, Publication.channel_id == channel_id)
                .limit(1)
            )
            if publication:
                return "published"
            active_job_id = await session.scalar(
                select(Job.id)
                .where(
                    Job.id != exclude_job_id,
                    Job.provider == provider,
                    Job.source_id == source_id,
                    Job.target_channel_id == channel_id,
                    Job.status.in_(ACTIVE_JOB_STATUSES | {JobStatus.WAITING_CONFIRMATION}),
                )
                .limit(1)
            )
            return "active" if active_job_id is not None else None

    async def allow_duplicate_and_enqueue(self, job_id: int) -> bool:
        platform=getattr(self,"platform",None)
        if self.gate and platform:
            from uuid import uuid4
            from app.application.platform import PlatformError
            job=await self.get(job_id)
            if not job:return False
            try:
                await platform.publication_action(job.created_by_user_id,job.id,"enqueue",{"expected_version":job.version,"confirm_duplicate":True},str(uuid4()))
                return True
            except PlatformError:return False
        async with self.sessions() as session, session.begin():
            job = await session.get(Job, job_id)
            if not job or job.status != JobStatus.WAITING_CONFIRMATION:
                return False
            if job.content_kind == ContentKind.ARTWORK and not job.post_data.get("media_items"):
                return False
            reserved = await session.execute(
                update(Job).where(Job.id == job.id, Job.updated_at == job.updated_at, Job.status == job.status)
                .values(updated_at=utcnow()).execution_options(synchronize_session=False)
            )
            if reserved.rowcount != 1:
                return False
            await session.refresh(job)
            job.allow_duplicate = True
            job.status = (
                JobStatus.SCHEDULED if job.scheduled_at is not None else JobStatus.QUEUED
            )
            job.queue_position = (
                None
                if job.status == JobStatus.SCHEDULED
                else await self._next_queue_position(session, job.target_channel_id)
            )
            return True

    async def queue(self, alias: str | None = None, limit: int | None = 50) -> list[Job] | None:
        async with self.sessions() as session:
            channel_id: int | None = None
            if alias:
                channel_id = await session.scalar(
                    select(Channel.id).where(Channel.alias == alias, Channel.is_enabled.is_(True))
                )
                if channel_id is None:
                    return None
            statement = (
                select(Job)
                .join(Channel, Job.target_channel_id == Channel.id)
                .options(selectinload(Job.channel))
                .where(Job.status.in_(ACTIVE_JOB_STATUSES), Channel.is_enabled.is_(True))
            )
            if channel_id is not None:
                statement = statement.where(Job.target_channel_id == channel_id)
            statement = statement.order_by(
                Channel.alias, Job.queue_position.is_(None), Job.queue_position,
                Job.created_at, Job.id,
            )
            if limit is not None:
                statement = statement.limit(limit)
            return list(
                (await session.scalars(statement)).all()
            )

    async def managed_queue(
        self, channel_id: int | None, status_filter: str, limit: int | None = 50,
    ) -> list[Job]:
        statuses = QUEUE_FILTER_STATUSES.get(status_filter)
        if statuses is None:
            raise ValueError(f"Unsupported queue filter: {status_filter}")
        async with self.sessions() as session:
            statement = (
                select(Job)
                .join(Channel, Job.target_channel_id == Channel.id)
                .options(selectinload(Job.channel))
                .where(Job.status.in_(statuses))
            )
            if channel_id is not None:
                statement = statement.where(Job.target_channel_id == channel_id)
            if status_filter == "scheduled":
                statement = statement.order_by(
                    Channel.alias, Job.scheduled_at, Job.created_at, Job.id,
                )
            else:
                statement = statement.order_by(
                    Channel.alias, Job.queue_position.is_(None), Job.queue_position,
                    Job.created_at, Job.id,
                )
            if limit is not None:
                statement = statement.limit(limit)
            return list((await session.scalars(statement)).all())

    async def cancel_filtered(self, channel_id: int | None, status_filter: str) -> int:
        statuses = QUEUE_FILTER_STATUSES.get(status_filter)
        if statuses is None:
            raise ValueError(f"Unsupported queue filter: {status_filter}")
        if status_filter in {"completed", "cancelled"}:
            return 0
        async with self.sessions() as session, session.begin():
            statement = select(Job).where(Job.status.in_(statuses))
            if channel_id is not None:
                statement = statement.where(Job.target_channel_id == channel_id)
            rows = list((await session.scalars(statement)).all())
            active = {JobStatus.DOWNLOADING, JobStatus.PROCESSING, JobStatus.PUBLISHING}
            for job in rows:
                if job.status in active:
                    job.cancel_requested = True
                else:
                    job.status = JobStatus.CANCELLED
                    job.finished_at = utcnow()
            return len(rows)

    async def retry_failed(self, channel_id: int | None) -> BulkRetryResult:
        async with self.sessions() as session, session.begin():
            statement = select(Job).where(Job.status == JobStatus.FAILED)
            if channel_id is not None:
                statement = statement.where(Job.target_channel_id == channel_id)
            rows = list((await session.scalars(statement)).all())
            retried = 0
            skipped_uncertain = 0
            for job in rows:
                if job.error_code == "uncertain_publish":
                    skipped_uncertain += 1
                    continue
                job.status = JobStatus.QUEUED
                job.attempts = 0
                job.error_code = job.error_message = None
                job.cancel_requested = False
                job.force_publish = False
                job.finished_at = None
                job.next_attempt_at = None
                job.scheduled_at = None
                job.queue_position = await self._next_queue_position(
                    session, job.target_channel_id,
                )
                retried += 1
            return BulkRetryResult(retried=retried, skipped_uncertain=skipped_uncertain)

    async def recent(self, limit: int = 10) -> list[Job]:
        async with self.sessions() as session:
            return list((await session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit))).all())

    async def stats(self) -> dict[str, int]:
        async with self.sessions() as session:
            rows = (await session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))).all()
            return {str(status): count for status, count in rows}

    async def recover(self, stale_minutes: int | None = None) -> int:
        processing_conditions: list[ColumnElement[bool]] = [
            Job.status.in_({JobStatus.DOWNLOADING, JobStatus.PROCESSING})
        ]
        publishing_conditions: list[ColumnElement[bool]] = [Job.status == JobStatus.PUBLISHING]
        if stale_minutes is not None:
            cutoff = utcnow() - timedelta(minutes=stale_minutes)
            processing_conditions.append(Job.updated_at < cutoff)
            publishing_conditions.append(Job.updated_at < cutoff)
        async with self.sessions() as session, session.begin():
            cancelled_result = await session.execute(
                update(Job)
                .where(*processing_conditions, Job.cancel_requested.is_(True))
                .values(
                    status=JobStatus.CANCELLED,
                    error_code=None,
                    error_message="Cancelled during restart recovery",
                    finished_at=utcnow(),
                    next_attempt_at=None,
                )
                .returning(Job.id)
            )
            recovered_result = await session.execute(
                update(Job)
                .where(*processing_conditions, Job.cancel_requested.is_(False))
                .values(
                    status=case(
                        (Job.scheduled_at.is_not(None), JobStatus.SCHEDULED),
                        else_=JobStatus.QUEUED,
                    ),
                    queue_position=case(
                        (Job.scheduled_at.is_not(None), None),
                        else_=Job.queue_position,
                    ),
                    error_code="recovered_after_restart",
                    error_message="Recovered after restart",
                    next_attempt_at=None,
                )
                .returning(Job.id)
            )
            # Publishing is deliberately not replayed automatically: that could duplicate posts.
            uncertain_result = await session.execute(
                update(Job)
                .where(*publishing_conditions)
                .values(
                    status=JobStatus.FAILED,
                    error_code="uncertain_publish",
                    error_message="Проверьте канал перед повтором",
                    finished_at=utcnow(),
                )
                .returning(Job.id)
            )
            affected_ids = [
                *cancelled_result.scalars(),
                *recovered_result.scalars(),
                *uncertain_result.scalars(),
            ]
            if affected_ids:
                await session.execute(
                    update(Channel)
                    .where(Channel.active_job_id.in_(affected_ids))
                    .values(active_job_id=None)
                )
            return len(affected_ids)

    @staticmethod
    async def _release_channel(session: AsyncSession, job: Job) -> None:
        await session.execute(
            update(Channel)
            .where(Channel.id == job.target_channel_id, Channel.active_job_id == job.id)
            .values(active_job_id=None)
        )

    @staticmethod
    async def _next_queue_position(session: AsyncSession, channel_id: int) -> int:
        current = await session.scalar(
            select(func.max(Job.queue_position)).where(
                Job.target_channel_id == channel_id,
                Job.status == JobStatus.QUEUED,
            )
        )
        return int(current or 0) + 1

    @staticmethod
    async def _set_channel_interval(
        session: AsyncSession, channel: Channel, seconds: int,
    ) -> None:
        channel.publish_interval_seconds = seconds
        last_published = await session.scalar(
            select(func.max(Publication.published_at)).where(
                Publication.channel_id == channel.id,
            )
        )
        channel.next_publish_at = (
            last_published + timedelta(seconds=seconds)
            if last_published and seconds else None
        )


# Managed clients share the same global maintenance barrier. Legacy constructors
# remain usable for isolated old-suite adapters without granting managed authority.
def _guard_mutation(method):
    from functools import wraps
    @wraps(method)
    async def guarded(self,*args,**kwargs):
        if self.gate:self.gate.assert_mutation_allowed()
        try:return await method(self,*args,**kwargs)
        except StaleDataError as error:
            from app.domain.exceptions import VersionConflictError
            raise VersionConflictError("Публикация уже изменилась в другом интерфейсе. Откройте её снова и сверьте правки.") from error
    return guarded

for _name in ("ensure_user","set_channel_interval","set_channel_interval_by_id","set_channel_paused","set_default_channel","create_preview","set_caption_override","set_post_field","replace_draft_post","save_title_translation","add_media","remove_media","clear_media","enqueue","force_publish","force_next_publish","request_cancel","change_channel","set_schedule","shuffle_queued","move_queued","allow_duplicate_and_enqueue","cancel_filtered","retry_failed"):
    setattr(JobService,_name,_guard_mutation(getattr(JobService,_name)))
