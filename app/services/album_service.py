import re

from app.db.models import Job
from app.domain.album import read_album, select_album
from app.domain.enums import ContentKind, JobStatus
from app.domain.exceptions import InvalidMediaSelectionError
from app.services.job_service import JobService


class AlbumEditor:
    def __init__(self, jobs: JobService, *, limit: int | None = 10) -> None:
        self.jobs = jobs
        self.limit = limit

    async def get(self, job_id: int) -> Job:
        job = await self.jobs.get(job_id)
        if not job or job.content_kind != ContentKind.ARTWORK or job.status != JobStatus.WAITING_CONFIRMATION:
            raise InvalidMediaSelectionError("Редактор доступен только для черновика иллюстрации. Откройте актуальное задание.")
        return job

    async def change(
        self, job_id: int, revision: int, action: str, number: int = 0, *, order: str = "",
    ) -> Job:
        job = await self.get(job_id)
        album = read_album(job.post_data)
        limit=self.limit
        platform=getattr(self.jobs,'platform',None)
        if platform:
            settings=await platform.effective_settings()
            limit=settings['pixiv_max_images'] if settings['pixiv_media_limit_enabled'] else None
        if album.revision != revision:
            raise InvalidMediaSelectionError("Альбом уже изменён. Откройте редактор заново из черновика.")
        selected = list(album.selection)
        if action == "toggle":
            if number not in album.catalog:
                raise InvalidMediaSelectionError("Изображение не найдено.")
            if number in selected:
                selected.remove(number)
            else:
                selected.append(number)
        elif action in {"first", "up", "down"}:
            if number not in selected:
                raise InvalidMediaSelectionError("Сначала включите изображение в альбом.")
            index = selected.index(number)
            target = 0 if action == "first" else max(0, index - 1) if action == "up" else min(len(selected) - 1, index + 1)
            selected.insert(target, selected.pop(index))
        elif action == "clear":
            selected = []
        elif action == "restore":
            selected = album.initial
        elif action == "first10":
            selected = sorted(album.catalog)[:min(10, limit) if limit else 10]
        elif action == "order":
            if not re.fullmatch(r"\s*\d+(?:\s*,\s*\d+)*\s*", order) or len(order) > 6000:
                raise InvalidMediaSelectionError("Пришлите номера через запятую, например: 7,1,5,3. Список заменит текущий выбор.")
            selected = [int(n) for n in order.split(",")]
        else:
            raise InvalidMediaSelectionError("Неизвестное действие редактора.")
        if platform:
            catalogue=await platform.album(job.created_by_user_id,job.id)
            if catalogue['items']:
                by_number={asset['source_index']:asset['id'] for asset in catalogue['items']}
                if any(number not in by_number for number in selected):raise InvalidMediaSelectionError('Каталог изменился; обновите редактор')
                chosen=[by_number[number] for number in selected]
                await platform.put_album(job.created_by_user_id,job.id,{'expected_version':job.version,'selected_asset_ids':chosen,'asset_order':chosen})
                return await self.jobs.get(job.id)
        updated = await self.jobs.replace_draft_post(job, select_album(job.post_data, selected, limit=limit))
        if updated is None:
            raise InvalidMediaSelectionError("Черновик изменён другим действием. Откройте редактор заново.")
        return updated
