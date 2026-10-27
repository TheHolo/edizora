"""Exact-time recovery policy shared by boot, handoff and queue claims."""
from datetime import UTC, datetime, timedelta
from sqlalchemy import select, update
from app.db.models import ApplicationState, Job, JobEvent, utcnow


async def apply_overdue_in_session(session,*,state=None):
    state=state or await session.get(ApplicationState,1)
    settings=state.settings if state else {}
    policy=settings.get("overdue_policy","catch_up")
    interval=max(1,int(settings.get("catch_up_interval_seconds",60)))
    now=utcnow()
    jobs=(await session.scalars(select(Job).where(Job.status=="scheduled",Job.scheduled_at<=now).order_by(Job.scheduled_at,Job.id))).all()
    affected=[]
    for job in jobs:
        metadata=dict(job.post_data.get("metadata",{}))
        if metadata.get("overdue_policy_applied"):
            continue
        metadata["overdue_policy_applied"]=policy
        if policy=="hold":
            metadata["overdue_hold"]=True
            job.next_attempt_at=datetime(9999,1,1,tzinfo=UTC)
        elif policy=="skip":
            if job.id in settings.get("confirmed_skip_job_ids",[]):
                job.status="cancelled";job.finished_at=now;job.error_code="overdue_skipped";job.error_message="Отменено по подтверждённой политике пропущенных дат"
            else:
                metadata["overdue_policy_applied"]="hold_unconfirmed_skip";metadata["overdue_hold"]=True
                job.next_attempt_at=datetime(9999,1,1,tzinfo=UTC)
        else:
            metadata["overdue_catchup"]=True
            last=settings.get("catchup_last_success",{}).get(str(job.target_channel_id))
            eligibility=datetime.fromisoformat(last.replace("Z","+00:00"))+timedelta(seconds=interval) if last else now
            job.next_attempt_at=max(now,eligibility)
        job.post_data={**job.post_data,"metadata":metadata}
        # Only recovery metadata changes; the approved content stays immutable.
        if job.render_plan:job.render_plan=dict(job.render_plan)
        session.add(JobEvent(job_id=job.id,event_type="overdue_policy",old_status="scheduled",new_status=job.status,message=f"{policy}; исходное точное время сохранено"))
        affected.append(job.id)
    return {"policy":policy,"affected_job_ids":affected,"catch_up_interval_seconds":interval}


async def record_catchup_success(session,job,published_at):
    if not job.post_data.get("metadata",{}).get("overdue_catchup"):
        return
    state=await session.get(ApplicationState,1)
    if not state:
        # Initial standalone queues need the same limiter even before settings save.
        from uuid import uuid4
        state=ApplicationState(id=1,system_id=str(uuid4()),version=1,settings={},publication_blocks={},maintenance_state="normal");session.add(state)
    settings=dict(state.settings)
    interval=max(1,int(settings.get("catch_up_interval_seconds",60)))
    settings["catchup_last_success"]={**settings.get("catchup_last_success",{}),str(job.target_channel_id):published_at.replace(tzinfo=UTC).isoformat()}
    state.settings=settings;state.version+=1
    next_time=published_at+timedelta(seconds=interval)
    jobs=(await session.scalars(select(Job).where(Job.id!=job.id,Job.target_channel_id==job.target_channel_id,Job.status=="scheduled",Job.scheduled_at<=published_at))).all()
    for waiting in jobs:
        if waiting.post_data.get("metadata",{}).get("overdue_catchup"):
            waiting.next_attempt_at=next_time
