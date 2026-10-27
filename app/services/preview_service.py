import shutil
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from uuid import uuid5,NAMESPACE_URL
import asyncio

from app.db.models import Job
from app.domain.enums import ContentKind, MediaType
from app.domain.exceptions import MediaValidationError
from app.domain.models import MediaItem, PreparedMedia, SourcePost
from app.services.caption_service import DEFAULT_TEMPLATE, CaptionService
from app.services.download_service import DownloadService
from app.services.media_service import MediaService
from app.services.job_service import JobService
from app.services.news_render_service import NewsRenderService
from app.services.publisher_service import TelegramPublisher
from app.services.translation_service import TranslationService
from app.utils.tags import publication_tags


def deserialize_post(data: dict) -> SourcePost:
    items = [MediaItem(**{**item, "media_type": MediaType(item.get("media_type", "image"))}) for item in data["media_items"]]
    return SourcePost(
        provider=data["provider"], source_id=data["source_id"], source_url=data["source_url"],
        normalized_url=data["normalized_url"], title=data["title"], description=data.get("description", ""),
        author_id=data.get("author_id"), author_name=data["author_name"], author_url=data["author_url"],
        source_tags=data.get("source_tags", []), media_items=items,
        content_kind=ContentKind(data.get("content_kind", "artwork")),
        body=data.get("body", ""),
        content_warning=data.get("content_warning"),
        published_at=datetime.fromisoformat(data["published_at"]) if data.get("published_at") else None,
        metadata=dict(data.get("metadata", {})),
    )


