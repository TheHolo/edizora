"""Transport-independent commands shared by web and managed Telegram adapters."""
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4, UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, func, or_, text, case
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError

from app.db.models import (User, Channel, Job, Publication, JobEvent, QueueRevision,
    ApplicationState, Operation, OperationEvent, CommandReceipt, MediaAsset, JobAsset,
    AuditEvent, AIWorker, NewsTask, utcnow)
from app.domain.enums import JobStatus, ContentKind
from app.services.job_service import JobService, serialize_post
from app.services.preview_service import deserialize_post
from app.services.caption_service import CaptionService, DEFAULT_TEMPLATE
from app.services.news_render_service import NewsRenderService
from app.utils.tags import normalize_tags, publication_tags
from app.utils.text import telegram_html_length


def iso(value):
    return value.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if value else None


def channel_render_hash(channel):
    return hashlib.sha256(json.dumps({"template":channel.caption_template,"tags":channel.default_tags,"mode":channel.publish_mode},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class PlatformError(Exception):
    def __init__(self, status, code, message, *, current_version=None, current=None, field_errors=None):
        super().__init__(message)
        self.status = self.status_code = status
        self.code, self.message = code, message
        self.current_version, self.current = current_version, current
        self.field_errors = field_errors


class PlatformService:
    def __init__(self, sessions, *, data_root=Path("data"), gate=None):
        self.sessions = sessions
        self.data_root = Path(data_root).resolve()
        self.gate = gate
        self.jobs = JobService(sessions, gate=gate)
        self.jobs.platform = self
        self.runtime = None

    def bind_runtime(self, application):
        self.runtime = application
        application.job_service.platform=self

    async def effective_settings(self,session=None):
        if session is None:
            async with self.sessions() as current:return await self.effective_settings(current)
        defaults={'timezone':'Asia/Vladivostok','auto_add_source_tags':True,'max_tags':20,'max_tag_length':64,
                  'pixiv_media_limit_enabled':True,'pixiv_max_images':10}
        configured=getattr(self.runtime,'settings',None)
        if configured:
            defaults.update({key:getattr(configured,key,value) for key,value in defaults.items()})
        state=await session.get(ApplicationState,1)
        return {**defaults,**{key:value for key,value in (state.settings if state else {}).items() if key in defaults}}
        if getattr(application,'news_tasks',None) is not None:application.news_tasks.platform=self

    async def _selected_ai_target(self,session,kind='news'):
        """Return the explicit global selection; never fail over to another worker."""
        state=await session.get(ApplicationState,1)
        selection=state.settings if state else {}
        target=selection.get('ai_selections',{}).get(kind)
        if target:
            worker=await session.get(AIWorker,target['worker_id'])
            if worker is None or worker.revoked_at:return None,None
            return worker.id,target['model_id']
        if selection.get('ai_worker_id'):
            worker=await session.get(AIWorker,selection['ai_worker_id'])
            if worker is None or worker.revoked_at:return None,None
            model=selection.get('ai_model') or worker.selected_model
            return (worker.id,model) if model else (None,None)
        # Compatibility for a single selected worker before global selection was stored.
        candidates=(await session.scalars(select(AIWorker).where(AIWorker.revoked_at.is_(None),AIWorker.selected_model.is_not(None)).limit(2))).all()
        if len(candidates)==1:return candidates[0].id,candidates[0].selected_model
        return None,None

    async def validate_actor(self,actor_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
        return True

    async def user_for_telegram(self,telegram_id):
        async with self.sessions() as session:
            user=await session.scalar(select(User).where(User.telegram_user_id==telegram_id))
            if not user:raise PlatformError(403,"forbidden","Администратор не настроен в этой установке")
            await self._actor(session,user.id)
            return user.id

    async def ping(self):
        async with self.sessions() as session:
            await session.scalar(text("SELECT 1"))
        return {"status":"ok"}

    async def deployment_readiness(self):
        issues=[]
        async with self.sessions() as session:
            jobs=(await session.scalars(select(Job).options(selectinload(Job.channel)).where(Job.status.in_(["queued","scheduled"])))).all()
            for job in jobs:
                selected=list((await session.execute(select(JobAsset,MediaAsset).join(MediaAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==job.id,JobAsset.selected.is_(True)))).all())
                ready,reasons=self._readiness(job,selected)
                from app.storage.assets import safe_path
                for _,asset in selected:
                    if asset.telegram_file_id:continue
                    try:exists=bool(asset.storage_key and safe_path(self.data_root,asset.storage_key).is_file())
                    except ValueError:exists=False
                    if not exists:reasons.append("Обязательный файл отсутствует на активном узле")
                if reasons:issues.append({"job_id":job.id,"reasons":list(dict.fromkeys(reasons))})
            waiting=await session.scalar(select(func.count()).select_from(NewsTask).where(NewsTask.status.in_(["queued","leased"])))
        now=utcnow()
        scheduled=[j for j in jobs if j.status=="scheduled"]
        return {"ready":not issues,"issues":issues,"counts":{"queued":len(jobs),"waiting_ai":waiting},"ai_can_wait":True,
                "scheduled_job_ids":[j.id for j in scheduled],"overdue_job_ids":[j.id for j in scheduled if j.scheduled_at and j.scheduled_at.replace(tzinfo=UTC)<=now]}

    async def prepare_handoff(self,migration_id,generation,overdue_policy="catch_up",catch_up_interval_seconds=60,confirmed_skip_job_ids=None):
        """Trusted controller command, only after writers drain and before snapshot freeze."""
        if overdue_policy not in {"catch_up","hold","skip"} or not isinstance(catch_up_interval_seconds,int) or catch_up_interval_seconds<1:
            raise PlatformError(422,"invalid_overdue_policy","Неверная политика просроченных публикаций")
        if overdue_policy=="skip" and (not isinstance(confirmed_skip_job_ids,list) or any(type(i) is not int or i<1 for i in confirmed_skip_job_ids) or len(confirmed_skip_job_ids)!=len(set(confirmed_skip_job_ids))):
            raise PlatformError(422,"skip_confirmation_required","Подтвердите конкретный список публикаций, которые можно пропустить")
        if self.gate:
            identity=self.gate.identity()
            blocks=self.gate.status()["blocks"]
            if identity["role"]!="active" or identity["generation"]!=generation or not any(b["reason"] in {"migration","backup"} and b["owner"]==migration_id for b in blocks):
                raise PlatformError(409,"invalid_handoff_context","Не подтверждена остановка текущего владельца")
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            state=await session.get(ApplicationState,1)
            if not state:
                state=ApplicationState(id=1,system_id=self.gate.identity()["system_id"] if self.gate else str(uuid4()),version=1,settings={},publication_blocks={},maintenance_state="normal");session.add(state)
            state.settings={**state.settings,"overdue_policy":overdue_policy,"catch_up_interval_seconds":catch_up_interval_seconds,"confirmed_skip_job_ids":list(confirmed_skip_job_ids or [])}
            tasks=(await session.scalars(select(NewsTask).where(NewsTask.status.in_(["leased","finalizing"])))).all()
            for task in tasks:
                task.status="queued";task.lease_generation+=1;task.lease_owner=None;task.lease_token=None;task.lease_expires_at=None;task.stage="queued";task.stage_message="Аренда отозвана при переносе; ждём исполнителя"
            from app.application.scheduling import apply_overdue_in_session
            affected=await apply_overdue_in_session(session,state=state)
            state.settings={**state.settings,"last_handoff":{"migration_id":migration_id,"generation":generation,"revoked_tasks":[t.id for t in tasks]}}
            state.version+=1
            await session.commit()
        return {"migration_id":migration_id,"generation":generation,"revoked_task_ids":[t.id for t in tasks],"overdue":affected,"policy":overdue_policy}

    async def apply_overdue_policy(self):
        if self.gate:self.gate.assert_mutation_allowed()
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            from app.application.scheduling import apply_overdue_in_session
            result=await apply_overdue_in_session(session)
            await session.commit()
            return result

    async def album(self,actor_id,job_id):
        dto=await self.get_publication(actor_id,job_id)
        return {"items":dto["assets"],"version":dto["version"],"selected":dto["album"]["selected"],"total":dto["album"]["total"]}

    async def put_album(self,actor_id,job_id,payload):
        updated=await self.patch_publication(actor_id,job_id,payload)
        return {"items":updated["assets"],"version":updated["version"]}

    async def archive_channel(self,actor_id,channel_id,payload):
        return await self.patch_channel(actor_id,channel_id,{"expected_version":payload.get("expected_version"),"is_enabled":False,"is_default":False})

    async def asset_content(self,actor_id,asset_id,thumbnail=False):
        return await self.asset(actor_id,asset_id,thumbnail)

    async def asset_metadata(self,actor_id,asset_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            asset=await session.get(MediaAsset,asset_id)
            if not asset:raise PlatformError(404,"not_found","Медиафайл не найден")
            return {"id":asset.id,"state":asset.state,"mime_type":asset.mime_type,"size":asset.size,"width":asset.width,"height":asset.height,"filename":asset.filename,"url":f"/api/v1/assets/{asset.id}/content" if asset.storage_key else None,"thumbnail_url":f"/api/v1/assets/{asset.id}/thumbnail" if asset.thumbnail_key else None}

    async def upload_asset(self,actor_id,job_id,payload=None,filename=None,content=None,idempotency_key=None):
        # Accept bytes through one application boundary; filename is metadata only.
        from app.storage.assets import store_bytes
        if isinstance(job_id,(bytes,bytearray)):
            async with self._write(actor_id) as session:
                try:stored=store_bytes(self.data_root,bytes(job_id))
                except ValueError as error:raise PlatformError(422,"invalid_media","Файл не является допустимым изображением") from error
                asset=MediaAsset(**stored,source_kind="upload",filename=Path(str(filename or "image")).name[:255])
                session.add(asset);await session.flush()
                return {"id":asset.id,"asset_id":asset.id,"state":asset.state,"url":f"/api/v1/assets/{asset.id}/content","thumbnail_url":f"/api/v1/assets/{asset.id}/thumbnail","filename":asset.filename,"mime_type":asset.mime_type}
        if isinstance(payload,dict):
            expected=payload.get("expected_version");raw=content if content is not None else payload.get("content",b"")
            name=filename or payload.get("filename","image")
        else:
            expected=None;raw=payload;name=filename or "image"
        async with self._write(actor_id) as session:
            job=await self._job(session,job_id)
            self._version(job,{"expected_version":expected})
            if job.status!="waiting_confirmation":raise PlatformError(409,"invalid_state","Загрузка доступна в черновике")
            try:stored=store_bytes(self.data_root,raw)
            except ValueError as error:raise PlatformError(422,"invalid_media","Файл не является допустимым изображением") from error
            asset=MediaAsset(**stored,source_kind="upload",filename=Path(str(name)).name[:255])
            session.add(asset);await session.flush()
            count=await session.scalar(select(func.count()).select_from(JobAsset).where(JobAsset.job_id==job.id))
            session.add(JobAsset(job_id=job.id,asset_id=asset.id,source_index=count+1,selected=True,sort_order=count,purpose="original"))
            data=dict(job.post_data);media=list(data.get("media_items",[]));media.append(self._media_payload(asset,len(media)));data["media_items"]=media
            job.post_data=data;job.render_plan=None
            await session.flush()
            return await self._dto(session,job,detail=True)

    async def _actor(self, session, actor_id):
        user = await session.get(User, actor_id)
        if not user or not user.is_active or not user.is_admin:
            raise PlatformError(403, "forbidden", "Доступ разрешён только действующему администратору")
        return user

    @asynccontextmanager
    async def _write(self, actor_id):
        if self.gate:
            try:
                self.gate.assert_mutation_allowed()
            except RuntimeError as error:
                raise PlatformError(503, "maintenance", "Изменения временно заблокированы: выполняется перенос или восстановление") from error
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            try:
                await self._actor(session, actor_id)
                state = await session.get(ApplicationState, 1)
                if state and state.maintenance_state != "normal":
                    raise PlatformError(503, "maintenance", "Система остановлена для обслуживания")
                yield session
                await session.commit()
            except StaleDataError as error:
                await session.rollback()
                raise PlatformError(409,"version_conflict","Запись изменилась в другом интерфейсе. Обновите её и сверьте правки") from error
            except BaseException:
                await session.rollback()
                raise

    async def _receipt(self, session, actor_id, scope, key, payload):
        try:
            UUID(str(key))
        except (ValueError, TypeError, AttributeError):
            raise PlatformError(422, "idempotency_key_required", "Требуется UUID Idempotency-Key")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()
        previous = await session.scalar(select(CommandReceipt).where(CommandReceipt.principal_id == actor_id, CommandReceipt.scope == scope, CommandReceipt.key == key))
        if previous:
            if previous.payload_hash != digest:
                raise PlatformError(409, "idempotency_conflict", "Этот ключ уже использован для другой команды")
            return previous.result, digest
        return None, digest

    def _save_receipt(self, session, actor_id, scope, key, digest, result):
        session.add(CommandReceipt(principal_id=actor_id, scope=scope, key=key, payload_hash=digest, result=result))

    def _audit(self, session, actor_id, action, resource_id, **details):
        session.add(AuditEvent(actor_id=actor_id, action=action, resource_id=str(resource_id), details=details))

    async def _invalidate_channel_plans(self,session,channel_id):
        pending=(await session.scalars(select(Job).where(Job.target_channel_id==channel_id,Job.status.in_(["waiting_confirmation","queued","scheduled"])))).all()
        for job in pending:job.render_plan=None

    async def _job(self, session, job_id):
        job = await session.scalar(select(Job).options(selectinload(Job.channel)).where(Job.id == job_id))
        if job is None:
            raise PlatformError(404, "not_found", "Публикация не найдена")
        return job

    def _version(self, record, payload):
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool):
            raise PlatformError(422, "expected_version_required", "Укажите версию редактируемой записи")
        if record.version != expected:
            raise PlatformError(409, "version_conflict", "Запись уже изменена. Сверьте новую версию", current_version=record.version)

    async def list_publications(self, actor_id, *, limit=50, cursor=None, status=None, channel_id=None, search=None, **filters):
        if limit < 1 or limit > 100:
            raise PlatformError(422, "invalid_limit", "Размер страницы должен быть от 1 до 100")
        async with self.sessions() as session:
            await self._actor(session, actor_id)
            query = select(Job).options(selectinload(Job.channel))
            if status:
                groups = {"draft": ["waiting_confirmation", "created", "resolving"], "history": ["completed", "failed", "cancelled"], "queue": ["queued", "scheduled", "downloading", "processing", "publishing"]}
                if status == "uncertain":
                    query = query.where(Job.error_code == "uncertain_publish")
                elif status in groups:
                    query = query.where(Job.status.in_(groups[status]))
                elif status in {s.value for s in JobStatus}:
                    query = query.where(Job.status == status)
                else:
                    raise PlatformError(422, "invalid_status", "Неизвестный фильтр состояния")
            if channel_id:
                query = query.where(Job.target_channel_id == channel_id)
            if filters.get('content_kind'):
                kind=filters['content_kind']
                if kind not in {'artwork','news','text'}:raise PlatformError(422,'invalid_kind','Неизвестный тип публикации')
                text_only=func.coalesce(Job.post_data['metadata']['text_only'].as_boolean(),False)
                query=query.where(text_only.is_(True)) if kind=='text' else query.where(Job.content_kind==kind,text_only.is_(False))
            state=await session.get(ApplicationState,1)
            timezone=ZoneInfo((state.settings if state else {}).get('timezone','Asia/Vladivostok'))
            for name,upper in (('date_from',False),('date_to',True)):
                if filters.get(name):
                    try:
                        raw=filters[name]
                        moment=datetime.fromisoformat(raw.replace('Z','+00:00'))
                        if moment.tzinfo is None:moment=moment.replace(tzinfo=timezone)
                        if upper and len(raw)==10:moment+=timedelta(days=1)
                        moment=moment.astimezone(UTC)
                    except (ValueError,TypeError):raise PlatformError(422,'invalid_date','Некорректная дата фильтра') from None
                    query=query.where(Job.created_at<moment) if upper else query.where(Job.created_at>=moment)
            if search:
                escaped = search[:200].replace("%", "\\%").replace("_", "\\_")
                query = query.where(or_(Job.source_url.ilike(f"%{escaped}%", escape="\\"), Job.post_data["title"].as_string().ilike(f"%{escaped}%", escape="\\")))
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            if cursor:
                try:
                    after = int(cursor)
                except ValueError:
                    raise PlatformError(422, "invalid_cursor", "Некорректная страница")
                query = query.where(Job.id < after)
            jobs = list((await session.scalars(query.order_by(Job.id.desc()).limit(limit + 1))).all())
            return {"items": [await self._dto(session, j) for j in jobs[:limit]], "next_cursor": str(jobs[limit - 1].id) if len(jobs) > limit else None, "total": total}

    async def get_publication(self, actor_id, job_id):
        async with self.sessions() as session:
            await self._actor(session, actor_id)
            return await self._dto(session, await self._job(session, job_id), detail=True)

    async def _dto(self, session, job, detail=False):
        data = job.post_data or {}
        items = data.get("media_items", [])
        links = list((await session.execute(select(JobAsset, MediaAsset).join(MediaAsset, JobAsset.asset_id == MediaAsset.id).where(JobAsset.job_id == job.id).order_by(JobAsset.sort_order))).all())
        selected = [(link, asset) for link, asset in links if link.selected]
        settings=await self.effective_settings(session)
        from app.domain.album import read_album
        initial_numbers=read_album(data).initial
        ready, reasons = self._readiness(job, selected)
        if job.content_kind=='artwork' and settings['pixiv_media_limit_enabled'] and len(items)>settings['pixiv_max_images']:
            ready=False;reasons.append('Выбор превышает сохранённый лимит изображений')
        operation = await session.scalar(select(Operation.id).where(Operation.job_id == job.id).order_by(Operation.created_at.desc()).limit(1))
        editable = job.status in {"waiting_confirmation", "queued", "scheduled"}
        actions = ["save", "cancel"] if editable else []
        if job.status == "waiting_confirmation":
            actions += ["prepare","translate","reimport"] + (["enqueue", "schedule", "publish-now","preview-in-telegram"] if ready else [])
        if job.status in {"waiting_confirmation", "failed"} and job.normalized_url and not job.allow_duplicate and job.error_code != "uncertain_publish":
            actions.append("confirm-duplicate")
        if job.status in {"queued", "scheduled"}:
            actions += ["return-to-draft", "schedule"] + (["publish-now"] if ready else [])
            if not ready: actions.append("prepare")
        if job.status == "failed":
            actions += ["resolve-uncertain"] if job.error_code == "uncertain_publish" else ["retry"]
        value = {"id": job.id, "version": job.version, "content_kind": "text" if data.get("metadata", {}).get("text_only") else job.content_kind,
                 "status": job.status, "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
                 "title": data.get("title", "Без названия"),
                 "source": {"provider": job.provider, "id": job.source_id, "url": job.source_url, "author": data.get("author_name"), "date": data.get("published_at")},
                 "channel": self._channel_dto(job.channel), "tags": job.user_tags,
                 "album": {"selected": len(items), "total": len(links) or len(items), "cover": f"/api/v1/assets/{selected[0][1].id}/thumbnail" if selected and selected[0][1].thumbnail_key else None,
                    'initial_selected_asset_ids':[asset.id for link,asset in links if link.source_index in initial_numbers],
                    'selection_limit':settings['pixiv_max_images'] if settings['pixiv_media_limit_enabled'] and job.content_kind=='artwork' else None},
                 "readiness": {"ready": ready, "reasons": reasons},
                 "schedule": {"kind": "exact" if job.scheduled_at else "queue", "utc": iso(job.scheduled_at), "timezone": "Asia/Vladivostok", "estimate": iso(job.channel.next_publish_at)},
                 "last_operation_id": operation, "allowed_actions": actions, "blocked_reason": reasons[0] if reasons else None,
                 "updated_at": iso(job.updated_at), "created_at": iso(job.created_at), "queue_position": job.queue_position}
        if detail:
            value.update({"description": data.get("description", ""), "body": data.get("body", ""), "caption_override": job.caption_override,
                          "assets": [{"id": a.id, "source_index": l.source_index, "selected": l.selected, "order": l.sort_order, "filename": a.filename, "mime_type": a.mime_type, "state": a.state, "width": a.width, "height": a.height, "size": a.size,
                            "url": f"/api/v1/assets/{a.id}/content" if a.state == "available" else None, "thumbnail_url": f"/api/v1/assets/{a.id}/thumbnail" if a.thumbnail_key else None} for l,a in links]})
            value.update({"user_tags":job.user_tags,"manual_caption":job.caption_override,"channel_id":job.target_channel_id,"title_translation":data.get("metadata",{}).get("title_translation","")})
        return value

    def _readiness(self, job, selected):
        reasons = []
        if not job.channel.is_enabled:
            reasons.append("Канал выключен")
        if job.error_code == "uncertain_publish":
            reasons.append("Результат прошлой отправки неизвестен: сначала проверьте канал")
        if job.content_kind == "artwork" and not job.post_data.get("media_items"):
            reasons.append("Выберите хотя бы одно изображение")
        items = job.post_data.get("media_items", [])
        if items and (len(selected) != len(items) or any(a.state != "available" and not a.telegram_file_id for _,a in selected)):
            reasons.append("Медиа ещё не подготовлены на активном узле")
        if not job.render_plan or job.render_plan.get("job_version") != job.version or (job.render_plan.get("channel_render_hash") != channel_render_hash(job.channel) if "channel_render_hash" in job.render_plan else job.render_plan.get("channel_version") != job.channel.version):
            reasons.append("Требуется подготовить предпросмотр")
        return not reasons, reasons

    @staticmethod
    def _channel_dto(c):
        return {"id": c.id, "version": c.version, "alias": c.alias, "title": c.title, "telegram_chat_id": c.telegram_chat_id,
                "is_enabled": c.is_enabled, "is_paused": c.is_paused, "is_default": c.is_default,
                "publish_mode": c.publish_mode, "publish_interval_seconds": c.publish_interval_seconds,
                "caption_template": c.caption_template, "default_tags": c.default_tags, "next_publish_at": iso(c.next_publish_at), "active_job_id": c.active_job_id,
                "permissions": {"status": "unchecked", "checked_at": None}, "member_count": None, "member_count_updated_at": None}

    async def create_publication(self, actor_id, payload, idempotency_key):
        async with self._write(actor_id) as session:
            scope = "publication:create"
            previous, digest = await self._receipt(session, actor_id, scope, idempotency_key, payload)
            if previous is not None:
                return previous
            channel = await session.get(Channel, payload.get("channel_id") or payload.get("target_channel_id")) if (payload.get("channel_id") or payload.get("target_channel_id")) else await session.scalar(select(Channel).where(Channel.is_default.is_(True)))
            if not channel or not channel.is_enabled:
                raise PlatformError(422, "invalid_channel", "Выберите включённый канал")
            kind = payload.get("content_kind", payload.get("kind", "artwork"))
            if kind not in {"artwork", "images", "news", "text"}:
                raise PlatformError(422, "invalid_kind", "Неизвестный тип публикации")
            source = str(payload.get("source_url", payload.get("source", "")) or "").strip()
            urls = payload.get("urls") or ([source] if source else [])
            settings=await self.effective_settings(session)
            tags = normalize_tags(payload.get("user_tags", payload.get("tags", [])), settings['max_tags'],settings['max_tag_length'])
            if len(urls) > 10:
                raise PlatformError(422, "too_many_sources", "За один импорт можно добавить до 10 ссылок")
            if kind in {"artwork", "images"} and urls:
                op = self._new_operation(session, actor_id, "import", {"urls": urls, "channel_id": channel.id, "tags": tags}, "Ожидает импорта")
                result = {"operation_id": op.id, "status_url": f"/api/v1/operations/{op.id}"}
            elif kind == "news" and (urls or payload.get("rewrite", True)):
                op = self._new_operation(session, actor_id, "news", {}, "Ждём домашний компьютер", status="waiting_worker")
                from app.news.classifier import classify_news_input
                try:request=classify_news_input(urls[0] if urls else str(payload.get("body",payload.get("text",""))))
                except Exception as error:raise PlatformError(422,"invalid_source","Укажите поддерживаемую публичную ссылку или исходный текст") from error
                task_input={"kind":request.kind.value,"source_text" if request.kind.value=="manual" else "source_url":request.value}
                upload_ids=payload.get("asset_ids",[])
                if not isinstance(upload_ids,list) or len(upload_ids)>100 or len(set(upload_ids))!=len(upload_ids):
                    raise PlatformError(422,"invalid_assets","Неверный список загруженных медиа")
                for asset_id in upload_ids:
                    uploaded=await session.get(MediaAsset,asset_id)
                    if not uploaded or uploaded.source_kind!="upload" or uploaded.state!="available":
                        raise PlatformError(422,"invalid_assets","Загруженный файл недоступен")
                if upload_ids:task_input["metadata"]={"upload_asset_ids":upload_ids}
                selected_worker,selected_model=await self._selected_ai_target(session)
                target={'worker_id':selected_worker} if selected_worker else {'awaiting_selection':True}
                legacy_settings=getattr(self.runtime,'settings',None) if self.runtime else None
                if not selected_worker and legacy_settings and getattr(legacy_settings,'news_worker_token',None) and not await session.scalar(select(func.count()).select_from(AIWorker)):
                    selected_model=getattr(legacy_settings,'news_model_name',None)
                    if selected_model:target={'legacy_worker_allowed':True}
                task_input['metadata']={**task_input.get('metadata',{}),**target}
                task = NewsTask(created_by_user_id=actor_id, target_channel_id=channel.id, origin_chat_id=None, source_kind=request.kind.value, input_payload=task_input, user_tags=tags, operation_id=op.id, model_name=selected_model,status="queued", stage="queued", stage_message="Ждём домашний компьютер", max_attempts=3)
                session.add(task)
                await session.flush()
                op.payload = {"task_id": task.id}
                result = {"operation_id": op.id, "task_id": task.id, "status_url": f"/api/v1/operations/{op.id}"}
            else:
                body = str(payload.get("body", payload.get("text", "")))
                title = str(payload.get("title", ""))[:1000]
                if not body.strip() and not title.strip() and kind in {"text", "news"}:
                    raise PlatformError(422, "empty_text", "Введите текст публикации")
                data = {"schema_version": 1, "provider": "manual", "source_id": str(uuid4()), "source_url": source, "normalized_url": source,
                        "title": title or body[:80] or "Без названия", "description": str(payload.get("description", "")), "body": body,
                        "author_name": "", "author_url": "", "source_tags": [], "media_items": [], "metadata": {"text_only": kind == "text"}, "content_kind": "news" if kind in {"text", "news"} else "artwork"}
                job = Job(created_by_user_id=actor_id, target_channel_id=channel.id, provider="manual", source_id=data["source_id"], source_url=source, normalized_url=source, post_data=data, user_tags=tags, source_tags=[], content_kind=data["content_kind"], status="waiting_confirmation", max_attempts=3)
                session.add(job)
                await session.flush()
                job.channel = channel
                asset_ids=payload.get('asset_ids',[])
                if not isinstance(asset_ids,list) or len(asset_ids)>100 or len(asset_ids)!=len(set(asset_ids)):
                    raise PlatformError(422,'invalid_assets','Неверный набор загруженных файлов')
                media=[]
                for index,asset_id in enumerate(asset_ids):
                    asset=await session.get(MediaAsset,asset_id)
                    if not asset or asset.source_kind!='upload' or asset.state!='available':raise PlatformError(422,'invalid_assets','Загруженный файл недоступен')
                    session.add(JobAsset(job_id=job.id,asset_id=asset.id,source_index=index+1,selected=True,sort_order=index,purpose='original'))
                    media.append(self._media_payload(asset,index))
                if media:
                    job.post_data={**data,'media_items':media}
                    await session.flush()
                result = {"resource_id": job.id, "publication_id": job.id, "publication": await self._dto(session, job)}
                self._audit(session, actor_id, "publication.create", job.id)
            self._save_receipt(session, actor_id, scope, idempotency_key, digest, result)
            return result

    def _new_operation(self, session, actor_id, kind, payload, message, *, status="queued", job_id=None):
        op = Operation(id=str(uuid4()), kind=kind, actor_id=actor_id, payload=payload, job_id=job_id, status=status, stage_key="accepted", stages=[{"key": "accepted", "title": "Принято", "status": "succeeded"}], message=message, last_event_seq=1)
        session.add(op)
        session.add(OperationEvent(operation_id=op.id, sequence=1, stage_key="accepted", status=status, message=message))
        return op

    async def patch_publication(self, actor_id, job_id, payload):
        payload=dict(payload)
        if "manual_caption" in payload:payload["caption_override"]=payload.pop("manual_caption")
        async with self._write(actor_id) as session:
            job = await self._job(session, job_id)
            self._version(job, payload)
            if job.status not in {"waiting_confirmation", "queued", "scheduled"} or job.channel.active_job_id == job.id:
                raise PlatformError(409, "invalid_state", "Активную или завершённую публикацию нельзя редактировать")
            allowed = {"expected_version", "title", "description", "body", "title_translation", "caption_override", "user_tags", "tags", "channel_id", "target_channel_id", "selected_asset_ids", "asset_order", "asset_ids"}
            if set(payload) - allowed:
                raise PlatformError(422, "invalid_field", "Неизвестное поле публикации")
            if job.status != "waiting_confirmation" and set(payload) & {"title", "title_translation", "description", "body", "selected_asset_ids", "asset_order", "asset_ids", "channel_id", "target_channel_id"}:
                raise PlatformError(409, "return_to_draft", "Верните публикацию в черновик для изменения содержимого")
            data = dict(job.post_data)
            for key in ("title", "description", "body"):
                if key in payload:
                    if not isinstance(payload[key], str) or len(payload[key]) > 50000:
                        raise PlatformError(422, "invalid_text", "Слишком длинный или неверный текст")
                    data[key] = payload[key]
            if "title" in payload:
                metadata = dict(data.get("metadata", {}))
                for key in list(metadata):
                    if key.startswith("title_translation"):
                        metadata.pop(key)
                data["metadata"] = metadata
            if "title_translation" in payload:
                if not isinstance(payload["title_translation"],str) or len(payload["title_translation"])>2000:
                    raise PlatformError(422,"invalid_translation","Неверный текст перевода")
                data["metadata"]={**data.get("metadata",{}),"title_translation":payload["title_translation"]}
            data["schema_version"] = 1
            job.post_data = data
            if "caption_override" in payload:
                job.caption_override = payload["caption_override"]
            if "user_tags" in payload or "tags" in payload:
                settings=await self.effective_settings(session)
                job.user_tags = normalize_tags(payload.get("user_tags", payload.get("tags")), settings['max_tags'],settings['max_tag_length'])
            if "channel_id" in payload or "target_channel_id" in payload:
                channel = await session.get(Channel, payload.get("channel_id", payload.get("target_channel_id")))
                if not channel or not channel.is_enabled:
                    raise PlatformError(422, "invalid_channel", "Канал недоступен")
                job.target_channel_id, job.channel = channel.id, channel
            if "asset_ids" in payload:
                ids=payload["asset_ids"]
                if not isinstance(ids,list) or len(ids)>100 or len(ids)!=len(set(ids)):
                    raise PlatformError(422,"invalid_assets","Неверный список медиафайлов")
                existing=list((await session.scalars(select(JobAsset).where(JobAsset.job_id==job.id))).all())
                for asset_id in ids:
                    if asset_id not in {l.asset_id for l in existing}:
                        asset=await session.get(MediaAsset,asset_id)
                        if not asset or asset.source_kind!="upload":raise PlatformError(422,"invalid_assets","Загруженный файл не найден")
                        link=JobAsset(job_id=job.id,asset_id=asset.id,source_index=max((l.source_index for l in existing),default=0)+1,selected=True,sort_order=ids.index(asset_id),purpose="original")
                        session.add(link);existing.append(link)
                await session.flush()
                payload={**payload,"selected_asset_ids":ids,"asset_order":ids}
            if "selected_asset_ids" in payload or "asset_order" in payload:
                links = list((await session.scalars(select(JobAsset).where(JobAsset.job_id == job.id))).all())
                selected_ids = payload.get("selected_asset_ids", [l.asset_id for l in links if l.selected])
                order = payload.get("asset_order", selected_ids)
                settings=await self.effective_settings(session)
                if job.content_kind=='artwork' and settings['pixiv_media_limit_enabled'] and len(selected_ids)>settings['pixiv_max_images']:
                    raise PlatformError(422,'selection_limit','Выбор превышает сохранённый лимит изображений')
                if len(order) != len(set(order)) or set(order) != set(selected_ids) or not set(order) <= {l.asset_id for l in links}:
                    raise PlatformError(422, "invalid_selection", "Выбор и порядок кадров не совпадают с альбомом")
                from app.domain.album import read_album,select_album
                previous_album=read_album(data)
                catalog=dict(previous_album.catalog)
                media = [];selected_numbers=[]
                for link in links:
                    asset=await session.get(MediaAsset,link.asset_id)
                    if link.source_index not in catalog:catalog[link.source_index]=self._media_payload(asset,link.sort_order)
                    link.selected = link.asset_id in selected_ids
                    if link.selected:
                        link.sort_order = order.index(link.asset_id)
                        selected_numbers.append((link.sort_order,link.source_index))
                metadata={**data.get('metadata',{}),'artwork_album':{'catalog':{str(n):item for n,item in catalog.items()},'selection':previous_album.selection,'initial':previous_album.initial,'revision':previous_album.revision}}
                data={**data,'metadata':metadata}
                data=select_album(data,[number for _,number in sorted(selected_numbers)],limit=settings['pixiv_max_images'] if settings['pixiv_media_limit_enabled'] and job.content_kind=='artwork' else None)
                job.post_data=data
            prepared = job.render_plan
            job.version += 1
            job.render_plan = None
            if job.status in {"queued", "scheduled"} and prepared:
                # Caption/tag edits reuse immutable prepared media, never trigger AI/downloads.
                from app.application.prepared_media import media_parts
                with session.no_autoflush:
                    plan = self.render_plan(job, settings=await self.effective_settings(session))
                    links = list((await session.execute(select(JobAsset, MediaAsset).join(MediaAsset, JobAsset.asset_id == MediaAsset.id)
                        .where(JobAsset.job_id == job.id, JobAsset.selected.is_(True)).order_by(JobAsset.sort_order))).all())
                media = prepared.get('media')
                if media is None:
                    from app.application.prepared_media import load_prepared_media
                    try:
                        legacy = load_prepared_media(prepared, links, deserialize_post(job.post_data), self.data_root, job.channel.publish_mode)
                    except ValueError as error:
                        raise PlatformError(409, 'not_ready', 'Сначала повторно подготовьте обязательные медиа') from error
                    media = [{'source_asset_id':asset.id,'asset_id':asset.id,'storage_key':asset.storage_key,
                        'telegram_file_id':item.telegram_file_id,'media_type':item.media_type.value,
                        'as_document':item.as_document,'order':link.sort_order} for item,(link,asset) in zip(legacy,links,strict=True)]
                plan['media'] = media
                plan['assets_prepared'] = True
                plan['parts'] = media_parts(plan['media'], links, plan['html'], text_separate=job.content_kind == 'news' and plan['text_length'] > 1024)
                job.render_plan = plan
            await session.flush()
            self._audit(session, actor_id, "publication.update", job.id, version=job.version)
            return await self._dto(session, job, detail=True)

    @staticmethod
    def _media_payload(asset, order):
        return {"url": asset.source_reference or "", "preview_url": None, "filename": asset.filename or "media", "mime_type": asset.mime_type, "media_type": "image", "width": asset.width, "height": asset.height, "size": asset.size, "telegram_file_id": asset.telegram_file_id, "telegram_file_unique_id": None, "order": order, "headers": {}}

    async def publication_action(self, actor_id, job_id, action, payload, idempotency_key):
        async with self._write(actor_id) as session:
            scope = f"publication:{job_id}:{action}"
            previous,digest = await self._receipt(session,actor_id,scope,idempotency_key,payload)
            if previous is not None:
                return previous
            job = await self._job(session, job_id)
            self._version(job, payload)
            old_status = job.status
            if action in {"prepare", "refresh-media", "translate"}:
                if job.status != "waiting_confirmation" and not (action == "prepare" and job.status in {"queued", "scheduled"}):
                    raise PlatformError(409,"invalid_state","Подготовка доступна только для черновика")
                kind = "title_translation" if action == "translate" else "prepare"
                op = self._new_operation(session,actor_id,kind,{"job_version":job.version},"Ожидает подготовки",job_id=job.id)
                result = {"operation_id":op.id,"resource_id":job.id,"status_url":f"/api/v1/operations/{op.id}"}
            else:
                if action in {"enqueue","schedule","publish-now","retry"}:
                    if job.error_code == "uncertain_publish":
                        raise PlatformError(409,"uncertain_publish","Сначала запишите результат ручной проверки канала")
                    if job.status not in ({"failed"} if action == "retry" else {"waiting_confirmation","queued","scheduled"}):
                        raise PlatformError(409,"invalid_state","Переход из текущего состояния запрещён")
                    if action == "enqueue" and job.status != "waiting_confirmation":
                        raise PlatformError(409,"already_queued","Публикация уже в очереди")
                    if job.channel.active_job_id:
                        raise PlatformError(409,"channel_busy","Канал сейчас занят отправкой")
                    links = list((await session.execute(select(JobAsset,MediaAsset).join(MediaAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==job.id,JobAsset.selected.is_(True)))).all())
                    ready,reasons = self._readiness(job,links)
                    settings=await self.effective_settings(session)
                    if job.content_kind=='artwork' and settings['pixiv_media_limit_enabled'] and len(links)>settings['pixiv_max_images']:
                        ready=False;reasons.append('Выбор превышает сохранённый лимит изображений')
                    if not ready:
                        raise PlatformError(409,"not_ready","; ".join(reasons))
                    if action == "publish-now":
                        if self.gate:
                            try: self.gate.assert_publication_allowed()
                            except RuntimeError as error: raise PlatformError(503,"publication_blocked","Публикации остановлены") from error
                        state=await session.get(ApplicationState,1)
                        if state and state.publication_blocks:
                            raise PlatformError(503,"publication_blocked","Публикации остановлены")
                    duplicate=await session.scalar(select(Job.id).where(Job.id!=job.id,Job.target_channel_id==job.target_channel_id,Job.normalized_url==job.normalized_url,Job.normalized_url!="",Job.status.in_(["waiting_confirmation","queued","scheduled","downloading","processing","publishing","completed"])).limit(1))
                    if duplicate and not job.allow_duplicate:
                        if payload.get("confirm_duplicate") is not True:
                            raise PlatformError(409,"duplicate","Этот источник уже добавлен в канал; подтвердите повтор отдельно")
                        job.allow_duplicate=True
                    if action=="schedule":
                        raw=payload.get("scheduled_at",payload.get("utc"))
                        if not raw:
                            job.scheduled_at=None
                        else:
                            try:
                                moment=datetime.fromisoformat(raw.replace("Z","+00:00"))
                                if moment.tzinfo is None: raise ValueError()
                                job.scheduled_at=moment.astimezone(UTC)
                            except (ValueError,TypeError): raise PlatformError(422,"invalid_datetime","Укажите точное время с часовым поясом")
                    if action=="publish-now":
                        job.force_publish=True
                        job.scheduled_at=None
                    job.status="scheduled" if job.scheduled_at else "queued"
                    job.queue_position=None if job.scheduled_at else (await session.scalar(select(func.coalesce(func.max(Job.queue_position),0)).where(Job.target_channel_id==job.target_channel_id)))+1
                    job.error_code=job.error_message=None
                    job.cancel_requested=False
                    job.finished_at=None
                    if action=="retry": job.attempts=0
                    # Queue transitions keep the same immutable prepared content.
                    plan=dict(job.render_plan)
                    job.version+=1
                    plan["job_version"]=job.version
                    job.render_plan=plan
                elif action=="return-to-draft":
                    if job.status not in {"queued","scheduled"} or job.channel.active_job_id==job.id:
                        raise PlatformError(409,"invalid_state","Уже начатую отправку нельзя вернуть в черновик")
                    job.status="waiting_confirmation";job.queue_position=None;job.scheduled_at=None;job.force_publish=False
                elif action=="cancel":
                    if job.status in {"completed","cancelled"}:
                        raise PlatformError(409,"invalid_state","Публикация уже завершена")
                    if job.status in {"downloading","processing","publishing"}: job.cancel_requested=True
                    else: job.status="cancelled";job.finished_at=utcnow();job.queue_position=None
                elif action=="confirm-duplicate":
                    if job.status not in {"waiting_confirmation", "failed"} or job.error_code == "uncertain_publish" or not job.normalized_url:
                        raise PlatformError(409, "invalid_state", "Подтверждение повтора доступно для источника в черновике или после ошибки")
                    job.allow_duplicate = True
                    job.version += 1
                    if job.render_plan:
                        job.render_plan = {**job.render_plan, "job_version": job.version}
                elif action=="resolve-uncertain":
                    outcome = payload.get("outcome")
                    if job.status != "failed" or job.error_code!="uncertain_publish" or outcome not in {"published","not_published","partial","still_unknown"}:
                        raise PlatformError(409,"invalid_resolution","Запишите результат проверки канала")
                    ids = payload.get("message_ids", [])
                    if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
                        raise PlatformError(422, "invalid_message_ids", "Укажите положительные целые ID сообщений")
                    note = payload.get("note", "")
                    if not isinstance(note, str) or len(note) > 4000:
                        raise PlatformError(422, "invalid_note", "Примечание должно содержать не более 4000 символов")
                    self._audit(session,actor_id,"publication.resolve_uncertain",job.id,outcome=outcome,message_ids=ids,note=note)
                    if outcome=="published":
                        ids=payload.get("message_ids",[])
                        if not ids or any(not isinstance(i,int) or i<=0 for i in ids): raise PlatformError(422,"message_ids_required","Укажите подтверждённые ID сообщений")
                        session.add(Publication(job_id=job.id,channel_id=job.target_channel_id,telegram_chat_id=job.channel.telegram_chat_id,telegram_message_ids=ids,caption="",published_at=utcnow()))
                        job.status="completed";job.error_code=job.error_message=None
                    elif outcome=="not_published":
                        job.error_code="manually_checked_not_published";job.error_message="Ручная проверка: публикация не найдена. Повтор требует отдельной команды."
                    job.version += 1
                    if job.render_plan:
                        job.render_plan = {**job.render_plan, "job_version": job.version}
                else:
                    raise PlatformError(422,"unknown_action","Неизвестное действие")
                await session.flush()
                result=await self._dto(session,job,detail=True)
                self._audit(session,actor_id,f"publication.{action}",job.id,old_status=old_status,new_status=job.status)
            self._save_receipt(session,actor_id,scope,idempotency_key,digest,result)
            return result

    async def list_channels(self,actor_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            return {"items":[self._channel_dto(c) for c in (await session.scalars(select(Channel).order_by(Channel.id))).all()],"next_cursor":None}

    async def get_channel(self,actor_id,channel_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            c=await session.get(Channel,channel_id)
            if not c: raise PlatformError(404,"not_found","Канал не найден")
            return self._channel_dto(c)

    async def create_channel(self,actor_id,payload,idempotency_key):
        async with self._write(actor_id) as session:
            previous,digest=await self._receipt(session,actor_id,"channel:create",idempotency_key,payload)
            if previous is not None:return previous
            import re
            alias=str(payload.get("alias",""))
            chat=str(payload.get("telegram_chat_id",""))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}",alias) or not re.fullmatch(r"(?:-100\d+|@[A-Za-z][A-Za-z0-9_]{4,})",chat):
                raise PlatformError(422,"invalid_channel","Укажите alias и Telegram ID канала (-100… или @имя)")
            if await session.scalar(select(Channel.id).where(Channel.alias==alias)):
                raise PlatformError(409,"duplicate_alias","Этот alias уже используется")
            # New channels stay disabled until Telegram permissions have been checked.
            c=Channel(alias=alias,telegram_chat_id=chat,title=str(payload.get("title",alias))[:255],is_enabled=False,is_default=False,publish_mode="auto",default_tags=[])
            session.add(c);await session.flush()
            result=self._channel_dto(c)
            self._save_receipt(session,actor_id,"channel:create",idempotency_key,digest,result)
            return result

    async def patch_channel(self,actor_id,channel_id,payload):
        async with self._write(actor_id) as session:
            c=await session.get(Channel,channel_id)
            if not c:raise PlatformError(404,"not_found","Канал не найден")
            self._version(c,payload)
            allowed={"expected_version","title","telegram_chat_id","is_enabled","is_paused","is_default","publish_interval_seconds","publish_mode","caption_template","default_tags"}
            if set(payload)-allowed:raise PlatformError(422,"invalid_field","Неизвестное поле канала")
            if "telegram_chat_id" in payload and str(payload["telegram_chat_id"])!=c.telegram_chat_id:
                if await session.scalar(select(func.count()).select_from(Job).where(Job.target_channel_id==c.id)):
                    raise PlatformError(409,"channel_has_history","Создайте новый канал: прежний ID связан с историей")
            if "publish_interval_seconds" in payload and (not isinstance(payload["publish_interval_seconds"],int) or not 0<=payload["publish_interval_seconds"]<=31536000):
                raise PlatformError(422,"invalid_interval","Интервал должен быть от 0 до 31536000 секунд")
            if "publish_mode" in payload and payload["publish_mode"] not in {"auto","photo","document"}:raise PlatformError(422,"invalid_mode","Неверный режим медиа")
            render_changed=any(name in payload and payload[name]!=getattr(c,name) for name in ("caption_template","default_tags","publish_mode"))
            for key,value in payload.items():
                if key!="expected_version":setattr(c,key,value)
            if "publish_interval_seconds" in payload:
                last=await session.scalar(select(Publication.published_at).where(Publication.channel_id==c.id).order_by(Publication.published_at.desc()).limit(1))
                c.next_publish_at=last+timedelta(seconds=c.publish_interval_seconds) if last and c.publish_interval_seconds else None
            if payload.get("is_default"):
                for other in (await session.scalars(select(Channel).where(Channel.id!=c.id,Channel.is_default.is_(True)))).all():other.is_default=False
            if render_changed:
                await self._invalidate_channel_plans(session,c.id)
            await session.flush()
            self._audit(session,actor_id,"channel.update",c.id,version=c.version)
            return self._channel_dto(c)

    async def queue(self,actor_id,*,channel_id=None,limit=50,cursor=None,**kwargs):
        if not 1 <= limit <= 100:
            raise PlatformError(422, "invalid_limit", "Размер страницы должен быть от 1 до 100")
        async with self.sessions() as session:
            await self._actor(session, actor_id)
            revisions={str(q.channel_id):q.revision for q in (await session.scalars(select(QueueRevision))).all()}
            scoped_revisions = revisions if channel_id is None else {str(channel_id): revisions.get(str(channel_id),0)}
            signature=hashlib.sha256(json.dumps([channel_id, scoped_revisions],sort_keys=True).encode()).hexdigest()[:24]
            offset=0
            if cursor:
                try:
                    raw, previous=cursor.split(":")
                    offset=int(raw)
                    if offset < 0: raise ValueError()
                except (ValueError, AttributeError):
                    raise PlatformError(422,"invalid_cursor","Некорректная страница очереди") from None
                if previous != signature:
                    raise PlatformError(409,"queue_conflict","Очередь изменилась. Обновите список с первой страницы")
            query=select(Job).options(selectinload(Job.channel)).where(Job.status.in_(["queued","scheduled","downloading","processing","publishing"]))
            if channel_id: query=query.where(Job.target_channel_id==channel_id)
            total=await session.scalar(select(func.count()).select_from(query.subquery()))
            priority=case((Job.status.in_(["downloading","processing","publishing"]),0),(Job.scheduled_at.is_(None),1),else_=2)
            jobs=list((await session.scalars(query.order_by(Job.target_channel_id,priority,Job.queue_position.asc().nulls_last(),Job.scheduled_at,Job.id).offset(offset).limit(limit+1))).all())
            ordinary=list((await session.execute(select(Job.id,Job.target_channel_id).where(Job.status=="queued",Job.scheduled_at.is_(None))
                .order_by(Job.target_channel_id,Job.queue_position.asc().nulls_last(),Job.id))).all())
            movement={}
            for index,(job_id,owner) in enumerate(ordinary):
                movement[job_id]={"up": index > 0 and ordinary[index-1][1]==owner,"down": index+1<len(ordinary) and ordinary[index+1][1]==owner}
            items=[]
            for job in jobs[:limit]:
                items.append({**await self._dto(session,job),"queue_movement":movement.get(job.id,{"up":False,"down":False})})
            state=await session.get(ApplicationState,1)
            return {"items":items,"total":total,"next_cursor":f"{offset+limit}:{signature}" if len(jobs)>limit else None,
                "revisions":revisions,"publication_blocks":state.publication_blocks if state else {}}

    async def move_queue(self,actor_id,channel_id,payload,idempotency_key):
        async with self._write(actor_id) as session:
            scope=f"queue:{channel_id}:move"
            previous,digest=await self._receipt(session,actor_id,scope,idempotency_key,payload)
            if previous is not None:return previous
            revision=await session.get(QueueRevision,channel_id)
            if payload.get("expected_queue_revision") != (revision.revision if revision else 0):
                raise PlatformError(409,"queue_conflict","Очередь уже изменилась")
            rows=list((await session.scalars(select(Job).where(Job.target_channel_id==channel_id,Job.status=="queued",Job.scheduled_at.is_(None))
                .order_by(Job.queue_position.asc().nulls_last(),Job.id))).all())
            ids=[job.id for job in rows]
            if payload.get("job_id") not in ids or payload.get("direction") not in {"up","down"}:
                raise PlatformError(409,"queue_membership_conflict","Запись больше не находится в обычной очереди канала")
            index=ids.index(payload["job_id"]);other=index+(-1 if payload["direction"]=="up" else 1)
            if not 0 <= other < len(rows):
                raise PlatformError(409,"queue_boundary","Запись уже находится на границе очереди")
            rows[index],rows[other]=rows[other],rows[index]
            for position,job in enumerate(rows,1): job.queue_position=position
            await session.flush()
            await session.refresh(revision)
            result={"channel_id":channel_id,"revision":revision.revision,"job_id":payload["job_id"]}
            self._save_receipt(session,actor_id,scope,idempotency_key,digest,result)
            return result

    async def reorder_queue(self,actor_id,channel_id,payload,idempotency_key):
        payload={**payload,"expected_revision":payload.get("expected_revision",payload.get("expected_queue_revision")),"job_ids":payload.get("job_ids",payload.get("ordered_job_ids",[]))}
        async with self._write(actor_id) as session:
            scope=f"queue:{channel_id}:reorder"
            previous,digest=await self._receipt(session,actor_id,scope,idempotency_key,payload)
            if previous is not None:return previous
            revision=await session.get(QueueRevision,channel_id)
            if payload.get("expected_revision")!=(revision.revision if revision else 0):raise PlatformError(409,"queue_conflict","Очередь уже изменилась")
            rows=list((await session.scalars(select(Job).where(Job.target_channel_id==channel_id,Job.status=="queued",Job.scheduled_at.is_(None)))).all())
            ids=payload.get("job_ids",[])
            if len(ids)!=len(set(ids)) or set(ids)!={j.id for j in rows}:raise PlatformError(409,"queue_membership_conflict","Передайте полный актуальный список обычной очереди канала")
            for job in rows:job.queue_position=ids.index(job.id)+1
            await session.flush()
            await session.refresh(revision)
            result={"channel_id":channel_id,"revision":revision.revision,"job_ids":ids}
            self._save_receipt(session,actor_id,scope,idempotency_key,digest,result)
            return result

    async def settings(self,actor_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            state=await session.get(ApplicationState,1)
            return {"version":state.version if state else 1,**await self.effective_settings(session),**(state.settings if state else {}),"publication_blocks":state.publication_blocks if state else {}}

    async def patch_settings(self,actor_id,payload):
        async with self._write(actor_id) as session:
            state=await session.get(ApplicationState,1)
            if not state:
                state=ApplicationState(id=1,system_id=self.gate.identity()["system_id"] if self.gate else str(uuid4()),version=1,settings={},publication_blocks={},maintenance_state="normal");session.add(state)
            self._version(state,payload)
            allowed={"timezone","auto_add_source_tags","max_tags","max_tag_length","overdue_policy",'pixiv_media_limit_enabled','pixiv_max_images'}
            if set(payload)-allowed-{"expected_version"}:raise PlatformError(422,"invalid_setting","Неизвестная настройка")
            if "timezone" in payload:
                try:ZoneInfo(payload["timezone"])
                except Exception:raise PlatformError(422,"invalid_timezone","Неизвестный часовой пояс IANA")
            for key,maximum in (('max_tags',100),('max_tag_length',128),('pixiv_max_images',1000)):
                if key in payload and (type(payload[key]) is not int or not 1<=payload[key]<=maximum):raise PlatformError(422,'invalid_setting','Недопустимый лимит')
            for key in ('auto_add_source_tags','pixiv_media_limit_enabled'):
                if key in payload and type(payload[key]) is not bool:raise PlatformError(422,'invalid_setting','Ожидается переключатель да/нет')
            if any(key in payload and payload[key]!=state.settings.get(key) for key in ('auto_add_source_tags','max_tags','max_tag_length','pixiv_media_limit_enabled','pixiv_max_images')):
                pending=(await session.scalars(select(Job).where(Job.status.in_(['waiting_confirmation','queued','scheduled'])))).all()
                for job in pending:job.render_plan=None;job.version+=1
            state.settings={**state.settings,**{k:v for k,v in payload.items() if k in allowed}};state.version+=1
            return {"version":state.version,"timezone":"Asia/Vladivostok",**state.settings,"publication_blocks":state.publication_blocks}

    async def publication_pause(self,actor_id,paused):
        async with self._write(actor_id) as session:
            state=await session.get(ApplicationState,1)
            if not state:
                state=ApplicationState(id=1,system_id=self.gate.identity()["system_id"] if self.gate else str(uuid4()),version=1,settings={},publication_blocks={},maintenance_state="normal");session.add(state)
            blocks=dict(state.publication_blocks)
            if paused:blocks["user_pause"]="user"
            else:blocks.pop("user_pause",None)
            state.publication_blocks=blocks;state.version+=1
            if self.gate:
                if paused:self.gate.block("user_pause","user")
                else:self.gate.unblock("user_pause","user")
            return {"publication_blocks":blocks}

    @staticmethod
    def operation_dto(op):
        actions=[]
        if op.status in {"queued","running","waiting_worker","waiting_external"}:actions=["cancel"]
        elif op.status in {"failed","waiting_user"}:actions=["retry"]
        if op.error and op.error.get('code')=='uncertain_publish':actions=[]
        return {"id":op.id,"kind":op.kind,"status":op.status,"stage_key":op.stage_key,"stages":op.stages,"version":op.version,"created_at":iso(op.created_at),"started_at":iso(op.started_at),"updated_at":iso(op.updated_at),"heartbeat_at":iso(op.heartbeat_at),"message":op.message,"progress":op.progress,"result":op.result,"error":op.error,"allowed_actions":actions,"last_event_seq":op.last_event_seq}

    async def operations(self,actor_id,**kwargs):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            ops=(await session.scalars(select(Operation).order_by(Operation.created_at.desc()).limit(100))).all()
            return {"items":[self.operation_dto(o) for o in ops],"next_cursor":None}

    async def operation(self,actor_id,operation_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            op=await session.get(Operation,operation_id)
            if not op:raise PlatformError(404,"not_found","Операция не найдена")
            return self.operation_dto(op)

    async def operation_events(self,actor_id,operation_id,after_seq=0):
        await self.operation(actor_id,operation_id)
        async with self.sessions() as session:
            events=(await session.scalars(select(OperationEvent).where(OperationEvent.operation_id==operation_id,OperationEvent.sequence>after_seq).order_by(OperationEvent.sequence).limit(500))).all()
            return {"items":[{"sequence":e.sequence,"stage_key":e.stage_key,"status":e.status,"message":e.message,"progress":e.progress,"created_at":iso(e.created_at)} for e in events]}

    async def operation_action(self,actor_id,operation_id,action,payload=None):
        async with self._write(actor_id) as session:
            op=await session.get(Operation,operation_id)
            if not op:raise PlatformError(404,"not_found","Операция не найдена")
            if action not in self.operation_dto(op)["allowed_actions"]:raise PlatformError(409,"invalid_state","Это действие сейчас недоступно")
            if action=="cancel":op.cancel_requested=True;op.status="cancelled" if op.status!="running" else "cancelling"
            else:
                op.status="queued";op.error=None;op.cancel_requested=False
                if op.kind=="import" and op.result:
                    op.result={"items":{k:v for k,v in op.result.get("items",{}).items() if v.get("status")=="succeeded"}}
            if op.kind in {'news','title_translation'}:
                task=await session.scalar(select(NewsTask).where(NewsTask.operation_id==op.id))
                if task:
                    if task.status=='completed':
                        raise PlatformError(409,'task_completed','Результат уже получен. Примените предложение или запустите новую подготовку.')
                    task.lease_generation+=1;task.lease_owner=None;task.lease_token=None;task.lease_expires_at=None
                    if action=='cancel':
                        task.status='cancelled';task.stage='cancelled';task.completed_at=utcnow();op.status='cancelled'
                    else:
                        task.status='queued';task.stage='queued';task.completed_at=None;task.attempts=0;task.error_message=None
                        op.status='waiting_worker';op.message='Ждём домашний компьютер'
            op.version+=1
            return self.operation_dto(op)

    async def preview(self,actor_id,job_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            job=await self._job(session,job_id)
            return self.render_plan(job,settings=await self.effective_settings(session))

    def render_plan(self,job,*,settings=None):
        if job.render_plan and job.render_plan.get('job_version')==job.version and (job.render_plan.get('channel_render_hash')==channel_render_hash(job.channel) if 'channel_render_hash' in job.render_plan else job.render_plan.get('channel_version')==job.channel.version):
            import copy
            return {**copy.deepcopy(job.render_plan),'assets_prepared':True}
        post=deserialize_post(job.post_data)
        settings=settings or {}
        tags=publication_tags(job.user_tags,job.source_tags,job.channel.default_tags,include_source=settings.get('auto_add_source_tags',True),max_tags=settings.get('max_tags',20),max_length=settings.get('max_tag_length',64))
        if job.caption_override is not None:
            caption=NewsRenderService().build_custom(job.caption_override,post,tags) if job.content_kind=="news" else CaptionService().build_custom(job.caption_override)
        else:caption=NewsRenderService().build(post,tags) if job.content_kind=="news" else CaptionService().build(post,tags,job.channel.caption_template or DEFAULT_TEMPLATE)
        from app.utils.safe_html import safe_telegram_html
        caption=safe_telegram_html(caption)
        count=len(post.media_items);length=telegram_html_length(caption)
        parts=[]
        for start in range(0,count,10):parts.append({"kind":"media","source_indices":list(range(start+1,min(start+10,count)+1)),"caption_html":caption if start==0 and length<=1024 else ""})
        if not count or length>1024:parts.append({"kind":"text","html":caption})
        return {"job_id":job.id,"job_version":job.version,"channel_version":job.channel.version,"channel_render_hash":channel_render_hash(job.channel),"html":caption,"text_length":length,"caption_limit":1024,"text_limit":4096,"media_count":count,"parts":parts,"warnings":[],"assets_prepared":bool(job.render_plan)}

    async def history(self,actor_id,job_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id);await self._job(session,job_id)
            from app.db.models import PublicationAttempt,PublicationPart
            pubs=(await session.scalars(select(Publication).where(Publication.job_id==job_id))).all()
            events=(await session.scalars(select(JobEvent).where(JobEvent.job_id==job_id).order_by(JobEvent.id))).all()
            attempts=(await session.scalars(select(PublicationAttempt).where(PublicationAttempt.job_id==job_id).order_by(PublicationAttempt.created_at,PublicationAttempt.id))).all()
            parts=(await session.scalars(select(PublicationPart).where(PublicationPart.attempt_id.in_([attempt.id for attempt in attempts])).order_by(PublicationPart.sequence,PublicationPart.id))).all() if attempts else []
            by_attempt={}
            for part in parts:by_attempt.setdefault(part.attempt_id,[]).append(part)
            attempt_dtos=[]
            for attempt in attempts:
                entries=by_attempt.get(attempt.id,[])
                error=attempt.error if attempt.error in {'uncertain_publish','runtime_interrupted','publish_error','channel_permission','publication_blocked'} else 'publication_error' if attempt.error else None
                attempt_dtos.append({'id':attempt.id,'job_version':attempt.job_version,'node_id':attempt.node_id,'generation':attempt.generation,'stage':attempt.stage,'error_code':error,
                    'created_at':iso(attempt.created_at),'completed_at':iso(attempt.completed_at),'known_message_ids':[value for part in entries for value in (part.message_ids or []) if type(value) is int and value>0],
                    'has_unknown_parts':any(part.status=='unknown' for part in entries),
                    'parts':[{'id':part.id,'sequence':part.sequence,'status':part.status,'message_ids':[value for value in (part.message_ids or []) if type(value) is int and value>0],
                              'before_send_at':iso(part.before_send_at),'request_digest':part.request_digest} for part in entries]})
            return {"publications":[{"id":p.id,"telegram_chat_id":p.telegram_chat_id,"message_ids":p.telegram_message_ids,"published_at":iso(p.published_at)} for p in pubs],"events":[{"id":e.id,"type":e.event_type,"message":e.message,"old_status":e.old_status,"new_status":e.new_status,"created_at":iso(e.created_at)} for e in events],"attempts":attempt_dtos}

    async def audit(self,actor_id,**kwargs):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            events=(await session.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100))).all()
            return {"items":[{"id":e.id,"actor_id":e.actor_id,"action":e.action,"resource_id":e.resource_id,"details":e.details,"created_at":iso(e.created_at)} for e in events]}

    async def asset(self,actor_id,asset_id,thumbnail=False):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            asset=await session.get(MediaAsset,asset_id)
            if not asset:raise PlatformError(404,"not_found","Медиафайл не найден")
            key=asset.thumbnail_key if thumbnail else asset.storage_key
            if not key:raise PlatformError(404,"asset_unavailable","Локальный файл недоступен")
            from app.storage.assets import safe_path
            path=safe_path(self.data_root,key)
            if not path.is_file():raise PlatformError(404,"asset_missing","Файл отсутствует; история сохранена")
            return {"path":str(path),"mime_type":asset.mime_type,"filename":asset.filename}

    async def ai(self,actor_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            workers=(await session.scalars(select(AIWorker))).all()
            tasks=(await session.scalars(select(NewsTask).order_by(NewsTask.id.desc()).limit(100))).all()
            return {"workers":[{"id":w.id,"name":w.name,"provider":w.provider,"models":w.models,"selected_model":w.selected_model,"last_seen_at":iso(w.last_seen_at),"status":"revoked" if w.revoked_at else "online" if w.last_seen_at and utcnow()-w.last_seen_at.replace(tzinfo=UTC)<timedelta(seconds=90) else "offline"} for w in workers],"tasks":[{"id":t.id,"version":t.lease_generation,"kind":t.kind,"status":t.status,"stage":t.stage,"message":t.stage_message,"model":t.model_name,"worker_id":t.lease_owner,"operation_id":t.operation_id,"created_at":iso(t.created_at)} for t in tasks]}

    async def system_status(self,actor_id):
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            import sqlite3
            return {"application":{"status":"ready","checked_at":iso(utcnow())},"sqlite":{"version":sqlite3.sqlite_version,"foreign_keys":bool(await session.scalar(text("PRAGMA foreign_keys")))},"telegram":{"status":"unchecked"},"sources":{"status":"unchecked"},"ai":{"status":"unchecked"},"publication":self.gate.status() if self.gate else {"status":"unmanaged"}}
