import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlparse

from sqlalchemy import and_, or_, select, update, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Channel, Job, NewsTask, User, Operation, OperationEvent, CommandReceipt, MediaAsset, JobAsset, AIWorker, utcnow
from app.domain.enums import ContentKind, JobStatus, MediaType, NewsTaskStatus
from app.domain.models import MediaItem, SourcePost
from app.news.models import NewsMedia, NewsMediaKind
from app.news.worker_models import NewsTask as WorkerTask
from app.news.worker_models import WorkerResult
from app.services.job_service import serialize_post
from app.services.news_render_service import NewsRenderService
from app.utils.tags import publication_tags


class NewsTaskLeaseError(Exception):
    pass


@dataclass(frozen=True)
class CompletedNewsTask:
    task_id: int
    job_id: int
    origin_chat_id: str | None
    status_message_id: int | None


class NewsTaskService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, lease_extension_seconds: int = 1800, gate=None,
        news_renderer: NewsRenderService | None = None,
        auto_add_source_tags: bool = True,
        max_tags: int = 20,
        max_tag_length: int = 64,
    ) -> None:
        self.sessions = sessions
        self.gate = gate
        self.lease_extension_seconds = lease_extension_seconds
        self.news_renderer = news_renderer or NewsRenderService()
        self.auto_add_source_tags = auto_add_source_tags
        self.max_tags = max_tags
        self.max_tag_length = max_tag_length

    async def create(
        self, *, user_id: int, channel_id: int, origin_chat_id: int | str,
        source_kind: str, input_payload: dict, user_tags: list[str],
        model_name: str | None, max_attempts: int,
    ) -> NewsTask:
        if self.gate:self.gate.assert_mutation_allowed()
        payload = dict(input_payload)
        payload.setdefault("kind", source_kind)
        import hashlib,json
        # Receipt identity depends on accepted input, never on later model selection.
        source_receipt_digest=hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()[:40]
        async with self.sessions() as session, session.begin():
            platform=getattr(self,'platform',None)
            if self.gate and platform:
                preferred_worker,selected_model=await platform._selected_ai_target(session)
                if preferred_worker:
                    model_name=selected_model;target={'worker_id':preferred_worker}
                elif model_name and not await session.scalar(select(func.count()).select_from(AIWorker)):
                    target={'legacy_worker_allowed':True}
                else:
                    model_name=None;target={'awaiting_selection':True}
                payload['metadata']={**payload.get('metadata',{}),**target}
            from app.bot.durable import current_update
            import hashlib, json
            update_identity=current_update.get()
            receipt_scope="telegram:news:"+source_receipt_digest
            receipt_key=str(update_identity) if update_identity else None
            if receipt_key:
                receipt=await session.scalar(select(CommandReceipt).where(CommandReceipt.principal_id==user_id,CommandReceipt.scope==receipt_scope,CommandReceipt.key==receipt_key))
                if receipt:return await session.get(NewsTask,receipt.result["task_id"])
            task = NewsTask(
                created_by_user_id=user_id,
                target_channel_id=channel_id,
                origin_chat_id=str(origin_chat_id) if origin_chat_id is not None else None,
                source_kind=source_kind,
                input_payload=payload,
                user_tags=list(user_tags),
                status=NewsTaskStatus.QUEUED,
                stage="queued",
                stage_message="Ожидаем домашний обработчик",
                model_name=model_name,
                max_attempts=max_attempts,
            )
            session.add(task)
            await session.flush()
            if receipt_key:session.add(CommandReceipt(principal_id=user_id,scope=receipt_scope,key=receipt_key,payload_hash="telegram",result={"task_id":task.id}))
            return task

    async def set_status_message(self, task_id: int, message_id: int) -> None:
        async with self.sessions() as session, session.begin():
            task = await session.get(NewsTask, task_id)
            if task is not None:
                task.status_message_id = message_id

    async def get(self, task_id: int) -> NewsTask | None:
        async with self.sessions() as session:
            return await session.get(NewsTask, task_id)

    async def lease(
        self, *, worker_id: str, lease_seconds: int, source_types: list[str] | None = None,
        model_name: str | None = None,
        use_worker_model: bool = False,
        allow_unassigned_model: bool = False,
        unassigned_worker_only: bool = False,
    ) -> WorkerTask | None:
        if use_worker_model and not model_name:
            return None
        if self.gate:
            self.gate.assert_mutation_allowed()
        allowed = set(source_types or [])
        async with self.sessions() as session, session.begin():
            await self._expire_exhausted_in_session(session)
            while True:
                now = utcnow()
                claimable = or_(
                    NewsTask.status == NewsTaskStatus.QUEUED,
                    and_(
                        NewsTask.status == NewsTaskStatus.LEASED,
                        NewsTask.lease_expires_at.is_not(None),
                        NewsTask.lease_expires_at <= now,
                    ),
                )
                statement = (
                    select(NewsTask.id)
                    .where(claimable, NewsTask.attempts < NewsTask.max_attempts, NewsTask.kind == "news")
                    .order_by(NewsTask.created_at, NewsTask.id)
                    .limit(1)
                )
                if allowed:
                    statement = statement.where(NewsTask.source_kind.in_(allowed))
                if unassigned_worker_only:
                    statement=statement.where(NewsTask.input_payload['metadata']['worker_id'].as_string().is_(None),
                        func.coalesce(NewsTask.input_payload['metadata']['awaiting_selection'].as_boolean(),False).is_(False))
                if model_name and not use_worker_model:
                    statement = statement.where(or_(NewsTask.model_name == model_name,NewsTask.model_name.is_(None)) if allow_unassigned_model else NewsTask.model_name == model_name)
                task_id = await session.scalar(statement)
                if task_id is None:
                    return None
                token = secrets.token_urlsafe(32)
                expires_at = now + timedelta(seconds=lease_seconds)
                claimed_id = await session.scalar(
                    update(NewsTask)
                    .where(NewsTask.id == task_id, claimable)
                    .values(
                        status=NewsTaskStatus.LEASED,
                        stage="leased",
                        stage_message=f"Задачу забрал обработчик {worker_id}",
                        lease_owner=worker_id,
                        lease_token=token,
                        lease_expires_at=expires_at,
                        attempts=NewsTask.attempts + 1,
                        lease_generation=NewsTask.lease_generation+1,
                        error_message=None,
                        # Local mode follows the selected model for waiting or
                        # retried work. A live lease keeps its original model.
                        **({"model_name": model_name} if use_worker_model else {"model_name":func.coalesce(NewsTask.model_name,model_name)} if allow_unassigned_model else {}),
                    )
                    .returning(NewsTask.id)
                    .execution_options(synchronize_session=False)
                )
                if claimed_id is None:
                    continue
                task = await session.get(NewsTask, claimed_id)
                if task is None:
                    raise NewsTaskLeaseError("Выданная задача больше не существует")
                return WorkerTask.model_validate({
                    "id": task.id,
                    "lease_token": token,
                    "input_payload": task.input_payload,
                    "model_name": task.model_name,
                })

    async def expire_exhausted(self) -> list[NewsTask]:
        async with self.sessions() as session, session.begin():
            ids = await self._expire_exhausted_in_session(session)
            if not ids:
                return []
            return list((await session.scalars(
                select(NewsTask).where(NewsTask.id.in_(ids)).order_by(NewsTask.id)
            )).all())

    async def progress(
        self, task_id: int, *, lease_token: str, stage: str, message: str,
    ) -> tuple[NewsTask, bool]:
        if self.gate:self.gate.assert_mutation_allowed()
        async with self.sessions() as session, session.begin():
            task = await self._leased_task(session, task_id, lease_token)
            clean_message = " ".join(message.split())[:300]
            changed = task.stage != stage or task.stage_message != clean_message
            extended = utcnow() + timedelta(seconds=self.lease_extension_seconds)
            expires_at = (
                extended
                if task.lease_expires_at is None or _as_utc(task.lease_expires_at) < extended
                else task.lease_expires_at
            )
            values = {"lease_expires_at": expires_at}
            if changed:
                values.update(stage=stage[:64], stage_message=clean_message)
            updated_id = await session.scalar(
                update(NewsTask)
                .where(*self._active_lease_conditions(task_id, lease_token))
                .values(**values)
                .returning(NewsTask.id)
                .execution_options(synchronize_session=False)
            )
            if updated_id is None:
                raise NewsTaskLeaseError("Аренда задачи недействительна")
            await session.refresh(task)
            if task.operation_id:
                operation=await session.get(Operation,task.operation_id)
                if operation:
                    operation.status="running";operation.stage_key=task.stage;operation.message=task.stage_message;operation.heartbeat_at=utcnow();operation.version+=1;operation.last_event_seq+=1
                    session.add(OperationEvent(operation_id=operation.id,sequence=operation.last_event_seq,stage_key=task.stage,status="running",message=task.stage_message))
            return task, changed

    async def complete(
        self, task_id: int, *, lease_token: str, result_payload: dict,
    ) -> CompletedNewsTask:
        if self.gate:self.gate.assert_mutation_allowed()
        result = WorkerResult.model_validate(result_payload)
        async with self.sessions() as session, session.begin():
            previous=await session.get(NewsTask,task_id)
            canonical=result.model_dump(mode="json")
            if previous and previous.status==NewsTaskStatus.COMPLETED and previous.job_id:
                import hashlib, secrets
                receipt=await session.scalar(select(CommandReceipt).where(CommandReceipt.principal_id==previous.created_by_user_id,CommandReceipt.scope=="news:complete",CommandReceipt.key==str(task_id)))
                previous_token=receipt.payload_hash if receipt else None
                if previous_token and secrets.compare_digest(previous_token,hashlib.sha256(lease_token.encode()).hexdigest()) and previous.result_json==canonical:
                    return CompletedNewsTask(previous.id,previous.job_id,previous.origin_chat_id,previous.status_message_id)
                raise NewsTaskLeaseError("Результат завершённой задачи не совпадает")
            claimed_id = await session.scalar(
                update(NewsTask)
                .where(*self._active_lease_conditions(task_id, lease_token))
                .values(status=NewsTaskStatus.FINALIZING)
                .returning(NewsTask.id)
                .execution_options(synchronize_session=False)
            )
            if claimed_id is None:
                raise NewsTaskLeaseError("Аренда задачи недействительна")
            task = await session.get(NewsTask, claimed_id)
            if task is None:
                raise NewsTaskLeaseError("Задача больше не существует")
            if result.source.kind.value != task.source_kind:
                raise ValueError("Тип обработанного источника не совпадает с задачей")
            if task.job_id is not None:
                raise NewsTaskLeaseError("Задача уже создала публикацию")
            post = self._post_from_result(task, result)
            uploaded_assets=[]
            for asset_id in task.input_payload.get("metadata",{}).get("upload_asset_ids",[]):
                asset=await session.get(MediaAsset,asset_id)
                if not asset or asset.source_kind!="upload":
                    raise ValueError("Загруженный медиафайл больше не доступен")
                uploaded_assets.append(asset)
                post.media_items.append(MediaItem(url=asset.source_reference or "",filename=asset.filename or "image",order=len(post.media_items),mime_type=asset.mime_type,width=asset.width,height=asset.height,size=asset.size,telegram_file_id=asset.telegram_file_id))
            channel = await session.get(Channel, task.target_channel_id)
            rendered_tags = publication_tags(
                task.user_tags, post.source_tags, getattr(channel, "default_tags", None),
                include_source=self.auto_add_source_tags,
                max_tags=self.max_tags, max_length=self.max_tag_length,
            )
            # Do not commit a completed task whose draft cannot be previewed or
            # published through Telegram.
            self.news_renderer.build(post, rendered_tags)
            job = Job(
                created_by_user_id=task.created_by_user_id,
                origin_ai_task_id=task.id,
                provider=post.provider,
                content_kind=ContentKind.NEWS,
                source_id=post.source_id,
                source_url=post.source_url,
                normalized_url=post.normalized_url,
                target_channel_id=task.target_channel_id,
                status=JobStatus.WAITING_CONFIRMATION,
                user_tags=list(task.user_tags),
                source_tags=list(post.source_tags),
                post_data=serialize_post(post),
                max_attempts=task.max_attempts,
            )
            session.add(job)
            await session.flush()
            # Catalogue the complete set, keeping uploaded originals immutable and shared.
            from uuid import uuid4
            uploaded_by_order={len(post.media_items)-len(uploaded_assets)+i:asset for i,asset in enumerate(uploaded_assets)}
            for index,item in enumerate(post.media_items):
                asset=uploaded_by_order.get(index)
                if asset is None:
                    asset=MediaAsset(id=str(uuid4()),source_kind=post.provider,source_reference=item.url,telegram_file_id=item.telegram_file_id,state="telegram" if item.telegram_file_id else "remote",filename=item.filename,mime_type=item.mime_type,size=item.size,width=item.width,height=item.height)
                    session.add(asset);await session.flush()
                session.add(JobAsset(job_id=job.id,asset_id=asset.id,source_index=index+1,selected=True,sort_order=index,purpose="original"))
            if task.origin_chat_id is not None:
                from uuid import uuid5,NAMESPACE_URL
                prepare_id=str(uuid5(NAMESPACE_URL,f"publisher:news:{task.id}:prepare"))
                preview_id=str(uuid5(NAMESPACE_URL,f"publisher:news:{task.id}:telegram-preview"))
                for operation_id,kind,payload in (
                    (prepare_id,"prepare",{"job_version":job.version}),
                    (preview_id,"telegram_preview",{"job_version":job.version,"chat_id":task.origin_chat_id,"depends_on":prepare_id}),
                ):
                    if await session.get(Operation,operation_id) is None:
                        session.add(Operation(id=operation_id,kind=kind,actor_id=task.created_by_user_id,job_id=job.id,status="queued",stage_key="accepted",payload=payload,stages=[{"key":"accepted","title":"Принято","status":"succeeded"}],message="Готовим предпросмотр новости для Telegram",last_event_seq=1))
                        session.add(OperationEvent(operation_id=operation_id,sequence=1,stage_key="accepted",status="queued",message="Готовим предпросмотр новости для Telegram"))
            user = await session.get(User, task.created_by_user_id)
            if user is not None:
                user.last_selected_channel_id = task.target_channel_id
            task.status = NewsTaskStatus.COMPLETED
            task.lease_generation+=1
            task.stage = "completed"
            task.stage_message = "Черновик готов"
            import hashlib
            task.result_json = result.model_dump(mode="json")
            session.add(CommandReceipt(principal_id=task.created_by_user_id,scope="news:complete",key=str(task.id),payload_hash=hashlib.sha256(lease_token.encode()).hexdigest(),result={"job_id":job.id}))
            if task.operation_id:
                operation=await session.get(Operation,task.operation_id)
                if operation:
                    operation.status="succeeded";operation.stage_key="complete";operation.message="Черновик готов";operation.result={"publication_id":job.id};operation.version+=1;operation.heartbeat_at=utcnow();operation.job_id=job.id;operation.last_event_seq+=1
                    session.add(OperationEvent(operation_id=operation.id,sequence=operation.last_event_seq,stage_key="complete",status="succeeded",message="Черновик готов"))
            task.job_id = job.id
            task.completed_at = utcnow()
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            return CompletedNewsTask(
                task_id=task.id,
                job_id=job.id,
                origin_chat_id=task.origin_chat_id,
                status_message_id=task.status_message_id,
            )

    async def fail(
        self, task_id: int, *, lease_token: str, error: str, retryable: bool,
    ) -> NewsTask:
        if self.gate:self.gate.assert_mutation_allowed()
        async with self.sessions() as session, session.begin():
            task = await self._leased_task(session, task_id, lease_token)
            retry = retryable and task.attempts < task.max_attempts
            stage_message = (
                "Временная ошибка — задача ожидает повторной обработки"
                if retry else "Обработка завершилась ошибкой"
            )
            updated_id = await session.scalar(
                update(NewsTask)
                .where(*self._active_lease_conditions(task_id, lease_token))
                .values(
                    error_message=" ".join(error.split())[:2000],
                    lease_generation=NewsTask.lease_generation+1,
                    status=NewsTaskStatus.QUEUED if retry else NewsTaskStatus.FAILED,
                    stage="queued" if retry else "failed",
                    stage_message=stage_message,
                    completed_at=None if retry else utcnow(),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                )
                .returning(NewsTask.id)
                .execution_options(synchronize_session=False)
            )
            if updated_id is None:
                raise NewsTaskLeaseError("Аренда задачи недействительна")
            await session.refresh(task)
            return task

    async def cancel(self, task_id: int) -> bool:
        async with self.sessions() as session, session.begin():
            cancelled_id = await session.scalar(
                update(NewsTask)
                .where(
                    NewsTask.id == task_id,
                    NewsTask.status.in_({NewsTaskStatus.QUEUED, NewsTaskStatus.LEASED}),
                )
                .values(
                    status=NewsTaskStatus.CANCELLED,
                    lease_generation=NewsTask.lease_generation+1,
                    stage="cancelled",
                    stage_message="Обработка отменена",
                    completed_at=utcnow(),
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                )
                .returning(NewsTask.id)
                .execution_options(synchronize_session=False)
            )
            return cancelled_id is not None

    @staticmethod
    async def _leased_task(
        session: AsyncSession, task_id: int, lease_token: str,
    ) -> NewsTask:
        task = await session.scalar(
            select(NewsTask).where(NewsTask.id == task_id).with_for_update()
        )
        if (
            task is None
            or task.status != NewsTaskStatus.LEASED
            or not task.lease_token
            or not secrets.compare_digest(task.lease_token.encode("utf-8"), lease_token.encode("utf-8"))
        ):
            raise NewsTaskLeaseError("Аренда задачи недействительна")
        if task.lease_expires_at and _as_utc(task.lease_expires_at) <= datetime.now(UTC):
            raise NewsTaskLeaseError("Срок аренды задачи истёк")
        return task

    @staticmethod
    def _active_lease_conditions(task_id: int, lease_token: str) -> tuple:
        return (
            NewsTask.id == task_id,
            NewsTask.status == NewsTaskStatus.LEASED,
            NewsTask.lease_token == lease_token,
            NewsTask.lease_expires_at.is_not(None),
            NewsTask.lease_expires_at > utcnow(),
        )

    @staticmethod
    async def _expire_exhausted_in_session(session: AsyncSession) -> list[int]:
        now = utcnow()
        result = await session.execute(
            update(NewsTask)
            .where(
                NewsTask.status == NewsTaskStatus.LEASED,
                NewsTask.lease_expires_at.is_not(None),
                NewsTask.lease_expires_at <= now,
                NewsTask.attempts >= NewsTask.max_attempts,
            )
            .values(
                status=NewsTaskStatus.FAILED,
                lease_generation=NewsTask.lease_generation+1,
                stage="failed",
                stage_message="Домашний обработчик не завершил задачу вовремя",
                error_message="Истёк срок последней попытки обработки",
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                completed_at=now,
            )
            .returning(NewsTask.id)
            .execution_options(synchronize_session=False)
        )
        return list(result.scalars())

    @staticmethod
    def _post_from_result(task: NewsTask, result: WorkerResult) -> SourcePost:
        source = result.source
        draft = result.draft
        source_url = source.source_url or ""
        normalized_url = source.normalized_url or source_url or f"manual:{source.source_id}"
        media_items: list[MediaItem] = []
        kind_map = {
            NewsMediaKind.IMAGE: MediaType.IMAGE,
            NewsMediaKind.VIDEO: MediaType.VIDEO,
            NewsMediaKind.ANIMATION: MediaType.ANIMATION,
            NewsMediaKind.DOCUMENT: MediaType.DOCUMENT,
        }
        source_media = [*source.media]
        skipped_remote_media = 0
        for payload in task.input_payload.get("media", []):
            if not isinstance(payload, dict) or not payload.get("telegram_file_id"):
                continue
            try:
                candidate = NewsMedia.from_dict(payload)
            except (KeyError, TypeError, ValueError):
                continue
            if all(
                item.telegram_file_id != candidate.telegram_file_id for item in source_media
            ):
                source_media.append(candidate)
        for media in source_media:
            if len(media_items) >= 10:
                break
            if media.telegram_file_id is None and media.kind != NewsMediaKind.IMAGE:
                # The lightweight VPS media pipeline validates downloaded files as images.
                # Telegram forwards keep their native file_id and therefore retain video/docs.
                skipped_remote_media += 1
                continue
            order = len(media_items)
            filename = media.filename or _media_filename(media.url, order, media.kind.value)
            unique_id = media.metadata.get("telegram_file_unique_id")
            media_items.append(MediaItem(
                url=media.url or f"telegram-media:{unique_id or order}",
                preview_url=media.preview_url,
                filename=filename,
                mime_type=media.mime_type,
                media_type=kind_map[media.kind],
                width=media.width,
                height=media.height,
                order=order,
                telegram_file_id=media.telegram_file_id,
                telegram_file_unique_id=str(unique_id) if unique_id else None,
            ))
        source_label = source.author_name
        if not source_label:
            source_label = str(source.metadata.get("site_name") or "").strip() or None
        if not source_label and source_url:
            source_label = urlparse(source_url).hostname
        if not source_label:
            source_label = (
                "Ручной ввод"
                if source.kind.value == "manual"
                else "Источник"
            )
        return SourcePost(
            provider=f"news-{source.kind.value}",
            source_id=source.source_id,
            source_url=source_url,
            normalized_url=normalized_url,
            title=draft.headline,
            description=draft.lead,
            body=draft.body,
            author_name=source_label[:160],
            author_url=source.author_url or source_url,
            media_items=media_items,
            content_kind=ContentKind.NEWS,
            source_tags=list(draft.suggested_tags),
            published_at=source.published_at,
            metadata={
                **dict(source.metadata),
                "source_title": source.title,
                "source_kind": source.kind.value,
                "facts_used": list(draft.facts_used),
                "warnings": [
                    *draft.warnings,
                    *(
                        ["Удалённое видео/документ не добавлено: перешлите файл боту вручную"]
                        if skipped_remote_media else []
                    ),
                ],
                "model": task.model_name,
                "news_task_id": task.id,
            },
        )


def _media_filename(url: str | None, order: int, kind: str) -> str:
    if url:
        name = PurePosixPath(urlparse(url).path).name
        if name:
            return name[:255]
    extension = {
        "image": ".jpg", "video": ".mp4", "animation": ".gif", "document": ".bin",
    }.get(kind, ".bin")
    return f"news-{order + 1}{extension}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
