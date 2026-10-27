"""Startup-only reconciliation after the previous runtime has lost its OS lock."""
from pathlib import Path

from sqlalchemy import select

from app.db.models import (Channel,Job,JobEvent,Operation,OperationEvent,Publication,
    PublicationAttempt,PublicationPart,utcnow)
from app.runtime import InstanceLock


def _require_runtime_lock(store,runtime_lock):
    expected=(store.root/'runtime.lock').resolve()
    if not isinstance(runtime_lock,InstanceLock) or runtime_lock.path.resolve()!=expected or runtime_lock._file is None or runtime_lock._file.closed:
        raise RuntimeError('Orphan recovery requires the acquired runtime OS lock before any executor starts')


def _known_ids(parts):
    return [value for part in parts for value in (part.message_ids or []) if type(value) is int and value>0]


def _publication_proves_completion(job,publication,parts):
    if publication is None or not publication.telegram_message_ids:return False
    expected=[value for value in publication.telegram_message_ids if type(value) is int and value>0]
    known=_known_ids(parts)
    if job.status=='completed':return set(known)<=set(expected)
    return bool(parts) and all(part.status=='confirmed' for part in parts) and known==expected


async def recover_orphaned_permits(sessions,store,*,runtime_lock):
    """Call under the real OS runtime lock, while no executor or RPC can write.

    Content uncertainty commits before deleting any durable permit. If interrupted
    between these independent stores, repeating the function preserves uncertainty
    and finishes cleanup without granting the former process any authority.
    """
    _require_runtime_lock(store,runtime_lock)
    if store.identity()['role']!='active':
        return {'skipped':'passive_node','released_permit_ids':[],'uncertain_job_ids':[]}
    permits=list(store.status()['permits'])
    permit_jobs={permit['job_id'] for permit in permits if permit['job_id']>0}
    uncertain=[];proved=[];preview_operations=[]
    async with sessions() as session,session.begin():
        attempts=list((await session.scalars(select(PublicationAttempt).where(PublicationAttempt.stage.in_(['sending','confirmed'])))).all())
        # A permit can exist before the attempt row, or after an uncertainty commit.
        if permit_jobs:
            extra=(await session.scalars(select(PublicationAttempt).where(PublicationAttempt.job_id.in_(permit_jobs),PublicationAttempt.stage.not_in(['completed','resolved_not_published'])))).all()
            attempts=list({attempt.id:attempt for attempt in [*attempts,*extra]}.values())
        by_job={}
        for attempt in attempts:by_job.setdefault(attempt.job_id,[]).append(attempt)
        for job_id in sorted(set(by_job)|permit_jobs):
            job=await session.get(Job,job_id)
            if job is None:continue
            publication=await session.scalar(select(Publication).where(Publication.job_id==job_id,Publication.channel_id==job.target_channel_id))
            rows=by_job.get(job_id,[])
            all_proved=bool(publication)
            for attempt in rows:
                parts=list((await session.scalars(select(PublicationPart).where(PublicationPart.attempt_id==attempt.id).order_by(PublicationPart.sequence))).all())
                known=_publication_proves_completion(job,publication,parts)
                all_proved=all_proved and known
                if known:
                    attempt.stage='completed'
                else:
                    attempt.stage='unknown';attempt.error='runtime_interrupted'
            if not rows:all_proved=bool(publication and publication.telegram_message_ids and job.status=='completed')
            if all_proved:
                if job.status!='completed':job.status='completed';job.finished_at=publication.published_at
                job.error_code=job.error_message=None;proved.append(job_id)
            else:
                changed=job.status!='failed' or job.error_code!='uncertain_publish'
                job.status='failed';job.error_code='uncertain_publish'
                job.error_message='Предыдущий runtime завершился во время отправки. Проверьте канал перед отдельным подтверждением повтора.'
                job.next_attempt_at=None;job.force_publish=False;job.queue_position=None
                if job.finished_at is None:job.finished_at=utcnow()
                if changed:session.add(JobEvent(job_id=job.id,event_type='runtime_recovery_unknown',new_status='failed',message='Результат отправки неизвестен; автоматический повтор запрещён'))
                uncertain.append(job_id)
            channel=await session.get(Channel,job.target_channel_id)
            if channel and channel.active_job_id==job.id:channel.active_job_id=None
        previews=(await session.scalars(select(Operation).where(Operation.kind=='telegram_preview',Operation.status.in_(['running','cancelling']),Operation.stage_key=='sending'))).all()
        for operation in previews:
            operation.status='waiting_user';operation.message='Предыдущий runtime завершился во время проверки. Проверьте личный чат; автоматического повтора не будет.'
            operation.error={'code':'uncertain_publish','message':'Результат личной проверки Telegram неизвестен'}
            operation.version+=1;operation.last_event_seq+=1;operation.heartbeat_at=utcnow()
            session.add(OperationEvent(operation_id=operation.id,sequence=operation.last_event_seq,stage_key='sending',status='waiting_user',message=operation.message))
            preview_operations.append(operation.id)
    # Never move this loop into finally: a failed content commit must keep permits.
    released=[]
    for permit in permits:
        store.release_permit(permit)
        released.append(permit['id'])
    return {'released_permit_ids':released,'uncertain_job_ids':uncertain,'proven_completed_job_ids':proved,'uncertain_preview_operation_ids':preview_operations}
