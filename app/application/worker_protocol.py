"""Scoped worker protocol; shares the existing news task table and completion path."""
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, text, or_, and_
from app.application.platform import PlatformError,iso
from app.db.models import AIWorker,NewsTask,Job,Operation,OperationEvent,CommandReceipt,utcnow
from app.services.news_task_service import NewsTaskLeaseError
from app.services.title_client import TitleTranslation
from app.runtime import InstanceLock


_WORKER_FAILURE_MESSAGES = {
    "missing_news_dependency": "Не установлены зависимости для обработки видео на домашнем worker",
    "news_content_too_large": "Источник превышает настроенный лимит длительности или размера",
    "news_source_unavailable": "Не удалось получить или обработать источник; проверьте локальную модель и доступ к видео",
    "news_transcript_unavailable": "У видео нет доступных субтитров",
    "unsupported_news_source": "Источник не поддерживается; для видео нужна локальная vision-модель",
    "source_text_too_large": "Материал не поместился в лимит частей; проверьте настройки источника и контекста",
}


def _model_ids(models):
    result=[]
    for model in models:
        if isinstance(model,str):name=model;kind="text"
        elif isinstance(model,dict):name=model.get("id");kind=str(model.get("type","text"))
        else:raise PlatformError(422,"invalid_models","Некорректный каталог моделей")
        if not isinstance(name,str) or not name or len(name)>255:raise PlatformError(422,"invalid_models","Некорректный ID модели")
        if "embed" not in (name+kind).lower():result.append(name)
    return list(dict.fromkeys(result))


async def _event(session,task,status,message,result=None):
    if not task.operation_id:return
    op=await session.get(Operation,task.operation_id)
    if not op:return
    op.status=status;op.stage_key=task.stage;op.message=message;op.heartbeat_at=utcnow();op.version+=1;op.last_event_seq+=1
    if not op.started_at:op.started_at=utcnow()
    if result is not None:op.result=result
    session.add(OperationEvent(operation_id=op.id,sequence=op.last_event_seq,stage_key=op.stage_key,status=status,message=message))


