import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from sqlalchemy import select

from app.bot.menu import configure_bot_ui
from app.bot.durable import DurableDispatcher, PersistentStorage
from app.bot.album_ui import build_album_router
from app.bot.middleware import AdminOnlyMiddleware
from app.bot.model_ui import build_model_router
from app.bot.platform_models_ui import build_platform_model_router
from app.bot.news_notifications import NewsTaskNotifier
from app.bot.news_ui import build_news_router
from app.bot.router import build_router
from app.config import Settings
from app.db.models import Channel,ApplicationState
from app.db.session import create_database, create_schema
from app.news.api import NewsApiServer
from app.news.http import PublicOnlyResolver
from app.news.worker_config import HomeWorkerSettings
from app.providers.deviantart import DeviantArtProvider
from app.providers.direct_image import DirectImageProvider
from app.providers.pixiv import PixivProvider
from app.providers.registry import ProviderRegistry
from app.runtime import supervise
from app.queue.worker import WorkerPool
from app.services.caption_service import CaptionService
from app.services.album_service import AlbumEditor
from app.services.album_thumbnails import AlbumThumbnails
from app.services.channel_stats_service import ChannelStatsService
from app.services.download_service import DownloadService
from app.services.health_service import HealthService
from app.services.ingest_service import IngestService
from app.services.job_service import JobService
from app.services.media_service import MediaService
from app.services.news_render_service import NewsRenderService
from app.services.news_submission_service import NewsSubmissionService
from app.services.news_task_service import NewsTaskService
from app.services.preview_service import PreviewService
from app.services.publisher_service import TelegramPublisher
from app.services.translation_service import TranslationService
from app.services.title_client import SelectedTitleClient
from app.services.model_selection import LMStudioModels, ModelSelection


