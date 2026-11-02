"""Compare a historical full scan with the current preparation pass on a disposable fixture."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.extras import WebPlatformService
from app.application.platform import channel_render_hash
from app.db.models import Job
from app.db.session import create_database
from app.host.control import ControlStore
from app.host.secrets import SecretStore
from app.operations.runner import OperationRunner
from app.runtime import InstanceLock


async def measure(root: Path, mode: str) -> None:
    engine, sessions = create_database('sqlite+aiosqlite:///' + (root / 'data/database.db').as_posix())
    try:
        tracemalloc.start()
        started = time.perf_counter()
        if mode == 'current':
            await OperationRunner(WebPlatformService(sessions, data_root=root / 'data'))._queue_preparations()
        else:
            # The former full scan is read-only here: it reports how many
            # drafts would have needed preparation without scheduling work.
            async with sessions() as session:
                jobs = (await session.scalars(select(Job).options(selectinload(Job.channel)).where(
                    Job.status == 'waiting_confirmation'))).all()
                stale = 0
                for job in jobs:
                    if job.content_kind == 'artwork' and not job.post_data.get('media_items'):
                        continue
                    fingerprint = channel_render_hash(job.channel)
                    plan = job.render_plan or {}
                    if not (plan.get('job_version') == job.version and (
                            plan.get('channel_render_hash') == fingerprint if 'channel_render_hash' in plan
                            else plan.get('channel_version') == job.channel.version)):
                        stale += 1
                print(f'Full scan: {len(jobs)} drafts, {stale} need preparation')
        _, peak = tracemalloc.get_traced_memory()
        elapsed = (time.perf_counter() - started) * 1000
        print(f'{mode}: {elapsed:.1f} ms, traced peak {peak / 1048576:.1f} MiB')
    finally:
        tracemalloc.stop()
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--mode', choices=('historical', 'current'), required=True)
    args = parser.parse_args()
    root = args.fixture.resolve(strict=True)
    if (root / '.edizora-prototype').read_text(encoding='utf-8') != 'disposable performance fixture':
        raise SystemExit('A generated disposable performance fixture is required')
    store = ControlStore(root / 'control')
    if store.status()['publication_allowed'] or SecretStore(root / 'control').read('BOT_TOKEN'):
        raise SystemExit('Fixture must remain paused and have no Telegram token')
    with InstanceLock(root / 'control/host.lock'):
        asyncio.run(measure(root, args.mode))