class PreviewService:
    def __init__(
        self, *, downloader: DownloadService, media: MediaService, captions: CaptionService,
        publisher: TelegramPublisher, storage: Path, auto_add_source_tags: bool,
        max_tags: int, max_tag_length: int, translator: TranslationService,
        news_renderer: NewsRenderService | None = None,
        jobs: JobService | None = None,
    ) -> None:
        self.downloader = downloader
        self.media = media
        self.captions = captions
        self.publisher = publisher
        self.translator = translator
        self.jobs = jobs
        self.news_renderer = news_renderer or NewsRenderService()
        self.storage = storage
        self.auto_add_source_tags = auto_add_source_tags
        self.max_tags = max_tags
        self.max_tag_length = max_tag_length

    async def send(self, job: Job, chat_id: int | str) -> None:
        if self.jobs is not None and getattr(self.jobs,"gate",None):
            return await self._managed_send(job,chat_id)
        post = deserialize_post(job.post_data)
        if post.content_kind == ContentKind.ARTWORK:
            if not post.media_items:
                raise MediaValidationError("Выберите хотя бы одно изображение в редакторе альбома.")
            await self._enrich_title(job, post)
        temporary_id = f"preview-{job.id}-{uuid4().hex}"
        try:
            prepared: list[PreparedMedia] = []
            for item in post.media_items:
                if item.telegram_file_id:
                    prepared.append(PreparedMedia(
                        path=None,
                        as_document=item.media_type == MediaType.DOCUMENT,
                        order=item.order,
                        media_type=item.media_type,
                        telegram_file_id=item.telegram_file_id,
                    ))
                    continue
                downloaded = await self.downloader.download(
                    temporary_id,
                    replace(item, url=item.preview_url) if item.preview_url else item,
                )
                prepared.append(await self.media.prepare(downloaded, job.channel.publish_mode))
            prepared.sort(key=lambda item: item.order)
            caption = self._build_caption(job, post)
            if post.content_kind == ContentKind.NEWS:
                await self.publisher.preview(chat_id, prepared, caption, post)
            else:
                await self.publisher.preview(chat_id, prepared, caption)
        finally:
            shutil.rmtree(self.storage / "jobs" / temporary_id, ignore_errors=True)

    async def caption(self, job: Job) -> str:
        if self.jobs is not None and getattr(self.jobs,"gate",None):
            platform=getattr(self.jobs,"platform",None)
            if platform is None:
                raise MediaValidationError("Управляемая панель ещё не готова; повторите после запуска runtime")
            return (await platform.preview(job.created_by_user_id,job.id))["html"]
        post = deserialize_post(job.post_data)
        if post.content_kind == ContentKind.ARTWORK:
            await self._enrich_title(job, post)
        return self._build_caption(job, post)

    async def _managed_send(self,job,chat_id):
        """Telegram is a client of the same persisted preparation and preview commands."""
        platform=getattr(self.jobs,"platform",None)
        if platform is None:
            raise MediaValidationError("Управляемая панель ещё не готова; повторите после запуска runtime")
        from app.bot.durable import current_update
        identity=current_update.get()
        def command_key(stage):
            return str(uuid5(NAMESPACE_URL,f"publisher:{identity}:{job.id}:{chat_id}:{stage}")) if identity else str(uuid4())
        dto=await platform.get_publication(job.created_by_user_id,job.id)
        if not dto["readiness"]["ready"]:
            if dto["status"]!="waiting_confirmation":
                raise MediaValidationError("Верните публикацию в черновик и подготовьте медиа в панели")
            prepared=await platform.publication_action(job.created_by_user_id,job.id,"prepare",{"expected_version":dto["version"]},command_key("prepare"))
            await self._await_operation(platform,job.created_by_user_id,prepared["operation_id"])
            dto=await platform.get_publication(job.created_by_user_id,job.id)
        if not hasattr(platform,"prepare"):
            raise MediaValidationError("Общий сервис предпросмотра ещё не настроен")
        operation=await platform.prepare(job.created_by_user_id,job.id,"preview-in-telegram",{"expected_version":dto["version"]},command_key("telegram-preview"))
        await self._await_operation(platform,job.created_by_user_id,operation["operation_id"])

    async def _await_operation(self,platform,actor_id,operation_id):
        deadline=asyncio.get_running_loop().time()+180
        while True:
            operation=await platform.operation(actor_id,operation_id)
            if operation["status"]=="succeeded":return operation
            if operation["status"] in {"failed","cancelled","waiting_user"}:
                raise MediaValidationError(operation.get("message") or "Операция требует проверки в панели")
            if asyncio.get_running_loop().time()>=deadline:
                raise MediaValidationError(f"Операция {operation_id} продолжает выполняться. Откройте панель: закрытие чата её не отменяет.")
            await asyncio.sleep(.25)

    async def _enrich_title(self, job: Job, post: SourcePost) -> None:
        await self.translator.enrich_title(post)
        if self.jobs is not None:
            await self.jobs.save_title_translation(job.id, post.title, post.metadata)

    def validate_custom_caption(self, value: str) -> None:
        self.captions.build_custom(value)

    def validate_custom_text(self, value: str, job: Job | None = None) -> None:
        if job is None:
            self.news_renderer.validate_custom(value)
            return
        post = deserialize_post(job.post_data)
        tags = self._tags(job)
        self.news_renderer.build_custom(value, post, tags)

    def _build_caption(self, job: Job, post: SourcePost) -> str:
        caption_tags = self._tags(job)
        caption_override = getattr(job, "caption_override", None)
        if caption_override is not None:
            if post.content_kind == ContentKind.NEWS:
                return self.news_renderer.build_custom(caption_override, post, caption_tags)
            return self.captions.build_custom(caption_override)
        if post.content_kind == ContentKind.NEWS:
            return self.news_renderer.build(post, caption_tags)
        return self.captions.build(post, caption_tags, job.channel.caption_template or DEFAULT_TEMPLATE)

    def _tags(self, job: Job) -> list[str]:
        return publication_tags(
            job.user_tags, job.source_tags, getattr(job.channel, "default_tags", None),
            include_source=self.auto_add_source_tags, max_tags=self.max_tags, max_length=self.max_tag_length,
        )
