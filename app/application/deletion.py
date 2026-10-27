"""Permanent publication deletion with durable, reference-checked file cleanup."""
import time
from pathlib import Path

from sqlalchemy import delete, select, text

from app.application.platform import PlatformError
from app.db.models import (ApplicationState, CommandReceipt, Job, JobAsset, JobEvent, MediaAsset, MediaRecord,
    NewsTask, Operation, OperationEvent, Publication, PublicationAttempt, PublicationPart)
from app.storage.assets import safe_path


DELETABLE_STATUSES={"waiting_confirmation","queued","scheduled","completed","failed","cancelled"}
BUSY_OPERATIONS={"running","cancelling","waiting_worker","waiting_external"}


def strings(value):
    if isinstance(value,str):return {value}
    if isinstance(value,dict):return set().union(*(strings(v) for v in value.values())) if value else set()
    if isinstance(value,list):return set().union(*(strings(v) for v in value)) if value else set()
    return set()


def batch_items(op):
    value=(op.result or {}).get('items',{})
    return value if isinstance(value,dict) else {}


def download_root(platform):
    downloader=getattr(getattr(platform.runtime,'workers',None),'downloader',None)
    storage=getattr(downloader,'storage',None)
    return Path(storage).resolve() if storage is not None else None


def managed_key(root,value):
    if not value:return None
    path=Path(value)
    if path.is_absolute():
        try:value=path.relative_to(root).as_posix()
        except ValueError:return None
    # Only managed media namespaces can ever be removed, never databases/configuration.
    if value.split('/')[0] not in {"assets","thumbnails","jobs"} and not value.startswith('storage/jobs/'):return None
    try:safe_path(root,value)
    except ValueError:return None
    return value


async def delete_publication(platform,actor_id,job_id,payload,idempotency_key):
    async with platform._write(actor_id) as session:
        scope=f"publication:{job_id}:delete"
        previous,digest=await platform._receipt(session,actor_id,scope,idempotency_key,payload)
        if previous is not None:
            result=previous
        else:
            job=await platform._job(session,job_id)
            platform._version(job,payload)
            if job.status not in DELETABLE_STATUSES or job.channel.active_job_id==job.id:
                raise PlatformError(409,"publication_busy","Дождитесь завершения или отмены отправки перед удалением")
            operations=list((await session.scalars(select(Operation))).all())
            related=[op for op in operations if op.job_id==job_id or
                job_id in ((op.result or {}).get('publication_ids') or []) or
                any(item.get('job_id')==job_id for item in batch_items(op).values() if isinstance(item,dict))]
            operation_ids=[op.id for op in related]
            if any(op.status in BUSY_OPERATIONS for op in related):
                raise PlatformError(409,"publication_busy","Дождитесь завершения или отмены обработки перед удалением")
            tasks=list((await session.scalars(select(NewsTask).where(
                (NewsTask.job_id==job_id)|(NewsTask.operation_id.in_(operation_ids))))).all())
            if any(task.status in {'leased','finalizing'} for task in tasks):
                raise PlatformError(409,"publication_busy","Дождитесь завершения обработки AI перед удалением")
            attempts=list((await session.scalars(select(PublicationAttempt).where(PublicationAttempt.job_id==job_id))).all())
            if any(attempt.stage=='sending' for attempt in attempts):
                raise PlatformError(409,"publication_busy","Сначала проверьте результат незавершённой отправки")
            asset_ids=set((await session.scalars(select(JobAsset.asset_id).where(JobAsset.job_id==job_id))).all())
            asset_ids.update(strings(job.render_plan)&set((await session.scalars(select(MediaAsset.id))).all()))
            variants=(await session.scalars(select(MediaAsset).where(MediaAsset.source_kind=='prepared',MediaAsset.source_reference.in_(asset_ids)))).all()
            asset_ids.update(asset.id for asset in variants)
            records=list((await session.scalars(select(MediaRecord).where(MediaRecord.job_id==job_id))).all())
            keys={key for record in records for value in (record.local_path,record.prepared_path)
                if (key:=managed_key(platform.data_root,value))}
            # Include managed per-job download caches; external user originals are never removed.
            for prefix in ('jobs','storage/jobs'):
                for name in [str(job_id),*(f'prepare-{identifier}' for identifier in operation_ids)]:
                    directory=platform.data_root/prefix/name
                    if directory.is_dir() and not directory.is_symlink():
                        keys.update(key for path in directory.rglob('*') if path.is_file()
                            and (key:=managed_key(platform.data_root,path.relative_to(platform.data_root).as_posix())))
            cache_root=download_root(platform)
            cache_keys=set()
            if cache_root:
                for name in [str(job_id),*(f'prepare-{identifier}' for identifier in operation_ids)]:
                    directory=cache_root/'jobs'/name
                    if directory.is_dir() and not directory.is_symlink():
                        for path in directory.rglob('*'):
                            key=path.relative_to(cache_root).as_posix()
                            if path.is_file() and managed_key(cache_root,key):cache_keys.add(key)
            removed_operations=[]
            for op in related:
                entries=batch_items(op)
                others=[v['job_id'] for v in entries.values() if isinstance(v,dict) and v.get('job_id') not in {None,job_id}]
                if op.kind=='import' and others:
                    # Keep the batch receipt and completed slots so retry cannot recreate a deleted draft.
                    op.job_id=None
                    urls=list(op.payload.get('urls',[]))
                    for index,item in entries.items():
                        if isinstance(item,dict) and item.get('job_id')==job_id and str(index).isdigit() and int(index)<len(urls):urls[int(index)]=''
                    op.payload={**op.payload,'urls':urls}
                    op.result={**op.result,'items':{k:({'status':'deleted'} if v.get('job_id')==job_id else v) for k,v in entries.items()},
                        'publication_ids':others,'publication_id':others[0] if len(others)==1 else None}
                else:removed_operations.append(op.id)
            for task in tasks:await session.delete(task)
            await session.execute(delete(OperationEvent).where(OperationEvent.operation_id.in_(removed_operations)))
            await session.execute(delete(Operation).where(Operation.id.in_(removed_operations)))
            await session.execute(delete(PublicationPart).where(PublicationPart.attempt_id.in_([a.id for a in attempts])))
            await session.execute(delete(PublicationAttempt).where(PublicationAttempt.job_id==job_id))
            for model in (Publication,JobEvent,MediaRecord,JobAsset):
                await session.execute(delete(model).where(model.job_id==job_id))
            await session.execute(delete(Job).where(Job.id==job_id))
            await session.flush()
            references=set((await session.scalars(select(JobAsset.asset_id))).all())
            for plan in (await session.scalars(select(Job.render_plan))).all():references.update(strings(plan))
            for plan in (await session.scalars(select(PublicationAttempt.render_plan))).all():references.update(strings(plan))
            candidates=(await session.scalars(select(MediaAsset).where(MediaAsset.id.in_(asset_ids-references)))).all()
            for asset in candidates:
                if asset.storage_key in references or asset.thumbnail_key in references or asset.source_reference in references:continue
                keys.update(k for value in (asset.storage_key,asset.thumbnail_key) if (k:=managed_key(platform.data_root,value)))
                await session.delete(asset)
            # Old replay receipts must not resurrect a deleted resource in the UI.
            for receipt in (await session.scalars(select(CommandReceipt))).all():
                value=receipt.result or {}
                if (value.get('publication_id')==job_id or value.get('resource_id')==job_id or
                    value.get('operation_id') in removed_operations or receipt.scope.startswith(f'publication:{job_id}:') or
                    receipt.scope in {f'prepare:{job_id}',f'reimport:{job_id}',f'preview-in-telegram:{job_id}'}):
                    receipt.result={'deleted_resource':True,'resource_id':job_id}
            cleanup=platform._new_operation(session,actor_id,'storage_cleanup',{'keys':sorted(keys),'cache_root':str(cache_root) if cache_root else None,'cache_keys':sorted(cache_keys)},'Удаляем файлы публикации')
            result={'id':job_id,'deleted':True,'cleanup_operation_id':cleanup.id}
            platform._save_receipt(session,actor_id,scope,idempotency_key,digest,result)
            platform._audit(session,actor_id,'publication.delete',job_id)
    pending=await cleanup_deleted_media(platform,result['cleanup_operation_id'])
    return {**result,'cleanup_pending':pending}