@dataclass
class Application:
    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    http: aiohttp.ClientSession
    engine: object
    workers: WorkerPool
    channel_stats: ChannelStatsService
    news_api: NewsApiServer | None = None
    local_http: aiohttp.ClientSession | None = None
    sessions: object = None
    job_service: object = None
    ingest: object = None
    news_tasks: object = None
    publisher: object = None
    previews: object = None

    async def run(self, *, news_worker: Callable[[], Awaitable[None]] | None = None) -> None:
        try:
            if self.news_api is not None:
                await self.news_api.start()
            await configure_bot_ui(self.bot)
            await self.workers.start()
            await self.channel_stats.start()
            polling = self.dispatcher.start_polling(
                self.bot, allowed_updates=self.dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
            if news_worker is None:
                await polling
            else:
                await supervise(polling, news_worker())
        finally:
            if self.news_api is not None:
                await self.news_api.stop()
            await self.channel_stats.stop()
            await self.workers.stop()
            await self.http.close()
            if self.local_http is not None:
                await self.local_http.close()
            await self.bot.session.close()
            await self.engine.dispose()


async def bootstrap(
    settings: Settings | None = None, *, home_worker_settings: HomeWorkerSettings | None = None, gate=None,
) -> Application:
    import os
    if os.environ.get("PUBLISHER_CONTROL_DIR") and gate is None:
        raise RuntimeError("Управляемая установка требует проверенный publication gate. Используйте python -m app.web.")
    async with AsyncExitStack() as cleanup:
        settings = settings or Settings()
        settings.storage_path.mkdir(parents=True, exist_ok=True)
        engine, sessions = create_database(settings.database_url)
        cleanup.push_async_callback(engine.dispose)
        await create_schema(engine)
        await _sync_channels(sessions, settings,system_id=gate.identity()["system_id"] if gate else None)
        timeout = aiohttp.ClientTimeout(total=settings.download_timeout)
        connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
        proxy_url = settings.proxy_url
        default_proxy = proxy_url
        if proxy_url and proxy_url.lower().startswith(("socks4://", "socks5://")):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError as error:
                raise RuntimeError("Для SOCKS-прокси установите зависимости: pip install -e '.[proxy]'") from error
            await connector.close()
            connector = ProxyConnector.from_url(proxy_url)
            default_proxy = None
        elif proxy_url:
            # The connector resolves the configured proxy rather than the final URL.
            # User-supplied destinations still receive the explicit DNS preflight in
            # DownloadService; direct connections additionally close the rebinding gap.
            await connector.close()
            connector = None
        http = aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": "Edizora/0.1"},
            connector=connector, proxy=default_proxy,
        )
        cleanup.push_async_callback(http.close)
        bot_session = AiohttpSession(proxy=proxy_url) if proxy_url else None
        if bot_session is not None:
            cleanup.push_async_callback(bot_session.close)
        bot = Bot(settings.bot_token, session=bot_session)
        cleanup.push_async_callback(bot.session.close)
        registry = ProviderRegistry([
            PixivProvider(http, settings.pixiv_cookies),
            DeviantArtProvider(http),
            DirectImageProvider(http, settings.max_download_bytes),
        ])
        jobs = JobService(sessions, gate=gate)
        ingest = IngestService(
            registry, settings.max_tags, settings.max_tag_length, settings.max_urls_per_message,
            media_limit_enabled=settings.pixiv_media_limit_enabled,
            max_images=settings.pixiv_max_images,
        )
        downloader = DownloadService(http, settings.storage_path, settings.max_download_bytes)
        media = MediaService()
        captions = CaptionService()
        news_renderer = NewsRenderService()
        publisher = TelegramPublisher(bot, sessions=sessions, gate=gate)
        # Also needed by /models when title translation is disabled or no model
        # has been selected yet. Local requests never use the public proxy.
        local_http = aiohttp.ClientSession(trust_env=False)
        cleanup.push_async_callback(local_http.close)
        worker_settings = home_worker_settings or HomeWorkerSettings(token="local-model-selection")
        model_selection = ModelSelection(settings.storage_path, worker_settings)
        local_news = home_worker_settings is not None and worker_settings.llm_provider == "lmstudio"
        title_client = None
        if settings.auto_translate_titles:
            title_client = SelectedTitleClient(
                local_http, model_selection, timeout=settings.translation_lmstudio_timeout,
            )
        translator = TranslationService(
            http, enabled=settings.auto_translate_titles, timeout=settings.translation_timeout,
            local_client=title_client, local_timeout=settings.translation_lmstudio_timeout,
            fallback_mymemory=settings.translation_fallback_mymemory,
        )
        previews = PreviewService(
            downloader=downloader, media=media, captions=captions, publisher=publisher,
            storage=settings.storage_path, auto_add_source_tags=settings.auto_add_source_tags,
            max_tags=settings.max_tags, max_tag_length=settings.max_tag_length, translator=translator,
            news_renderer=news_renderer, jobs=jobs,
        )
        wakeup = asyncio.Event()
        workers = WorkerPool(
            bot=bot, sessions=sessions, jobs=jobs,
            downloader=downloader, media=media, captions=captions, publisher=publisher,
            count=settings.worker_count, wakeup=wakeup,
            delete_after_publish=settings.delete_files_after_publish, storage=settings.storage_path,
            auto_add_source_tags=settings.auto_add_source_tags,
            max_tags=settings.max_tags, max_tag_length=settings.max_tag_length, translator=translator,
            news_renderer=news_renderer,
        )
        health = HealthService(
            bot=bot, sessions=sessions, workers=workers, storage=settings.storage_path,
            registry=registry, database_url=settings.database_url,
        )
        channel_stats = ChannelStatsService(
            bot=bot, sessions=sessions, admin_ids=settings.admin_ids,
            timezone_name=settings.timezone,
        )
        news_tasks = NewsTaskService(
            sessions, gate=gate,
            lease_extension_seconds=settings.news_task_lease_seconds,
            news_renderer=news_renderer,
            auto_add_source_tags=settings.auto_add_source_tags,
            max_tags=settings.max_tags,
            max_tag_length=settings.max_tag_length,
        )
        news_submissions = NewsSubmissionService(
            jobs=jobs,
            tasks=news_tasks,
            default_channel_alias=settings.default_channel_alias,
            model_name=settings.news_model_name,
            model_resolver=(lambda: model_selection.selected) if local_news else None,
            max_attempts=settings.max_job_attempts,
            enabled=settings.news_api_enabled,
        )
        news_notifier = NewsTaskNotifier(bot=bot, jobs=jobs, previews=previews)
        news_api = (
            NewsApiServer(
                tasks=news_tasks,
                token=settings.news_worker_token or "",
                host=settings.news_api_bind_host or settings.news_api_host,
                port=settings.news_api_port,
                default_lease_seconds=settings.news_task_lease_seconds,
                on_progress=news_notifier.progress,
                on_complete=news_notifier.complete,
                on_failure=news_notifier.failure,
                use_worker_model=local_news,
            )
            if settings.news_api_enabled else None
        )
        dispatcher = DurableDispatcher(sessions=sessions, gate=gate, storage=PersistentStorage(sessions), events_isolation=SimpleEventIsolation()) if gate else Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        middleware = AdminOnlyMiddleware(settings.admin_ids)
        news_router = build_news_router(
            submissions=news_submissions, tasks=news_tasks, jobs=jobs, previews=previews,
        )
        album_router = build_album_router(
            editor=AlbumEditor(jobs, limit=settings.pixiv_max_images if settings.pixiv_media_limit_enabled else None),
            thumbnails=AlbumThumbnails(DownloadService(http, settings.storage_path, 5 * 1024 * 1024)),
            previews=previews,
        )
        model_router = (
            build_platform_model_router(jobs) if gate is not None
            else build_model_router(LMStudioModels(local_http, model_selection), local_news=local_news)
        )
        router = build_router(
            ingest, jobs, previews, translator, health, channel_stats,
            wakeup, registry, settings,
            news_submissions=news_submissions, news_tasks=news_tasks,
        )
        news_router.message.outer_middleware(middleware)
        news_router.callback_query.outer_middleware(middleware)
        album_router.message.outer_middleware(middleware)
        album_router.callback_query.outer_middleware(middleware)
        model_router.message.outer_middleware(middleware)
        model_router.callback_query.outer_middleware(middleware)
        router.message.outer_middleware(middleware)
        router.callback_query.outer_middleware(middleware)
        dispatcher.include_router(model_router)
        dispatcher.include_router(news_router)
        dispatcher.include_router(album_router)
        dispatcher.include_router(router)
        application = Application(
            settings, bot, dispatcher, http, engine, workers, channel_stats, news_api, local_http,
            sessions=sessions,job_service=jobs,ingest=ingest,news_tasks=news_tasks,publisher=publisher,previews=previews,
        )
        cleanup.pop_all()
        return application


