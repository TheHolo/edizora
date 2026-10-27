import asyncio
import time
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload
from app.db.models import ApplicationState, Operation, OperationEvent, Job, MediaAsset, JobAsset, NewsTask, utcnow
from app.application.platform import channel_render_hash
from app.services.job_service import serialize_post
from app.services.preview_service import deserialize_post
from app.domain.album import read_album
from app.storage.assets import store_bytes


class OperationRunner:
    """Single runtime-owned executor. Every accepted operation is committed before work."""
    def __init__(self, platform, application=None):
        self.platform=platform
        self.application=application or platform.runtime
        self.stopping=False
        self.task=None
        self.active_operation=None

    async def start(self):
        async with self.platform.sessions() as session,session.begin():
            interrupted=(await session.scalars(select(Operation).where(Operation.status.in_(["running","cancelling"])))).all()
            for op in interrupted:
                uncertain = op.kind == 'telegram_preview' and op.stage_key == 'sending'
                op.status="waiting_user" if uncertain else "cancelled" if op.cancel_requested else "queued"
                if uncertain:op.error={'code':'uncertain_publish','message':'Ответ Telegram неизвестен. Проверьте личный чат; автоматического повтора не будет.'}
                op.message="Восстановлено после перезапуска"
                op.version+=1
        self.stopping=False
        self.task=asyncio.create_task(self.run(),name="durable-operations")

    async def stop(self):
        self.stopping=True
        if self.task:
            await self.task

    async def run(self):
        next_preparation_check=0
        while not self.stopping:
            if self.platform.gate:
                try:self.platform.gate.assert_mutation_allowed()
                except RuntimeError:
                    await asyncio.sleep(.25);continue
            if time.monotonic()>=next_preparation_check:
                await self._queue_preparations()
                next_preparation_check=time.monotonic()+1
            async with self.platform.sessions() as session:
                op=await session.scalar(select(Operation).where(Operation.status=="queued",Operation.kind.in_(["import","prepare","title_translation","reimport","telegram_preview"])).order_by(Operation.created_at).limit(1))
                operation_id=op.id if op else None
            if not operation_id:
                await asyncio.sleep(.25);continue
            self.active_operation=operation_id
            try:
                await self._event(operation_id,"running","working","Выполняется")
                if op.kind=="import":await self._import(operation_id)
                elif op.kind=="prepare":await self._prepare(operation_id)
                elif op.kind=='title_translation':await self._translation(operation_id)
                elif op.kind=='reimport':await self._reimport(operation_id)
                else:await self._telegram_preview(operation_id)
            except Exception as error:
                # Never serialize provider traceback, headers or credential-bearing URL.
                uncertain = getattr(error,'code','') == 'uncertain_publish'
                await self._event(operation_id,"waiting_user" if uncertain else "failed","failed","Результат Telegram неизвестен; проверьте личный чат" if uncertain else "Не удалось выполнить операцию. Проверьте источник и повторите",error={"code":getattr(error,"code",type(error).__name__),"message":"Операция не завершена; сохранённые данные доступны"})
            finally:self.active_operation=None

    async def _queue_preparations(self):
        """Prepare each saved revision once, including drafts created before startup.

        Failed downloads stay visible and require an explicit retry; editing a
        draft creates a new revision. Scheduling never grants publication rights.
        """
        async with self.platform.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            state=await session.get(ApplicationState,1)
            if state and state.maintenance_state!="normal":
                await session.rollback();return
            jobs=(await session.scalars(select(Job).options(selectinload(Job.channel)).where(
                Job.status=="waiting_confirmation"))).all()
            for job in jobs:
                if job.content_kind=="artwork" and not job.post_data.get("media_items"):continue
                fingerprint=channel_render_hash(job.channel)
                plan=job.render_plan or {}
                if plan.get("job_version")==job.version and (
                    plan.get("channel_render_hash")==fingerprint if "channel_render_hash" in plan
                    else plan.get("channel_version")==job.channel.version):continue
                previous=(await session.scalars(select(Operation).where(
                    Operation.job_id==job.id,Operation.kind=="prepare",
                    Operation.payload["job_version"].as_integer()==job.version))).all()
                if any(op.payload.get("channel_render_hash",fingerprint)==fingerprint for op in previous):continue
                self.platform._new_operation(session,job.created_by_user_id,"prepare",
                    {"job_version":job.version,"channel_render_hash":fingerprint,"automatic":True},
                    "Загружаем изображения и обновляем предпросмотр",job_id=job.id)
            await session.commit()

    async def _event(self,operation_id,status,stage,message,*,progress=None,result=None,error=None):
        async with self.platform.sessions() as session,session.begin():
            op=await session.get(Operation,operation_id)
            if op.cancel_requested and status not in {"failed","cancelled"}:
                status="cancelled";message="Операция отменена"
            op.status=status;op.stage_key=stage;op.message=message;op.progress=progress;op.heartbeat_at=utcnow();op.version+=1;op.last_event_seq+=1
            if not op.started_at:op.started_at=utcnow()
            if result is not None:op.result=result
            if error is not None:op.error=error
            stages=[dict(s) for s in op.stages]
            for step in stages:
                if step.get("status")=="running":step["status"]="succeeded"
            stages.append({"key":stage,"title":message,"status":"succeeded" if status=="succeeded" else "failed" if status=="failed" else "running"})
            op.stages=stages
            session.add(OperationEvent(operation_id=op.id,sequence=op.last_event_seq,stage_key=stage,status=status,message=message,progress=progress))

    async def _cancelled(self,operation_id):
        async with self.platform.sessions() as session:
            op=await session.get(Operation,operation_id)
            return op.cancel_requested

    async def _import(self,operation_id):
        if not self.application:
            await self._event(operation_id,"waiting_user","runtime","Для импорта запустите активный runtime с настроенными источниками")
            return
        async with self.platform.sessions() as session:
            op=await session.get(Operation,operation_id);payload=dict(op.payload);done=dict((op.result or {}).get("items",{}))
        urls=payload["urls"]
        for index,url in enumerate(urls):
            if str(index) in done:continue
            if await self._cancelled(operation_id):
                await self._event(operation_id,"cancelled","cancelled","Импорт отменён",result={"items":done});return
            await self._event(operation_id,"running","source",f"Получаем источник {index+1} из {len(urls)}",progress={"unit":"items","completed":len(done),"total":len(urls),"percent":len(done)/len(urls)*100,"estimated_seconds_remaining":None})
            try:
                post=await self.application.ingest.fetch(url)
            except Exception as error:
                done[str(index)]={"status":"failed","error":getattr(error,"code","source_unavailable")}
                async with self.platform.sessions() as session,session.begin():
                    current=await session.get(Operation,operation_id);current.result={"items":done}
                continue
            async with self.platform.sessions() as session:
                await session.execute(text("BEGIN IMMEDIATE"))
                current=await session.get(Operation,operation_id)
                committed=dict((current.result or {}).get("items",{}))
                if str(index) in committed:
                    done=committed;await session.rollback();continue
                job=Job(created_by_user_id=current.actor_id,target_channel_id=payload["channel_id"],provider=post.provider,source_id=post.source_id,source_url=post.source_url,normalized_url=post.normalized_url,content_kind=post.content_kind,status="waiting_confirmation",user_tags=payload["tags"],source_tags=post.source_tags,post_data=serialize_post(post),max_attempts=3)
                session.add(job);await session.flush()
                await self._catalog(session,job)
                committed[str(index)]={"status":"succeeded","job_id":job.id};current.result={"items":committed};current.job_id=job.id
                await session.commit();done=committed
        failures=sum(v["status"]=="failed" for v in done.values())
        ids=[v["job_id"] for v in done.values() if "job_id" in v]
        await self._event(operation_id,"waiting_user" if failures else "succeeded","complete",f"Создано черновиков: {len(ids)}"+(f"; недоступных источников: {failures}" if failures else ""),result={"items":done,"publication_ids":ids,"publication_id":ids[0] if len(ids)==1 else None})

    async def _catalog(self,session,job):
        album=read_album(job.post_data)
        for source_index,item in album.catalog.items():
            asset=MediaAsset(id=str(uuid4()),source_kind=job.provider,source_reference=item.get("url"),telegram_file_id=item.get("telegram_file_id"),state="telegram" if item.get("telegram_file_id") else "remote",filename=item.get("filename"),mime_type=item.get("mime_type"),size=item.get("size"),width=item.get("width"),height=item.get("height"))
            session.add(asset)
            await session.flush()
            session.add(JobAsset(job_id=job.id,asset_id=asset.id,source_index=source_index,selected=source_index in album.selection,sort_order=album.selection.index(source_index) if source_index in album.selection else source_index,purpose="original"))

    async def _prepare(self,operation_id):
        async with self.platform.sessions() as session,session.begin():
            op=await session.get(Operation,operation_id);job=await self.platform._job(session,op.job_id)
            if job.status not in {"waiting_confirmation", "queued", "scheduled"} or job.version!=op.payload["job_version"]:
                raise ValueError("stale_job")
            links=list((await session.execute(select(JobAsset,MediaAsset).join(MediaAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==job.id,JobAsset.selected.is_(True)).order_by(JobAsset.sort_order))).all())
            if not links and job.post_data.get("media_items"):
                await self._catalog(session,job);await session.flush()
                links=list((await session.execute(select(JobAsset,MediaAsset).join(MediaAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==job.id,JobAsset.selected.is_(True)).order_by(JobAsset.sort_order))).all())
            original_version=job.version
            original_status=job.status
            original_channel_hash=channel_render_hash(job.channel)
            settings=await self.platform.effective_settings(session)
            if job.content_kind=='artwork' and settings['pixiv_media_limit_enabled'] and len(links)>settings['pixiv_max_images']:
                raise ValueError('selection_limit')
            post=deserialize_post(job.post_data)
        for index,(link,asset) in enumerate(links):
            if await self._cancelled(operation_id):
                await self._event(operation_id,"cancelled","cancelled","Подготовка отменена");return
            if asset.telegram_file_id or asset.state=="available":continue
            if not self.application:
                await self._event(operation_id,"waiting_user","runtime","Для загрузки медиа нужен активный runtime");return
            await self._event(operation_id,"running","media",f"Готовим медиа {index+1} из {len(links)}",progress={"unit":"files","completed":index,"total":len(links),"percent":index/len(links)*100,"estimated_seconds_remaining":None})
            item=post.media_items[index]
            downloaded=await self.application.workers.downloader.download(f"prepare-{operation_id}",item)
            stored=await asyncio.to_thread(store_bytes,self.platform.data_root,downloaded.path.read_bytes())
            async with self.platform.sessions() as session,session.begin():
                current=await session.get(MediaAsset,asset.id)
                for key,value in stored.items():
                    if key!="id":setattr(current,key,value)
        # Prepare and pin the actual Telegram variant before queue admission.
        from app.domain.models import DownloadedMedia
        from app.domain.enums import MediaType
        from app.services.media_service import MediaService
        from app.storage.assets import safe_path
        from app.application.prepared_media import media_parts
        manifest=[]
        async with self.platform.sessions() as session:
            links=list((await session.execute(select(JobAsset,MediaAsset).join(MediaAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==job.id,JobAsset.selected.is_(True)).order_by(JobAsset.sort_order))).all())
        for index,((link,asset),source) in enumerate(zip(links,post.media_items,strict=True)):
            entry={'source_asset_id':asset.id,'asset_id':asset.id,'storage_key':asset.storage_key,'telegram_file_id':asset.telegram_file_id,
                   'media_type':source.media_type.value,'as_document':job.channel.publish_mode=='document' or source.media_type==MediaType.DOCUMENT,'order':link.sort_order}
            if not asset.telegram_file_id:
                path=safe_path(self.platform.data_root,asset.storage_key)
                if source.media_type in {MediaType.IMAGE,MediaType.ANIMATION,MediaType.DOCUMENT}:
                    prepared=await MediaService().prepare(DownloadedMedia(source=source,path=path,mime_type=asset.mime_type,size=asset.size),job.channel.publish_mode)
                    entry['as_document']=prepared.as_document
                    if prepared.path!=path:
                        stored=await asyncio.to_thread(store_bytes,self.platform.data_root,prepared.path.read_bytes())
                        async with self.platform.sessions() as session,session.begin():
                            variant=MediaAsset(**stored,source_kind='prepared',source_reference=asset.id,filename=asset.filename)
                            session.add(variant);await session.flush()
                            entry.update(asset_id=variant.id,storage_key=variant.storage_key)
                        prepared.path.unlink(missing_ok=True)
            manifest.append(entry)
        async with self.platform.sessions() as session,session.begin():
            current=await self.platform._job(session,job.id)
            if current.version!=original_version or current.status!=original_status or channel_render_hash(current.channel)!=original_channel_hash:
                raise ValueError("stale_job")
            plan=self.platform.render_plan(current,settings=await self.platform.effective_settings(session))
            plan['assets_prepared']=True
            plan['media']=manifest
            plan['parts']=media_parts(manifest,links,plan['html'],text_separate=current.content_kind=='news' and plan['text_length']>1024)
            current.render_plan=plan
            await session.flush()
        await self._event(operation_id,"succeeded","complete","Публикация подготовлена",result={"publication_id":job.id,"job_version":current.version})

    async def _translation(self,operation_id):
        async with self.platform.sessions() as session,session.begin():
            op=await session.get(Operation,operation_id);job=await self.platform._job(session,op.job_id)
            if job.version!=op.payload["job_version"]:raise ValueError("stale_job")
            existing=await session.scalar(select(NewsTask).where(NewsTask.operation_id==operation_id))
            if not existing:
                worker_id,model_name=await self.platform._selected_ai_target(session,'title_translation')
                metadata={'worker_id':worker_id} if worker_id else {'awaiting_selection':True}
                task=NewsTask(created_by_user_id=op.actor_id,target_channel_id=job.target_channel_id,origin_chat_id=None,source_kind="text",kind="title_translation",operation_id=operation_id,source_job_version=job.version,input_payload={"kind":"title_translation","title":job.post_data.get("title",""),"job_id":job.id,'metadata':metadata},model_name=model_name,status="queued",stage="queued",user_tags=[],max_attempts=3)
                session.add(task)
        await self._event(operation_id,"waiting_worker","worker","Ждём домашний компьютер")

    async def _reimport(self,operation_id):
        if not self.application:
            await self._event(operation_id,'waiting_user','runtime','Для проверки источника нужен настроенный runtime');return
        async with self.platform.sessions() as session:
            op=await session.get(Operation,operation_id);job=await self.platform._job(session,op.job_id)
            source_url=job.source_url
            old_count=len(job.post_data.get('media_items',[]))
        post=await self.application.ingest.fetch(source_url)
        async with self.platform.sessions() as session,session.begin():
            op=await session.get(Operation,operation_id)
            op.payload={**op.payload,'candidate':serialize_post(post)}
        await self._event(operation_id,'succeeded','candidate','Каталог получен. Примените его отдельно после проверки различий.',result={'publication_id':job.id,'candidate_ready':True,'previous_selected_count':old_count,'source_media_count':len(post.media_items),'requires_apply':True})

    async def _telegram_preview(self,operation_id):
        if not self.application:
            await self._event(operation_id,'waiting_user','runtime','Для проверки в Telegram нужен настроенный runtime');return
        from app.domain.models import PreparedMedia
        from app.domain.enums import MediaType
        from app.storage.assets import safe_path
        async with self.platform.sessions() as session:
            op=await session.get(Operation,operation_id);job=await self.platform._job(session,op.job_id)
            dependency=await session.get(Operation,op.payload['depends_on']) if op.payload.get('depends_on') else None
            if dependency and dependency.status!='succeeded':
                dependency_pending=True
            else:dependency_pending=False
        if dependency_pending:
            await self._event(operation_id,'waiting_user','preparation','Проверка в Telegram ждёт успешной подготовки. Откройте связанную операцию.',result={'publication_id':job.id,'prepare_operation_id':dependency.id});return
        async with self.platform.sessions() as session:
            op=await session.get(Operation,operation_id);job=await self.platform._job(session,op.job_id)
            expected_version=dependency.result.get('job_version') if dependency and dependency.result else op.payload['job_version']
            if job.version!=expected_version or not job.render_plan or job.render_plan.get('job_version')!=job.version:
                raise ValueError('stale_job')
            links=list((await session.execute(select(JobAsset,MediaAsset).join(MediaAsset,JobAsset.asset_id==MediaAsset.id).where(JobAsset.job_id==job.id,JobAsset.selected.is_(True)).order_by(JobAsset.sort_order))).all())
            post=deserialize_post(job.post_data);plan=job.render_plan;chat_id=op.payload['chat_id']
        if len(links)!=len(post.media_items):raise ValueError('asset_manifest_incomplete')
        from app.application.prepared_media import load_prepared_media
        prepared=load_prepared_media(plan,links,post,self.platform.data_root,job.channel.publish_mode)
        # This checkpoint precedes the network side effect. An interrupted send is never restarted automatically.
        await self._event(operation_id,'running','sending','Отправляем проверку в ваш личный Telegram-чат')
        result=await self.application.publisher.preview(chat_id,prepared,plan['html'],post)
        await self._event(operation_id,'succeeded','complete','Проверка отправлена в личный чат',result={'publication_id':job.id,'message_ids':result.message_ids})
