"""Generate a disposable, paused 10k-publication dataset; never use real secrets."""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import io
from pathlib import Path
import random
import sqlite3
import sys
from uuid import NAMESPACE_URL, uuid5

from PIL import Image, ImageDraw
from sqlalchemy import insert, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.models import Channel, Job, JobAsset, MediaAsset, User
from app.db.session import create_database
from app.desktop.launcher import SetupInput, initialize
from app.storage.assets import store_stream


async def generate(root: Path) -> None:
    if not root.is_absolute() or root.exists():
        raise ValueError('Fixture must be a new absolute directory')
    for ancestor in root.parents:
        if (ancestor / 'control/node.json').exists():
            raise ValueError('Fixture cannot be created inside a managed installation')
    root.mkdir(parents=True)
    (root / '.edizora-prototype').write_text('disposable performance fixture', encoding='utf-8')
    await initialize(root, SetupInput(admin_id=987654321, password='fixture-password-only',
                                     password_repeat='fixture-password-only'))
    data = root / 'data'
    rng = random.Random(20261030)
    assets = []
    for number in range(64):
        color = tuple(rng.randrange(25, 230) for _ in range(3))
        image = Image.new('RGB', (960, 640), color)
        draw = ImageDraw.Draw(image)
        for _ in range(18):
            x, y = rng.randrange(960), rng.randrange(640)
            radius = rng.randrange(30, 230)
            fill = tuple(rng.randrange(256) for _ in range(3))
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill)
        draw.text((30, 30), f'EDIZORA FIXTURE {number:02}', fill='white')
        payload = io.BytesIO()
        image.save(payload, format='JPEG', quality=86)
        payload.seek(0)
        record = store_stream(data, payload)
        record.update(id=str(uuid5(NAMESPACE_URL, f'edizora-fixture-{number}')),
                      source_kind='upload', filename=f'fixture-{number:02}.jpg')
        assets.append(record)
    engine, sessions = create_database('sqlite+aiosqlite:///' + (data / 'database.db').as_posix())
    try:
        async with sessions() as session, session.begin():
            user_id = await session.scalar(select(User.id).where(User.telegram_user_id == 987654321))
            assert user_id is not None
            channels = [{'id': number, 'alias': f'fixture-{number:02}',
                         'telegram_chat_id': str(-1009000000000 - number),
                         'title': f'Тестовый канал {number:02}', 'is_default': number == 1,
                         'is_enabled': True, 'publish_interval_seconds': 900} for number in range(1, 21)]
            await session.execute(insert(Channel), channels)
            await session.execute(insert(MediaAsset), assets)
            now = datetime(2026, 10, 1, tzinfo=UTC)
            for start in range(1, 10001, 500):
                jobs, links = [], []
                for identifier in range(start, min(start + 500, 10001)):
                    channel_id = (identifier - 1) % 20 + 1
                    is_artwork = identifier % 3 != 0
                    count = (1, 2, 3, 4)[identifier % 4] if is_artwork else 0
                    media = []
                    for index in range(count):
                        asset = assets[(identifier + index) % len(assets)]
                        links.append({'job_id': identifier, 'asset_id': asset['id'], 'source_index': index + 1,
                                      'selected': True, 'sort_order': index, 'purpose': 'original'})
                        media.append({'url': '', 'filename': asset['filename'], 'mime_type': asset['mime_type'],
                                      'media_type': 'image', 'width': asset['width'], 'height': asset['height'],
                                      'size': asset['size'], 'order': index, 'headers': {}})
                    queued = identifier <= 1000
                    status = 'queued' if queued else ('completed' if identifier % 13 == 0 else
                                                      'failed' if identifier % 29 == 0 else 'waiting_confirmation')
                    jobs.append({'id': identifier, 'created_by_user_id': user_id, 'target_channel_id': channel_id,
                                 'version': 1, 'provider': 'fixture', 'content_kind': 'artwork' if is_artwork else 'news',
                                 'source_id': str(identifier), 'source_url': f'https://example.invalid/fixture/{identifier}',
                                 'normalized_url': f'https://example.invalid/fixture/{identifier}', 'status': status,
                                 'queue_position': (identifier - 1) // 20 + 1 if queued else None,
                                 'post_data': {'title': f'Тестовая публикация {identifier:05}',
                                     'description': 'Синтетический материал для проверки интерфейса.',
                                     'body': 'Никакие рабочие токены и данные здесь не используются.',
                                     'author_name': f'Автор {identifier % 200:03}', 'media_items': media,
                                     'metadata': {'text_only': not is_artwork}},
                                 'user_tags': ['fixture', f'tag-{identifier % 40}'],
                                 'source_tags': [], 'render_plan': {'job_version': 1, 'channel_version': 1},
                                 'created_at': now + timedelta(minutes=identifier),
                                 'updated_at': now + timedelta(minutes=identifier)})
                await session.execute(insert(Job), jobs)
                if links:
                    await session.execute(insert(JobAsset), links)
    finally:
        await engine.dispose()
    with sqlite3.connect(data / 'database.db') as connection:
        counts = tuple(connection.execute(query).fetchone()[0] for query in (
            'SELECT COUNT(*) FROM jobs', 'SELECT COUNT(*) FROM channels',
            "SELECT COUNT(*) FROM jobs WHERE status = 'queued'", 'SELECT COUNT(*) FROM job_assets'))
    if counts[:3] != (10000, 20, 1000):
        raise RuntimeError(f'Fixture counts did not match: {counts}')
    print(f'Fixture ready: publications={counts[0]}, channels={counts[1]}, queued={counts[2]}, album links={counts[3]}')
    print('Synthetic login: Telegram ID 987654321; password fixture-password-only. Queue is paused.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='New absolute directory for disposable fixture')
    asyncio.run(generate(parser.parse_args().root))