async def _sync_channels(sessions, settings: Settings, *, system_id=None) -> None:
    async with sessions() as session, session.begin():
        state=await session.get(ApplicationState,1)
        if state and state.settings.get("config_seeded"):
            return
        if state is None:
            from uuid import uuid4
            state=ApplicationState(id=1,system_id=system_id or str(uuid4()),version=1,settings={},publication_blocks={},maintenance_state="normal")
            session.add(state)
        for alias, values in settings.channels_json.items():
            channel = await session.scalar(select(Channel).where(Channel.alias == alias))
            is_new = channel is None
            if channel is None:
                channel = Channel(
                    alias=alias,
                    telegram_chat_id=str(values["chat_id"]),
                    title=values.get("title", alias),
                    is_default=alias == settings.default_channel_alias,
                )
                session.add(channel)
            if not is_new:
                continue  # SQLite owns mutable settings after the first explicit import.
            channel.telegram_chat_id = str(values["chat_id"])
            channel.title = values.get("title", alias)
            channel.publish_mode = values.get("publish_mode", "auto")
            if is_new and "publish_interval_seconds" in values:
                channel.publish_interval_seconds = int(values["publish_interval_seconds"])
            channel.caption_template = values.get("caption_template")
            if "tags" in values or is_new:
                channel.default_tags = values.get("tags", [])
            channel.is_enabled = values.get("enabled", True)
        await session.flush()
        default_channel = await session.scalar(select(Channel).where(Channel.is_default.is_(True)))
        if default_channel is None:
            configured_default = await session.scalar(
                select(Channel).where(Channel.alias == settings.default_channel_alias)
            )
            if configured_default is not None:
                configured_default.is_default = True
        state.settings={**state.settings,"config_seeded":True}
        state.version+=1