async def cleanup_deleted_media(platform,operation_id):
    """The journal commits before unlink; interrupted/locked files can be retried."""
    if platform.gate:
        try:platform.gate.assert_mutation_allowed()
        except RuntimeError:return True
    async with platform.sessions() as session:
        await session.execute(text('BEGIN IMMEDIATE'))
        state=await session.get(ApplicationState,1)
        if state and state.maintenance_state!='normal':return True
        op=await session.get(Operation,operation_id)
        if op is None or op.status=='succeeded':return False
        referenced=set()
        for asset in (await session.scalars(select(MediaAsset))).all():
            referenced.update(k for v in (asset.storage_key,asset.thumbnail_key) if (k:=managed_key(platform.data_root,v)))
        for record in (await session.scalars(select(MediaRecord))).all():
            referenced.update(k for v in (record.local_path,record.prepared_path) if (k:=managed_key(platform.data_root,v)))
        for plan in (await session.scalars(select(Job.render_plan))).all():referenced.update(strings(plan))
        for plan in (await session.scalars(select(PublicationAttempt.render_plan))).all():referenced.update(strings(plan))
        pending=[]
        for key in op.payload.get('keys',[]):
            if key in referenced:continue
            if managed_key(platform.data_root,key) is None:continue
            try:safe_path(platform.data_root,key).unlink(missing_ok=True)
            except OSError:pending.append(key)
        cache_pending=[]
        cache_root=download_root(platform)
        legacy_paths={str(Path(value).resolve()) for record in (await session.scalars(select(MediaRecord))).all()
            for value in (record.local_path,record.prepared_path) if value}
        for key in op.payload.get('cache_keys',[]):
            if cache_root is None or str(cache_root)!=op.payload.get('cache_root'):
                cache_pending.append(key);continue
            if not key.startswith('jobs/') or managed_key(cache_root,key) is None:continue
            path=safe_path(cache_root,key)
            if str(path) in legacy_paths:continue
            try:path.unlink(missing_ok=True)
            except OSError:cache_pending.append(key)
        unfinished=bool(pending or cache_pending)
        op.payload={'keys':pending,'cache_root':op.payload.get('cache_root'),'cache_keys':cache_pending,
            'retry_at':time.time()+30 if unfinished else 0}
        op.status='queued' if unfinished else 'succeeded'
        op.message='Файлы временно заняты; очистка будет повторена' if unfinished else 'Запись и неиспользуемые файлы удалены'
        op.result={'cleanup_pending':unfinished}
        op.version+=1
        await session.commit()
        return unfinished