async def worker_command(service,operation,payload,credential=""):
    gate=service.gate
    if not gate:raise PlatformError(503,"managed_host_required","Нет управляющего узла")
    try:gate.assert_mutation_allowed()
    except RuntimeError as error:raise PlatformError(503,"maintenance","Исполнитель ожидает окончания обслуживания") from error
    identity=gate.identity()
    if len(json.dumps(payload,ensure_ascii=False))>2*1024*1024:
        raise PlatformError(413,"payload_too_large","Превышен размер результата исполнителя")
    if operation=="register":
        code_hash=hashlib.sha256(str(payload.get("pairing_code","")).encode()).hexdigest()
        with InstanceLock(gate.root/"worker-pair.lock"):
            record=gate.load("worker_pair",code_hash)
            if not record or record["used"] or record["expires"]<time.time():raise PlatformError(401,"invalid_pairing_code","Код привязки истёк или уже использован")
            token,worker_id=secrets.token_urlsafe(48),str(uuid4())
            async with service.sessions() as session,session.begin():
                session.add(AIWorker(id=worker_id,name=str(payload.get("name","Домашний компьютер"))[:128],provider=str(payload.get("provider","lmstudio"))[:32],credential_hash=hashlib.sha256(token.encode()).hexdigest(),capabilities={},models=[],last_seen_at=utcnow()))
            record["used"]=True;gate.save("worker_pair",code_hash,record)
        return {"worker_id":worker_id,"credential":token,"system_id":identity["system_id"],"generation":identity["generation"]}
    async with service.sessions() as session:
        worker=await session.scalar(select(AIWorker).where(AIWorker.credential_hash==hashlib.sha256(credential.encode()).hexdigest()))
        if not worker or worker.revoked_at:raise PlatformError(401,"worker_unauthorized","Учётные данные исполнителя недействительны")
    if operation in {"heartbeat","capabilities"}:
        async with service.sessions() as session,session.begin():
            current=await session.get(AIWorker,worker.id);current.last_seen_at=utcnow()
            if operation=="capabilities":
                models=payload.get("models",[])
                if not isinstance(models,list) or len(models)>500:raise PlatformError(422,"invalid_models","Слишком большой каталог моделей")
                ids=_model_ids(models)
                current.models=[m for m in models if (m if isinstance(m,str) else m.get("id")) in ids]
                capabilities=payload.get("capabilities",{})
                if not isinstance(capabilities,dict):raise PlatformError(422,"invalid_capabilities","Неверные возможности исполнителя")
                current.capabilities={"task_kinds":[k for k in capabilities.get("task_kinds",["news","title_translation"]) if k in {"news","title_translation"}],"source_types":[k for k in capabilities.get("source_types",["website","youtube","telegram","manual"]) if k in {"website","youtube","telegram","manual"}]}
            if payload.get("task_id"):
                task=await session.get(NewsTask,int(payload["task_id"]))
                _validate_lease(task,worker.id,payload,identity)
                task.lease_expires_at=utcnow()+timedelta(seconds=1800)
        return {"worker_id":worker.id,"status":"online","generation":identity["generation"],"selected_model":current.selected_model,"system_id":identity["system_id"]}
    if operation=="lease":
        ready_models=payload.get("ready_models",[])
        if not isinstance(ready_models,list) or len(ready_models)>500:raise PlatformError(422,"invalid_models","Неверный список доступных моделей")
        catalog=set(_model_ids(worker.models))
        ready=set(_model_ids(ready_models))&catalog
        if not ready:return None
        kinds=set(payload.get("task_kinds",["news","title_translation"]))&set(worker.capabilities.get("task_kinds",["news","title_translation"]))
        source_types=set(worker.capabilities.get("source_types",["website","youtube","telegram","manual"]))
        async with service.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            preferred=NewsTask.input_payload['metadata']['worker_id'].as_string()
            candidates=(await session.scalars(select(NewsTask).where(NewsTask.kind.in_(kinds),NewsTask.attempts<NewsTask.max_attempts,
                or_(preferred.is_(None),preferred==worker.id),
                (or_(NewsTask.model_name.in_(ready),NewsTask.model_name.is_(None)) if worker.selected_model in ready else NewsTask.model_name.in_(ready)),
                or_(NewsTask.kind!='news',NewsTask.source_kind.in_(source_types)),
                or_(NewsTask.status=="queued",and_(NewsTask.status=="leased",NewsTask.lease_expires_at<=utcnow()))).order_by(NewsTask.created_at,NewsTask.id).limit(100))).all()
            task=None
            for candidate in candidates:
                metadata=candidate.input_payload.get('metadata',{})
                if metadata.get('awaiting_selection'):continue
                if metadata.get('worker_id') and metadata['worker_id']!=worker.id:continue
                model=candidate.model_name or worker.selected_model
                if model not in ready:continue
                if candidate.kind=="news" and candidate.source_kind not in source_types:continue
                task=candidate;break
            if not task:await session.rollback();return None
            task.status="leased";task.lease_generation+=1;task.lease_owner=worker.id;task.lease_token=secrets.token_urlsafe(32);task.lease_expires_at=utcnow()+timedelta(seconds=1800);task.attempts+=1;task.model_name=task.model_name or worker.selected_model;task.stage="leased";task.stage_message="Задача принята домашним исполнителем"
            await _event(session,task,"running",task.stage_message)
            result={"id":task.id,"kind":task.kind,"lease_token":task.lease_token,"generation":identity["generation"],"lease_revision":task.lease_generation,"model_name":task.model_name,"input_payload":task.input_payload,"deadline":iso(task.lease_expires_at),"system_id":identity["system_id"]}
            await session.commit();return result
    try:task_id=int(payload.get("task_id",0))
    except (TypeError,ValueError):raise PlatformError(422,"invalid_task","Неверный ID задачи")
    async with service.sessions() as session:
        task=await session.get(NewsTask,task_id)
        if not task:raise PlatformError(409,"lease_lost","Задача не найдена")
        if payload.get("generation")!=identity["generation"]:raise PlatformError(409,"stale_generation","Аренда относится к прежнему поколению узла")
        if task.status!="completed":_validate_lease(task,worker.id,payload,identity)
    token=str(payload.get("lease_token",""))
    if task.kind=="news" and operation in {"progress","complete","fail"}:
        try:
            if operation=="progress":
                task,_=await service.news.progress(task_id,lease_token=token,stage=str(payload.get("stage","working"))[:64],message=str(payload.get("message","Обрабатываем новость"))[:300]);return {"status":task.status}
            if operation=="complete":
                result=await service.news.complete(task_id,lease_token=token,result_payload=payload.get("result",{}));return {"status":"completed","job_id":result.job_id}
            task=await service.news.fail(task_id,lease_token=token,error="Ошибка домашнего исполнителя",retryable=bool(payload.get("retryable")));return {"status":task.status}
        except NewsTaskLeaseError:raise PlatformError(409,"lease_lost","Аренда истекла или отозвана") from None
    async with service.sessions() as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        task=await session.get(NewsTask,task_id)
        if operation=="complete" and task.kind=="title_translation":
            result=TitleTranslation.model_validate(payload.get("result",{})).model_dump()
            digest=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
            previous=await session.scalar(select(CommandReceipt).where(CommandReceipt.principal_id==task.created_by_user_id,CommandReceipt.scope=="title:complete",CommandReceipt.key==str(task.id)))
            if previous:
                if previous.payload_hash!=digest or not secrets.compare_digest(previous.result.get("token_hash",""),hashlib.sha256(token.encode()).hexdigest()) or previous.result.get("worker_id")!=worker.id:
                    raise PlatformError(409,"completion_conflict","Результат завершённой задачи не совпадает")
                response={"status":"completed","job_id":previous.result["job_id"],"applied":previous.result["applied"]}
                await session.rollback();return response
            _validate_lease(task,worker.id,payload,identity)
            job=await session.get(Job,task.input_payload.get("job_id"))
            if not job:raise PlatformError(409,"job_missing","Черновик больше не доступен")
            applied=job.status=="waiting_confirmation" and job.version==task.source_job_version and job.post_data.get("title")==task.input_payload.get("title")
            if applied:
                metadata={**job.post_data.get("metadata",{}),"title_translation":result["translated_text"],"title_translation_source_language":result["source_language"],"title_translation_model":task.model_name,"title_translation_worker":worker.id}
                job.post_data={**job.post_data,"metadata":metadata};job.render_plan=None
            task.status="completed";task.lease_generation+=1;task.result_json=result;task.job_id=job.id;task.completed_at=utcnow();task.stage="complete"
            await _event(session,task,"succeeded" if applied else "waiting_user","Перевод готов" if applied else "Черновик изменён: перевод доступен как предложение",result={"publication_id":job.id,"proposal":result,"applied":applied})
            session.add(CommandReceipt(principal_id=task.created_by_user_id,scope="title:complete",key=str(task.id),payload_hash=digest,result={"job_id":job.id,"token_hash":hashlib.sha256(token.encode()).hexdigest(),"worker_id":worker.id,"applied":applied}))
            task.lease_owner=None;task.lease_token=None;task.lease_expires_at=None
            await session.commit();return {"status":"completed","job_id":job.id,"applied":applied}
        _validate_lease(task,worker.id,payload,identity)
        if operation=="progress":
            task.stage=str(payload.get("stage","working"))[:64];task.stage_message=str(payload.get("message","Модель готовит текст"))[:300];task.lease_expires_at=utcnow()+timedelta(seconds=1800)
            await _event(session,task,"running",task.stage_message)
        elif operation in {"fail","release"}:
            retry=operation=="release" or bool(payload.get("retryable")) and task.attempts<task.max_attempts
            code=payload.get("error_code")
            failure=_WORKER_FAILURE_MESSAGES.get(code,"Ошибка домашнего исполнителя") if isinstance(code,str) else "Ошибка домашнего исполнителя"
            task.status="queued" if retry else "failed";task.stage="queued" if retry else "failed";task.stage_message="Ждём домашний компьютер" if retry else failure
            task.lease_owner=None;task.lease_token=None;task.lease_expires_at=None;task.lease_generation+=1
            await _event(session,task,"waiting_worker" if retry else "failed",task.stage_message)
        else:raise PlatformError(422,"invalid_worker_command","Неизвестная команда исполнителя")
        await session.commit();return {"status":task.status}


def _validate_lease(task,worker_id,payload,identity):
    token=str(payload.get("lease_token",""))
    if not task or task.status!="leased" or task.lease_owner!=worker_id or not task.lease_token or not secrets.compare_digest(task.lease_token,token):
        raise PlatformError(409,"lease_lost","Аренда задачи недействительна")
    if task.lease_expires_at is None or task.lease_expires_at.replace(tzinfo=UTC)<=utcnow():raise PlatformError(409,"lease_expired","Аренда задачи истекла")
    if payload.get("generation")!=identity["generation"] or payload.get("lease_revision",task.lease_generation)!=task.lease_generation:raise PlatformError(409,"stale_generation","Аренда отозвана")
