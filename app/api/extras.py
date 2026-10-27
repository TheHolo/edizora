"""Additional adapters backed by the same application transactions and worker queue."""
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text

from app.application.platform import PlatformService, PlatformError, iso
from app.db.models import AIWorker, ApplicationState, AuditEvent, Channel, NewsTask, Operation, User, utcnow
from app.services.news_task_service import NewsTaskService, NewsTaskLeaseError


class WebPlatformService(PlatformService):
    def __init__(self, sessions, *, data_root, gate=None):
        super().__init__(sessions, data_root=data_root, gate=gate)
        self.news = NewsTaskService(sessions, gate=gate)
        self.news.platform=self

    async def list_channels(self,actor_id):
        result=await super().list_channels(actor_id)
        await self._hydrate_channels(result['items'])
        return result

    async def get_channel(self,actor_id,channel_id):
        item=await super().get_channel(actor_id,channel_id)
        await self._hydrate_channels([item])
        return item

    async def _hydrate_channels(self,items):
        if not items:return
        from app.db.models import ChannelMemberSnapshot
        from sqlalchemy import func
        identifiers=[item['id'] for item in items]
        async with self.sessions() as session:
            latest=(select(AuditEvent.resource_id,func.max(AuditEvent.id).label('event_id')).where(
                AuditEvent.action=='channel.check',AuditEvent.resource_id.in_([str(i) for i in identifiers])).group_by(AuditEvent.resource_id).subquery())
            checks=(await session.scalars(select(AuditEvent).join(latest,AuditEvent.id==latest.c.event_id))).all()
            by_channel={int(event.resource_id):event.details for event in checks}
            ranked=(select(ChannelMemberSnapshot.channel_id,ChannelMemberSnapshot.member_count,ChannelMemberSnapshot.captured_at,ChannelMemberSnapshot.source,
                func.row_number().over(partition_by=ChannelMemberSnapshot.channel_id,order_by=(ChannelMemberSnapshot.captured_at.desc(),ChannelMemberSnapshot.id.desc())).label('position')).where(ChannelMemberSnapshot.channel_id.in_(identifiers)).subquery())
            snapshots=(await session.execute(select(ranked).where(ranked.c.position==1))).all()
            by_snapshot={row.channel_id:row for row in snapshots}
        for item in items:
            check=by_channel.get(item['id'])
            if check:
                item['permissions']={'status':'allowed' if check.get('can_post') else 'denied','can_post':bool(check.get('can_post')),'checked_at':check.get('checked_at')}
                item['permissions_checked_at']=check.get('checked_at')
            snapshot=by_snapshot.get(item['id'])
            if snapshot:
                item.update(member_count=snapshot.member_count,member_count_updated_at=iso(snapshot.captured_at),member_count_source=snapshot.source)

    async def prepare(self, actor_id, job_id, kind, payload, idempotency_key):
        if kind in {'prepare', 'translate-title'}:
            return await self.publication_action(actor_id, job_id, 'translate' if kind == 'translate-title' else 'prepare', payload, idempotency_key)
        if kind not in {'reimport', 'preview-in-telegram'}:
            raise PlatformError(422, 'invalid_operation', 'Неизвестное действие подготовки')
        async with self._write(actor_id) as session:
            previous, request_hash = await self._receipt(session, actor_id, kind + ':' + str(job_id), idempotency_key, payload)
            if previous is not None:
                return previous
            job = await self._job(session, job_id)
            self._version(job, payload)
            if kind == 'reimport' and job.status != 'waiting_confirmation':
                raise PlatformError(409, 'invalid_state', 'Повторный импорт доступен в черновике')
            if kind == 'preview-in-telegram':
                if self.gate:
                    self.gate.assert_publication_allowed()
                if not job.render_plan or job.render_plan.get('job_version') != job.version:
                    raise PlatformError(409, 'not_ready', 'Сначала подготовьте актуальный предпросмотр')
            actor = await self._actor(session, actor_id)
            op = self._new_operation(session, actor_id, 'reimport' if kind == 'reimport' else 'telegram_preview',
                {'job_version': job.version, 'chat_id': actor.telegram_user_id}, 'Операция принята', job_id=job.id)
            result = {'operation_id': op.id, 'resource_id': job.id, 'status_url': f'/api/v1/operations/{op.id}'}
            self._save_receipt(session, actor_id, kind + ':' + str(job_id), idempotency_key, request_hash, result)
            return result

    async def apply_reimport(self, actor_id, job_id, payload):
        from app.db.models import JobAsset
        from app.domain.album import read_album
        from app.services.job_service import serialize_post
        from app.operations.runner import OperationRunner
        async with self._write(actor_id) as session:
            job = await self._job(session, job_id)
            self._version(job, payload)
            op = await session.get(Operation, payload.get('operation_id'))
            if not op or op.job_id != job.id or op.kind != 'reimport' or not op.payload.get('candidate') or job.status != 'waiting_confirmation':
                raise PlatformError(409, 'candidate_unavailable', 'Нет готового кандидата для этого черновика')
            if op.payload['job_version'] != job.version:
                raise PlatformError(409, 'version_conflict', 'Черновик изменён после получения кандидата', current_version=job.version)
            candidate = dict(op.payload['candidate'])
            # Keep manually edited fields and unknown historical JSON. Apply only the new source catalogue.
            existing = dict(job.post_data)
            old_selection = read_album(existing).selection
            existing['media_items'] = candidate['media_items']
            existing['metadata'] = {**existing.get('metadata', {}), **{k: v for k, v in candidate.get('metadata', {}).items() if k.startswith('album')}}
            job.post_data = existing
            links = (await session.scalars(select(JobAsset).where(JobAsset.job_id == job.id))).all()
            for link in links:
                await session.delete(link)
            await session.flush()
            await OperationRunner(self)._catalog(session, job)
            links = (await session.scalars(select(JobAsset).where(JobAsset.job_id == job.id))).all()
            selected_media=[]
            for link in links:
                link.selected = link.source_index in old_selection
                if link.selected:
                    link.sort_order=old_selection.index(link.source_index)
                    asset=await session.get(__import__('app.db.models',fromlist=['MediaAsset']).MediaAsset,link.asset_id)
                    selected_media.append((link.sort_order,self._media_payload(asset,link.sort_order)))
            existing['media_items']=[item for _,item in sorted(selected_media)]
            job.post_data=existing
            job.render_plan = None
            op.result = {**(op.result or {}), 'applied': True}
            self._audit(session, actor_id, 'publication.apply_reimport', job.id, operation_id=op.id)
            await session.flush()
            return await self._dto(session, job, detail=True)

    async def apply_translation_proposal(self,actor_id,operation_id,payload,idempotency_key):
        from app.db.models import OperationEvent
        from app.services.title_client import TitleTranslation
        async with self._write(actor_id) as session:
            previous,request_hash=await self._receipt(session,actor_id,'translation-apply:'+operation_id,idempotency_key,payload)
            if previous is not None:return previous
            op=await session.get(Operation,operation_id)
            if not op or op.kind!='title_translation' or not op.result or not op.result.get('proposal'):
                raise PlatformError(409,'proposal_unavailable','Предложение перевода недоступно')
            job=await self._job(session,op.job_id)
            self._version(job,payload)
            if job.status!='waiting_confirmation':raise PlatformError(409,'invalid_state','Верните публикацию в черновик')
            proposal=TitleTranslation.model_validate(op.result['proposal'])
            job.post_data={**job.post_data,'metadata':{**job.post_data.get('metadata',{}),
                'title_translation':proposal.translated_text,'title_translation_source':job.post_data.get('title','')}}
            job.render_plan=None
            op.result={**op.result,'applied':True};op.status='succeeded';op.version+=1;op.last_event_seq+=1
            op.message='Предложенный перевод применён владельцем'
            session.add(OperationEvent(operation_id=op.id,sequence=op.last_event_seq,status=op.status,stage_key='applied',message=op.message))
            self._audit(session,actor_id,'publication.apply_translation',job.id,operation_id=op.id)
            await session.flush()
            result={'operation':self.operation_dto(op),'publication':await self._dto(session,job,detail=True)}
            self._save_receipt(session,actor_id,'translation-apply:'+operation_id,idempotency_key,request_hash,result)
            return result

    async def config_import_plan(self, actor_id, payload):
        await self.validate_actor(actor_id)
        if not self.gate:
            raise PlatformError(503, 'managed_host_required', 'Импорт доступен через host')
        if 'channels' in payload:
            incoming=payload['channels']
        else:
            # The browser cannot select a local pathname or request infrastructure secrets.
            from app.config import Settings
            try:
                configured=Settings(bot_token='')
                incoming=[]
                names={'title':'title','publish_mode':'publish_mode','publish_interval_seconds':'publish_interval_seconds',
                       'caption_template':'caption_template','tags':'default_tags','enabled':'is_enabled'}
                for alias,values in configured.channels_json.items():
                    incoming.append({'alias':alias,'telegram_chat_id':str(values['chat_id']),
                        **{target:values[source] for source,target in names.items() if source in values}})
            except Exception:
                raise PlatformError(422,'configured_channels_invalid','Не удалось прочитать настроенный список каналов. Проверьте локальную конфигурацию установки.') from None
        if not isinstance(incoming, list) or len(incoming) > 100:
            raise PlatformError(422, 'invalid_config', 'Недопустимый список каналов')
        current = (await self.list_channels(actor_id))['items']
        changes = []
        for item in incoming:
            if not isinstance(item,dict) or not isinstance(item.get('alias'),str):
                raise PlatformError(422,'invalid_config','Каждый канал должен иметь корректный alias')
            channel = next((x for x in current if x['alias'] == item.get('alias')), None)
            if not channel:
                raise PlatformError(422, 'new_channel_requires_check', 'Новые каналы добавляются через проверку прав Telegram')
            if item.get('telegram_chat_id', channel['telegram_chat_id']) != channel['telegram_chat_id']:
                raise PlatformError(409, 'channel_identity_immutable', 'Telegram ID существующего канала менять нельзя')
            allowed = {'title', 'publish_mode', 'publish_interval_seconds', 'caption_template', 'default_tags', 'is_enabled', 'is_paused'}
            patch = {k: v for k, v in item.items() if k in allowed and channel.get(k) != v}
            if 'publish_mode' in patch and patch['publish_mode'] not in {'auto','photo','document'}:
                raise PlatformError(422,'invalid_config','Недопустимый режим публикации канала')
            if 'publish_interval_seconds' in patch and (type(patch['publish_interval_seconds']) is not int or not 0<=patch['publish_interval_seconds']<=31536000):
                raise PlatformError(422,'invalid_config','Недопустимый интервал публикаций')
            if any(name in patch and type(patch[name]) is not bool for name in ('is_enabled','is_paused')):
                raise PlatformError(422,'invalid_config','Включение и пауза канала должны быть логическими значениями')
            if 'default_tags' in patch and (not isinstance(patch['default_tags'],list) or len(patch['default_tags'])>100 or any(not isinstance(tag,str) for tag in patch['default_tags'])):
                raise PlatformError(422,'invalid_config','Теги должны быть списком строк')
            if 'title' in patch and (not isinstance(patch['title'],str) or not 1<=len(patch['title'])<=255):
                raise PlatformError(422,'invalid_config','Название канала должно содержать от 1 до 255 символов')
            if 'caption_template' in patch and patch['caption_template'] is not None and (not isinstance(patch['caption_template'],str) or len(patch['caption_template'])>10000):
                raise PlatformError(422,'invalid_config','Недопустимый шаблон подписи')
            if patch:
                changes.append({'id': channel['id'], 'expected_version': channel['version'], 'before': {k: channel.get(k) for k in patch}, 'after': patch})
        plan = {'id': str(uuid4()), 'actor_id': actor_id, 'expires_at': __import__('time').time() + 900, 'changes': changes}
        self.gate.save('config-import-plan', plan['id'], plan)
        return plan

    async def config_import_apply(self, actor_id, payload, idempotency_key):
        plan = self.gate.load('config-import-plan', payload.get('plan_id', '')) if self.gate else None
        if not plan or plan['actor_id'] != actor_id or plan['expires_at'] < __import__('time').time():
            raise PlatformError(409, 'plan_expired', 'Обновите план импорта настроек')
        async with self._write(actor_id) as session:
            previous, request_hash = await self._receipt(session, actor_id, 'config-import', idempotency_key, payload)
            if previous is not None:
                return previous
            for change in plan['changes']:
                channel = await session.get(Channel, change['id'])
                self._version(channel, change)
                for name, value in change['after'].items():
                    setattr(channel, name, value)
                if set(change['after']) & {'caption_template','default_tags','publish_mode'}:
                    await self._invalidate_channel_plans(session,channel.id)
                self._audit(session, actor_id, 'channel.config_import', channel.id, fields=list(change['after']))
            result = {'status': 'applied', 'channels_changed': len(plan['changes'])}
            self._save_receipt(session, actor_id, 'config-import', idempotency_key, request_hash, result)
            return result

    async def queue(self,actor_id,*,channel_id=None,**kwargs):
        result=await super().queue(actor_id,channel_id=channel_id,**kwargs)
        from app.db.models import Job
        from sqlalchemy.orm import selectinload
        from app.utils.queue_schedule import estimate_queue_schedule
        async with self.sessions() as session:
            jobs=(await session.scalars(select(Job).options(selectinload(Job.channel)).where(Job.id.in_([x['id'] for x in result['items']])))).all()
            estimates={j.id:when for j,when in estimate_queue_schedule(jobs)}
        for item in result['items']:item['schedule']['estimate']=iso(estimates.get(item['id']))
        return result

    async def calendar(self, actor_id, *, channel_id=None, start=None, end=None, limit=100, cursor=None):
        """Page a complete date selection, forecasting lightweight queue metadata only."""
        import base64,heapq,binascii
        from app.db.models import Job
        from sqlalchemy.orm import selectinload
        if not isinstance(limit,int) or not 1<=limit<=100:
            raise PlatformError(422,'invalid_limit','Размер страницы должен быть от 1 до 100')
        def aware(value):return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value
        bounds=[]
        for value in (start,end):
            try:
                parsed=datetime.fromisoformat(value.replace('Z','+00:00')) if value else None
                if parsed is not None and parsed.tzinfo is None:raise ValueError()
                bounds.append(parsed.astimezone(UTC) if parsed else None)
            except (ValueError,TypeError,AttributeError):
                raise PlatformError(422,'invalid_date','Укажите UTC-границу календаря с часовым поясом') from None
        if bounds[0] and bounds[1] and bounds[0]>=bounds[1]:
            raise PlatformError(422,'invalid_range','Конец периода должен быть позже начала')
        scope=hashlib.sha256(json.dumps([channel_id,start,end],sort_keys=True).encode()).hexdigest()
        after=None;anchor=utcnow()
        if cursor:
            try:
                if len(cursor)>1024:raise ValueError()
                page=json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                if page['scope']!=scope:raise ValueError()
                anchor=datetime.fromisoformat(page['anchor']);after=(datetime.fromisoformat(page['after'][0]),int(page['after'][1]))
                if anchor.tzinfo is None or after[0].tzinfo is None:raise ValueError()
            except (ValueError,KeyError,TypeError,AttributeError,binascii.Error):
                raise PlatformError(422,'invalid_cursor','Обновите страницу календаря') from None
        async with self.sessions() as session:
            await self._actor(session,actor_id)
            query=select(Job.id,Job.target_channel_id,Job.status,Job.force_publish,Job.queue_position,
                Job.created_at,Job.next_attempt_at,Job.scheduled_at,Channel.alias,
                Channel.publish_interval_seconds,Channel.next_publish_at,Channel.is_paused,Channel.is_enabled).join(Channel,Job.target_channel_id==Channel.id).where(
                    Job.status.in_(['queued','scheduled','downloading','processing','publishing']))
            if channel_id is not None:query=query.where(Job.target_channel_id==channel_id)
            rows=[dict(row._mapping) for row in (await session.execute(query)).all()]
            grouped={}
            for row in rows:grouped.setdefault(row['target_channel_id'],[]).append(row)
            estimates={}
            for group in grouped.values():
                channel=group[0];interval=timedelta(seconds=channel['publish_interval_seconds'])
                next_slot=max(anchor,aware(channel['next_publish_at']) or anchor)
                immediate=[r for r in group if r['status'] in {'downloading','processing','publishing'} or r['force_publish']]
                immediate_ids={r['id'] for r in immediate}
                regular=[r for r in group if r['id'] not in immediate_ids and r['scheduled_at'] is None]
                last_immediate=None
                for row in immediate:
                    moment=max(anchor,aware(row['next_attempt_at']) or anchor);estimates[row['id']]=moment
                    if row['scheduled_at'] is None:last_immediate=max(last_immediate or moment,moment)
                if last_immediate is not None:next_slot=last_immediate+interval
                pending=[];ready=[]
                for row in regular:
                    priority=(row['queue_position'] is None,row['queue_position'] or 0,aware(row['created_at']),row['id'])
                    heapq.heappush(pending,(max(anchor,aware(row['next_attempt_at']) or anchor),row['id'],priority))
                while pending or ready:
                    if not ready and pending and pending[0][0]>next_slot:next_slot=pending[0][0]
                    while pending and pending[0][0]<=next_slot:
                        available,job_id,priority=heapq.heappop(pending);heapq.heappush(ready,(priority,job_id))
                    _,job_id=heapq.heappop(ready);estimates[job_id]=next_slot
                    try:next_slot+=interval
                    except OverflowError:next_slot=datetime.max.replace(tzinfo=UTC)
            matching=[];overdue=[]
            for row in rows:
                moment=aware(row['scheduled_at']) if row['scheduled_at'] else estimates.get(row['id'])
                if row['scheduled_at'] and moment<anchor:overdue.append((moment,row['id']))
                if moment and (bounds[0] is None or moment>=bounds[0]) and (bounds[1] is None or moment<bounds[1]):matching.append((moment,row['id']))
            matching.sort();overdue.sort();total=len(matching)
            remaining=[entry for entry in matching if after is None or entry>after]
            selected=remaining[:limit];overdue_page=overdue[:25]
            selected_ids={job_id for _,job_id in selected+overdue_page}
            jobs=(await session.scalars(select(Job).options(selectinload(Job.channel)).where(Job.id.in_(selected_ids)))).all() if selected_ids else []
            values={}
            for job in jobs:
                dto=await self._dto(session,job)
                dto['schedule']['estimate']=iso(estimates.get(job.id))
                if job.scheduled_at and aware(job.scheduled_at)<anchor:dto['overdue']=True
                if not job.channel.is_enabled or job.channel.is_paused and not job.force_publish:
                    dto['schedule']['blocked_reason']='Канал выключен или приостановлен; прогноз изменится после возобновления'
                values[job.id]=dto
        next_cursor=None
        if len(remaining)>limit:
            last=selected[-1]
            next_cursor=base64.urlsafe_b64encode(json.dumps({'scope':scope,'anchor':anchor.isoformat(),'after':[last[0].isoformat(),last[1]]},separators=(',',':')).encode()).decode().rstrip('=')
        return {'items':[values[job_id] for _,job_id in selected if job_id in values],
                'overdue_items':[values[job_id] for _,job_id in overdue_page if job_id in values],
                'overdue_total':len(overdue),'overdue_has_more':len(overdue)>len(overdue_page),
                'timezone':(await self.settings(actor_id)).get('timezone','Asia/Vladivostok'),
                'next_cursor':next_cursor,'total':total,'partial':next_cursor is not None,'forecast_at':iso(anchor)}

    async def bulk(self, actor_id, payload, idempotency_key):
        items = payload.get('items', [])
        action = payload.get('action')
        if action not in {'cancel', 'retry', 'enqueue', 'return-to-draft'} or not isinstance(items, list) or len(items) > 100:
            raise PlatformError(422, 'invalid_bulk', 'Недопустимая массовая команда')
        if payload.get('expected_selection_count') != len(items) or len({x.get('id') for x in items}) != len(items):
            raise PlatformError(409, 'selection_changed', 'Набор выбранных публикаций изменился')
        results = []
        for item in items:
            try:
                stable_key = str(__import__('uuid').uuid5(__import__('uuid').UUID(idempotency_key), str(item['id'])))
                result = await self.publication_action(actor_id, item['id'], action,
                         {'expected_version': item.get('expected_version')}, stable_key)
                results.append({'id': item['id'], 'status': 'success', 'result': result})
            except PlatformError as error:
                results.append({'id': item.get('id'), 'status': 'not_found' if error.status == 404 else 'blocked' if error.status == 503 else 'conflict',
                                'error': {'code': error.code, 'message': error.message}})
        return {'items': results}

    async def check_channel(self, actor_id, channel_id):
        await self.get_channel(actor_id, channel_id)
        if not self.runtime:
            raise PlatformError(503, 'telegram_unavailable', 'Для проверки прав запустите настроенный runtime')
        self.gate.assert_mutation_allowed() if self.gate else None
        async with self.sessions() as session:
            channel = await session.get(Channel, channel_id)
            chat_id = channel.telegram_chat_id
        try:
            me = await self.runtime.bot.get_me()
            membership = await self.runtime.bot.get_chat_member(chat_id, me.id)
            can_post = membership.status in {'creator', 'administrator'} and getattr(membership, 'can_post_messages', True)
        except Exception:
            raise PlatformError(503, 'telegram_check_failed', 'Telegram не ответил. Предыдущие сведения сохранены.') from None
        async with self._write(actor_id) as session:
            self._audit(session, actor_id, 'channel.check', channel_id, can_post=bool(can_post), checked_at=iso(utcnow()))
        return {'channel_id': channel_id, 'can_post': bool(can_post), 'checked_at': iso(utcnow())}

    async def channel_statistics(self, actor_id, channel_id):
        await self.get_channel(actor_id, channel_id)
        from app.db.models import ChannelMemberSnapshot
        async with self.sessions() as session:
            rows = (await session.scalars(select(ChannelMemberSnapshot).where(ChannelMemberSnapshot.channel_id == channel_id)
                        .order_by(ChannelMemberSnapshot.captured_at.desc()).limit(90))).all()
            return {'items': [{'member_count': x.member_count, 'source': x.source, 'captured_at': iso(x.captured_at)} for x in rows],
                    'status': 'known' if rows else 'unchecked', 'last_checked_at': iso(rows[0].captured_at) if rows else None}

    async def ai_workers(self, actor_id):
        return {'items': (await self.ai(actor_id))['workers']}

    async def ai_tasks(self, actor_id):
        items=(await self.ai(actor_id))['tasks']
        blocked=False
        if self.gate:
            try:self.gate.assert_mutation_allowed()
            except RuntimeError:blocked=True
        for item in items:
            item['allowed_actions']=[] if blocked else self._ai_actions(item['status'])
            if blocked:item['blocked_reason']='AI-задачи остановлены для обслуживания'
        return {'items':items}

    @staticmethod
    def _ai_actions(status):
        return {'queued':['cancel','reassign'],'leased':['cancel'],'finalizing':['cancel'],
                'failed':['retry','reassign','cancel'],'cancelled':['retry','reassign']}.get(status,[])

    async def ai_models(self, actor_id):
        workers = (await self.ai(actor_id))['workers']
        result = []
        for worker in workers:
            for model in worker.get('models', []):
                value = {'id': model} if isinstance(model, str) else dict(model)
                if 'embed' in str(value.get('type', '')).lower() or 'embed' in str(value.get('id', '')).lower():
                    continue
                result.append({**value, 'worker_id': worker['id'], 'available': worker['status'] == 'online',
                               'selected': value.get('id') == worker.get('selected_model')})
        return {'items': result}

    async def pair_worker(self, actor_id, payload):
        if not self.gate:
            raise PlatformError(503, 'managed_host_required', 'Привязка доступна через управляющий host')
        await self.validate_actor(actor_id)
        self.gate.assert_mutation_allowed()
        code = secrets.token_urlsafe(24)
        self.gate.save('worker_pair', hashlib.sha256(code.encode()).hexdigest(),
                       {'actor_id': actor_id, 'expires': __import__('time').time() + 300, 'used': False})
        return {'pairing_code': code, 'expires_in': 300}

    async def revoke_worker(self, actor_id, worker_id):
        async with self._write(actor_id) as session:
            worker = await session.get(AIWorker, worker_id)
            if not worker:
                raise PlatformError(404, 'not_found', 'Исполнитель не найден')
            worker.revoked_at = utcnow()
            tasks = (await session.scalars(select(NewsTask).where(NewsTask.lease_owner == worker_id, NewsTask.status == 'leased'))).all()
            for task in tasks:
                task.status = 'queued'; task.lease_token = None; task.lease_owner = None; task.lease_expires_at = None
                task.lease_generation += 1
            self._audit(session, actor_id, 'worker.revoke', worker_id)
            return {'status': 'revoked'}

    async def ai_selection(self, actor_id, payload):
        async with self._write(actor_id) as session:
            worker = await session.get(AIWorker, payload.get('worker_id'))
            model_id = payload.get('model_id', payload.get('model'))
            if not worker or worker.revoked_at:
                raise PlatformError(404, 'not_found', 'Исполнитель не найден')
            from app.application.worker_protocol import _model_ids
            models = _model_ids(worker.models)
            if model_id not in models or 'embed' in str(model_id).lower():
                raise PlatformError(422, 'model_unavailable', 'Текстовая модель отсутствует в каталоге исполнителя')
            worker.selected_model = model_id
            state = await session.get(ApplicationState, 1)
            if state is None:
                state=ApplicationState(id=1,system_id=self.gate.identity()['system_id'] if self.gate else str(uuid4()),version=1,settings={},publication_blocks={},maintenance_state='normal')
                session.add(state)
            task_kind=payload.get('task_kind','all')
            if task_kind not in {'all','news','title_translation'}:
                raise PlatformError(422,'invalid_task_kind','Выберите новости, перевод названий или все задачи')
            selections=dict(state.settings.get('ai_selections',{}))
            kinds=['news','title_translation'] if task_kind=='all' else [task_kind]
            for kind in kinds:selections[kind]={'worker_id':worker.id,'model_id':model_id}
            state.settings = {**state.settings, 'ai_worker_id': worker.id, 'ai_model': model_id,'ai_selections':selections}; state.version += 1
            self._audit(session, actor_id, 'ai.selection', worker.id, model=model_id)
            return {'worker_id': worker.id, 'model_id': model_id, 'applies_to': 'new_tasks'}

    async def ai_task_action(self, actor_id, task_id, action, payload):
        async with self._write(actor_id) as session:
            task = await session.get(NewsTask, task_id)
            if not task:
                raise PlatformError(404, 'not_found', 'AI-задача не найдена')
            if type(payload.get('expected_version')) is not int:
                raise PlatformError(422,'expected_version_required','Укажите версию AI-задачи')
            if payload['expected_version']!=task.lease_generation:
                raise PlatformError(409,'version_conflict','AI-задача уже изменилась. Обновите её состояние.',current_version=task.lease_generation)
            if action not in self._ai_actions(task.status):
                raise PlatformError(409, 'invalid_state', 'Это действие недоступно для текущего состояния AI-задачи')
            if action == 'cancel':
                task.status = 'cancelled'
                task.stage='cancelled';task.stage_message='AI-задача отменена';task.completed_at=utcnow()
            elif action in {'retry', 'reassign'}:
                if action == 'reassign' and task.status == 'leased':
                    raise PlatformError(409, 'task_active', 'Сначала отмените выполняемую попытку')
                task.status = 'queued'
                requested_worker=payload.get('worker_id')
                requested_model=payload.get('model_id')
                if action=='reassign':
                    if not requested_worker:
                        requested_worker,default_model=await self._selected_ai_target(session,task.kind)
                        requested_model=requested_model or default_model
                    worker=await session.get(AIWorker,requested_worker) if requested_worker else None
                    from app.application.worker_protocol import _model_ids
                    if worker is None or worker.revoked_at or requested_model not in _model_ids(worker.models):
                        raise PlatformError(422,'model_unavailable','Выберите исполнителя и текстовую модель из его каталога')
                    task.model_name=requested_model
                    task.input_payload={**task.input_payload,'metadata':{**task.input_payload.get('metadata',{}),'worker_id':worker.id,'awaiting_selection':False}}
                if payload.get('model_id') and action=='reassign':
                    models=(await session.scalars(select(AIWorker.models).where(AIWorker.revoked_at.is_(None)))).all()
                    available={model if isinstance(model,str) else model.get('id') for catalogue in models for model in catalogue}
                    if payload['model_id'] not in available or 'embed' in str(payload['model_id']).lower():
                        raise PlatformError(422,'model_unavailable','Выберите текстовую модель из каталога исполнителя')
                    task.model_name = payload['model_id']
                if action=='retry':task.attempts=0
                task.completed_at=None;task.error_message=None;task.stage='queued';task.stage_message='Ждём домашний компьютер'
            task.lease_generation += 1; task.lease_token = None; task.lease_owner = None; task.lease_expires_at = None
            if task.operation_id:
                operation=await session.get(Operation,task.operation_id)
                if operation:
                    operation.status='cancelled' if action=='cancel' else 'waiting_worker';operation.stage_key=task.stage;operation.version+=1
                    operation.message='AI-задача отменена' if action=='cancel' else 'Ждём домашний компьютер'
                    if action!='cancel':operation.error=None;operation.cancel_requested=False
            self._audit(session, actor_id, 'ai.' + action, task_id)
            return {'id': task.id, 'status': task.status}

    async def worker_command(self, operation, payload, credential=''):
        from app.application.worker_protocol import worker_command
        return await worker_command(self, operation, payload, credential)

    async def legacy_worker_command(self,operation,payload,credential=''):
        """Transition adapter over the same news queue, with its own old credential."""
        import os,hmac
        expected=getattr(getattr(self.runtime,'settings',None),'news_worker_token',None) if self.runtime else None
        expected=expected or os.environ.get('NEWS_WORKER_TOKEN')
        if not expected and self.gate:
            from app.host.secrets import SecretStore
            expected=SecretStore(self.gate.root).read('NEWS_WORKER_TOKEN')
        if not expected or not hmac.compare_digest(str(expected),str(credential)):
            raise PlatformError(401,'legacy_worker_unauthorized','Учётные данные старого исполнителя недействительны')
        if operation=='health':return {'status':'ok','protocol':'legacy-news-adapter'}
        if self.gate:
            try:self.gate.assert_mutation_allowed()
            except RuntimeError as error:raise PlatformError(503,'maintenance','Очередь AI остановлена для обслуживания') from error
        if operation=='lease':
            worker_id=str(payload.get('worker_id','')).strip()[:128]
            if not worker_id:raise PlatformError(422,'worker_id_required','Укажите имя исполнителя')
            capabilities=payload.get('capabilities') or {}
            if not isinstance(capabilities,dict):raise PlatformError(422,'invalid_capabilities','Неверные возможности исполнителя')
            model=str(capabilities.get('model') or '').strip()[:128]
            if not model:return None  # Offline/unselected model never consumes an attempt.
            try:lease_seconds=min(21600,max(60,int(payload.get('lease_seconds',1800))))
            except (TypeError,ValueError):raise PlatformError(422,'invalid_lease','Неверный срок аренды')
            source_types=payload.get('source_types')
            if source_types is not None and (not isinstance(source_types,list) or not set(source_types)<={'manual','website','telegram','youtube'}):
                raise PlatformError(422,'invalid_sources','Неверные типы источников')
            task=await self.news.lease(worker_id=worker_id,lease_seconds=lease_seconds,source_types=source_types,model_name=model,allow_unassigned_model=True,unassigned_worker_only=True)
            return task.model_dump(mode='json') if task else None
        try:task_id=int(payload.get('task_id',0))
        except (TypeError,ValueError):raise PlatformError(422,'invalid_task','Неверный ID задачи')
        token=str(payload.get('lease_token',''))
        async with self.sessions() as session:
            task=await session.get(NewsTask,task_id)
            if not task or task.kind!='news':raise PlatformError(409,'lease_lost','Задача недоступна старому news-worker')
        try:
            if operation=='progress':
                from app.news.worker_models import ProgressStage
                try:stage=ProgressStage(payload.get('stage')).value
                except (TypeError,ValueError):raise PlatformError(422,'invalid_stage','Неизвестный этап AI')
                task,_=await self.news.progress(task_id,lease_token=token,stage=stage,message=str(payload.get('message',''))[:300])
                return {'status':task.status,'stage':task.stage}
            if operation=='complete':
                completed=await self.news.complete(task_id,lease_token=token,result_payload=payload.get('result',{}))
                return {'status':'completed','job_id':completed.job_id}
            if operation=='fail':
                if not isinstance(payload.get('retryable',False),bool):raise PlatformError(422,'invalid_retry','Неверный признак повтора')
                task=await self.news.fail(task_id,lease_token=token,error='Ошибка старого домашнего исполнителя',retryable=payload.get('retryable',False))
                return {'status':task.status,'stage':task.stage}
        except NewsTaskLeaseError:raise PlatformError(409,'lease_lost','Аренда истекла или отозвана') from None
        raise PlatformError(422,'invalid_worker_command','Неизвестная команда старого исполнителя')
