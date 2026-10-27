import asyncio
import logging
from dataclasses import dataclass, field
from html import escape

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from app.bot.menu import (
    ADD_BUTTON,
    CHANNELS_BUTTON,
    HEALTH_BUTTON,
    HELP_BUTTON,
    NEWS_BUTTON,
    PREVIEW_BUTTON,
    QUEUE_BUTTON,
    STATS_BUTTON,
)
from app.bot.states import CreateNews, EditNews
from app.domain.enums import ContentKind, JobStatus, MediaType
from app.domain.exceptions import ApplicationError
from app.domain.models import MediaItem
from app.news.classifier import classify_news_input
from app.news.models import NewsMediaKind, NewsSourceKind, NewsSourceRequest
from app.services.job_service import JobService
from app.services.news_submission_service import NewsSubmissionService, QueuedNews
from app.services.news_task_service import NewsTaskService
from app.services.preview_service import PreviewService, deserialize_post

logger = logging.getLogger(__name__)

_FORWARDED_ALBUM_DEBOUNCE_SECONDS = 0.4
_PENDING_FORWARDED_ALBUM_STATE_KEY = "news_pending_forwarded_album"


@dataclass(slots=True)
class _PendingForwardedAlbum:
    key: tuple[int, int, str]
    token: str
    messages: dict[int, Message] = field(default_factory=dict)
    state: FSMContext | None = None
    flush_task: asyncio.Task[None] | None = None


def news_preview_keyboard(
    job_id: int, media_count: int, *, queued: bool = False,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text="🚀 Опубликовать сейчас" if queued else "✅ Добавить в очередь",
            callback_data=f"preview_publish:{job_id}" if queued else f"publish:{job_id}",
        )],
    ]
    if not queued:
        rows.extend([[
            InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"news_text:{job_id}"),
            InlineKeyboardButton(
                text=f"🖼 Медиа · {media_count}", callback_data=f"news_media:{job_id}",
            ),
        ]])
        rows.append([InlineKeyboardButton(
            text="📡 Сменить канал", callback_data=f"news_channel:{job_id}",
        )])
    rows.extend([
        [InlineKeyboardButton(
            text="🕒 Изменить время" if queued else "🕒 Назначить время",
            callback_data=f"preview_schedule:{job_id}" if queued else f"schedule:{job_id}",
        )],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel:{job_id}")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def news_task_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отменить обработку", callback_data=f"news_task_cancel:{task_id}"),
    ]])


def news_source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ввести текст вручную", callback_data="news_manual_input")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="news_create_cancel")],
    ])


def news_media_keyboard(job_id: int, count: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👁 Показать", callback_data=f"news_media_show:{job_id}")],
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"news_media_add:{job_id}"),
            InlineKeyboardButton(text="♻️ Заменить всё", callback_data=f"news_media_replace:{job_id}"),
        ],
    ]
    rows.extend([
        [InlineKeyboardButton(
            text=f"🗑 Удалить #{index + 1}",
            callback_data=f"news_media_remove:{job_id}:{index}",
        )]
        for index in range(count)
    ])
    if count:
        rows.append([InlineKeyboardButton(
            text="🗑 Удалить всё", callback_data=f"news_media_clear:{job_id}",
        )])
    rows.append([InlineKeyboardButton(text="⬅️ К черновику", callback_data=f"news_draft:{job_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def news_media_input_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово", callback_data=f"news_media_done:{job_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"news_media_input_cancel:{job_id}")],
    ])


def build_news_router(
    *, submissions: NewsSubmissionService, tasks: NewsTaskService,
    jobs: JobService, previews: PreviewService,
) -> Router:
    router = Router(name="news")
    pending_forwarded_albums: dict[tuple[int, int, str], _PendingForwardedAlbum] = {}
    forwarded_album_tasks: set[asyncio.Task[None]] = set()
    router.forwarded_album_tasks = forwarded_album_tasks

    async def queue_request(
        message: Message, request: NewsSourceRequest, *, extra_payload: dict | None = None,
    ) -> QueuedNews | None:
        if message.from_user is None:
            return None
        try:
            queued = await submissions.create(
                telegram_user_id=message.from_user.id,
                username=message.from_user.username,
                display_name=message.from_user.full_name,
                origin_chat_id=message.chat.id,
                request=request,
                extra_payload=extra_payload,
            )
        except (ValueError, TypeError) as error:
            await message.answer(str(error))
            return None
        status = await message.answer(
            _task_status_text(
                queued.task.id,
                "Ожидаем домашний обработчик",
                queued.channel.alias,
            ),
            reply_markup=news_task_keyboard(queued.task.id),
        )
        await tasks.set_status_message(queued.task.id, status.message_id)
        return queued

    async def finish_wizard_album(
        pending: _PendingForwardedAlbum, *, success: bool,
    ) -> None:
        state = pending.state
        if state is None:
            return
        try:
            current_state = await state.get_state()
            data = await state.get_data()
            if (
                current_state != CreateNews.waiting_for_source.state
                or data.get(_PENDING_FORWARDED_ALBUM_STATE_KEY) != pending.token
            ):
                return
            if success:
                await state.clear()
                return
            data.pop(_PENDING_FORWARDED_ALBUM_STATE_KEY, None)
            await state.set_data(data)
        except Exception:
            logger.exception("news_forwarded_album_fsm_cleanup_failed key=%r", pending.key)

    async def wizard_album_is_active(pending: _PendingForwardedAlbum) -> bool:
        if pending.state is None:
            return True
        state = pending.state
        return (
            await state.get_state() == CreateNews.waiting_for_source.state
            and (await state.get_data()).get(_PENDING_FORWARDED_ALBUM_STATE_KEY)
            == pending.token
        )

    async def answer_album_error(message: Message, text: str) -> None:
        try:
            await message.answer(text)
        except Exception:
            logger.exception(
                "news_forwarded_album_error_notice_failed media_group_id=%s",
                message.media_group_id,
            )

    async def flush_forwarded_album(key: tuple[int, int, str]) -> None:
        await asyncio.sleep(_FORWARDED_ALBUM_DEBOUNCE_SECONDS)
        pending = pending_forwarded_albums.get(key)
        if pending is None or pending.flush_task is not asyncio.current_task():
            return
        pending_forwarded_albums.pop(key, None)
        messages = sorted(pending.messages.values(), key=lambda item: item.message_id)
        anchor = _forwarded_album_anchor(messages)
        try:
            if not await wizard_album_is_active(pending):
                return
            if not _message_text(anchor):
                await answer_album_error(
                    anchor, "В пересланном альбоме нет текста для создания новости.",
                )
                await finish_wizard_album(pending, success=False)
                return
            request, payload = _forwarded_album_request(messages)
            queued = await queue_request(anchor, request, extra_payload=payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("news_forwarded_album_flush_failed key=%r", key)
            await finish_wizard_album(pending, success=False)
            await answer_album_error(
                anchor,
                "Не удалось обработать пересланный альбом. Повторите отправку.",
            )
            return
        await finish_wizard_album(pending, success=queued is not None)

    def consume_album_task(task: asyncio.Task[None]) -> None:
        forwarded_album_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "news_forwarded_album_task_failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def collect_forwarded_album(
        message: Message, state: FSMContext | None = None,
    ) -> None:
        key = _forwarded_album_key(message)
        pending = pending_forwarded_albums.get(key)
        if pending is None:
            token = ":".join((str(key[0]), str(key[1]), key[2], str(message.message_id)))
            pending = _PendingForwardedAlbum(key=key, token=token, state=state)
            pending_forwarded_albums[key] = pending
            if state is not None:
                await state.update_data(**{
                    _PENDING_FORWARDED_ALBUM_STATE_KEY: token,
                })
        pending.messages[message.message_id] = message
        if pending.flush_task is not None and not pending.flush_task.done():
            pending.flush_task.cancel()
        task = asyncio.create_task(
            flush_forwarded_album(key),
            name=f"news-forwarded-album-{message.media_group_id}",
        )
        pending.flush_task = task
        forwarded_album_tasks.add(task)
        task.add_done_callback(consume_album_task)

    async def show_draft(message: Message, job_id: int, *, send_preview: bool = True) -> None:
        job = await jobs.get(job_id)
        if not _editable_news(job):
            await message.answer("Черновик уже недоступен для редактирования.")
            return
        post = deserialize_post(job.post_data)
        preview_error: str | None = None
        if send_preview:
            try:
                await previews.send(job, message.chat.id)
            except ApplicationError as error:
                logger.warning("news_draft_preview_failed job_id=%s", job.id, exc_info=True)
                preview_error = str(error)
        warnings = post.metadata.get("warnings") or []
        warning_text = (
            "\nПредупреждения: " + escape("; ".join(str(item) for item in warnings[:3]))
            if warnings else ""
        )
        preview_line = (
            "Предпросмотр публикации отправлен отдельным сообщением."
            if preview_error is None
            else f"Предпросмотр не удалось отправить: {escape(preview_error[:300])}"
        )
        await message.answer(
            f"📰 <b>Черновик новости #{job.id}</b>\n"
            f"Канал: <code>{escape(job.channel.alias)}</code>\n"
            f"Медиа: {len(post.media_items)}\n"
            f"{preview_line}{warning_text}",
            parse_mode="HTML",
            reply_markup=news_preview_keyboard(
                job.id,
                len(post.media_items),
                queued=job.status in {JobStatus.QUEUED, JobStatus.SCHEDULED},
            ),
        )

    @router.message(Command("news"))
    @router.message(F.text == NEWS_BUTTON)
    async def start_news(message: Message, state: FSMContext) -> None:
        await state.clear()
        if not getattr(submissions, "enabled", True):
            await message.answer(
                "Обработка новостей не настроена: задайте NEWS_WORKER_TOKEN на сервере."
            )
            return
        await state.set_state(CreateNews.waiting_for_source)
        await message.answer(
            "Отправьте ссылку на статью, YouTube-видео или публичный t.me-пост. "
            "Также можно переслать пост из Telegram или выбрать ручной ввод.",
            reply_markup=news_source_keyboard(),
        )

    @router.message(
        F.text.startswith("/")
        | F.text.in_({
            ADD_BUTTON,
            QUEUE_BUTTON,
            PREVIEW_BUTTON,
            STATS_BUTTON,
            CHANNELS_BUTTON,
            HEALTH_BUTTON,
            HELP_BUTTON,
        })
    )
    async def leave_news_for_global_navigation(message: Message, state: FSMContext) -> None:
        await state.clear()
        raise SkipHandler

    @router.callback_query(StateFilter(CreateNews.waiting_for_source), F.data == "news_manual_input")
    async def manual_input(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(CreateNews.waiting_for_manual_text)
        await callback.message.edit_text(
            "Отправьте текст новости. Можно сразу приложить одно фото, видео или документ "
            "и поместить текст в подпись; остальные медиа добавляются в редакторе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="news_create_cancel"),
            ]]),
        )
        await callback.answer()

    @router.callback_query(
        StateFilter(CreateNews.waiting_for_source, CreateNews.waiting_for_manual_text),
        F.data == "news_create_cancel",
    )
    async def cancel_create(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("Создание новости отменено.")
        await callback.answer()

    @router.message(CreateNews.waiting_for_source)
    async def receive_source(message: Message, state: FSMContext) -> None:
        text = (message.text or message.caption or "").strip()
        if message.forward_origin is not None:
            if message.media_group_id:
                await collect_forwarded_album(message, state)
                return
            if not text:
                await message.answer("В пересланном посте нет текста для создания новости.")
                return
            request, payload = _forwarded_request(message)
        elif not text:
            await message.answer("В источнике нет текста или ссылки.", reply_markup=news_source_keyboard())
            return
        else:
            try:
                request = classify_news_input(text)
            except ApplicationError as error:
                await message.answer(str(error), reply_markup=news_source_keyboard())
                return
            payload = {"media": _telegram_media_payload(message)} if _has_media(message) else None
        if await queue_request(message, request, extra_payload=payload):
            await state.clear()

    @router.message(CreateNews.waiting_for_manual_text)
    async def receive_manual(message: Message, state: FSMContext) -> None:
        text = (message.text or message.caption or "").strip()
        if not text:
            await message.answer("Добавьте к сообщению текст новости.")
            return
        payload = {"media": _telegram_media_payload(message)} if _has_media(message) else None
        request = NewsSourceRequest(NewsSourceKind.MANUAL, text)
        if await queue_request(message, request, extra_payload=payload):
            await state.clear()

    @router.message(StateFilter(None), F.forward_origin)
    async def receive_forwarded(message: Message) -> None:
        if message.media_group_id:
            await collect_forwarded_album(message)
            return
        if not (message.text or message.caption or "").strip():
            await message.answer("В пересланном посте нет текста для создания новости.")
            return
        request, payload = _forwarded_request(message)
        await queue_request(message, request, extra_payload=payload)

    @router.callback_query(F.data.startswith("news_task_cancel:"))
    async def cancel_task(callback: CallbackQuery) -> None:
        task_id = int(callback.data.rsplit(":", 1)[1])
        if await tasks.cancel(task_id):
            await callback.message.edit_text(f"Обработка новости #{task_id} отменена.")
            await callback.answer()
        else:
            await callback.answer("Задачу уже нельзя отменить.", show_alert=True)

    @router.callback_query(F.data.startswith("news_draft:"))
    async def reopen_draft(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        job_id = int(callback.data.rsplit(":", 1)[1])
        await show_draft(callback.message, job_id, send_preview=False)
        await callback.answer()

    @router.callback_query(F.data.startswith("news_text:"))
    async def edit_text(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        if not _editable_news(job):
            await callback.answer("Текст уже нельзя изменить.", show_alert=True)
            return
        await state.set_state(EditNews.waiting_for_text)
        await state.set_data({"news_job_id": job_id})
        await callback.message.answer(
            "Отправьте новый итоговый текст новости. Максимум 4096 символов. "
            "HTML будет экранирован; ссылка на источник и теги добавятся автоматически."
        )
        await callback.answer()

    @router.message(EditNews.waiting_for_text)
    async def receive_edited_text(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job_id = data.get("news_job_id")
        text = (message.text or "").strip()
        if not isinstance(job_id, int):
            await state.clear()
            await message.answer("Редактор устарел.")
            return
        job = await jobs.get(job_id)
        if not _editable_news(job):
            await state.clear()
            await message.answer("Текст уже нельзя изменить.")
            return
        try:
            previews.validate_custom_text(text, job)
        except ApplicationError as error:
            await message.answer(str(error))
            return
        if await jobs.set_caption_override(job_id, text) is None:
            await state.clear()
            await message.answer("Текст уже нельзя изменить.")
            return
        await state.clear()
        await show_draft(message, job_id)

    @router.callback_query(F.data.startswith("news_media:"))
    async def manage_media(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        if not _editable_news(job):
            await callback.answer("Медиа уже нельзя изменить.", show_alert=True)
            return
        count = len(job.post_data.get("media_items", []))
        await callback.message.answer(
            f"Медиа черновика: {count}/10", reply_markup=news_media_keyboard(job_id, count),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("news_media_show:"))
    async def show_media(callback: CallbackQuery) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        if not _editable_news(job):
            await callback.answer("Черновик недоступен.", show_alert=True)
            return
        if not job.post_data.get("media_items"):
            await callback.answer("В черновике нет медиа.", show_alert=True)
            return
        await callback.answer("Подготавливаю предпросмотр…")
        try:
            await previews.send(job, callback.message.chat.id)
        except ApplicationError as error:
            await callback.message.answer(f"Не удалось показать медиа: {error}")

    @router.callback_query(
        F.data.startswith("news_media_add:") | F.data.startswith("news_media_replace:")
    )
    async def start_media_input(callback: CallbackQuery, state: FSMContext) -> None:
        replace = callback.data.startswith("news_media_replace:")
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        if not _editable_news(job):
            await callback.answer("Медиа уже нельзя изменить.", show_alert=True)
            return
        await state.set_state(EditNews.waiting_for_media)
        await state.set_data({
            "news_job_id": job_id,
            "replace_media": replace,
            "replace_started": False,
        })
        await callback.message.answer(
            "Отправьте фото, видео или документ. Можно отправить несколько сообщений; "
            "после завершения нажмите «Готово». Старые медиа будут удалены только после "
            "успешного получения первого нового файла."
            if replace else
            "Отправьте фото, видео или документ. После завершения нажмите «Готово».",
            reply_markup=news_media_input_keyboard(job_id),
        )
        await callback.answer()

    @router.message(EditNews.waiting_for_media, F.photo | F.video | F.document | F.animation)
    async def receive_media(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job_id = data.get("news_job_id")
        if not isinstance(job_id, int):
            await state.clear()
            await message.answer("Редактор медиа устарел.")
            return
        item = _media_item(message)
        replace = bool(data.get("replace_media")) and not bool(data.get("replace_started"))
        try:
            updated = await jobs.add_media(job_id, item, replace=replace)
        except ValueError as error:
            await message.answer(str(error), reply_markup=news_media_input_keyboard(job_id))
            return
        if updated is None:
            await state.clear()
            await message.answer("Медиа уже нельзя изменить.")
            return
        await state.update_data(replace_started=True)
        count = len(updated.post_data.get("media_items", []))
        await message.answer(
            f"Медиа сохранено. Сейчас в черновике: {count}/10.",
            reply_markup=news_media_input_keyboard(job_id),
        )

    @router.message(EditNews.waiting_for_media)
    async def reject_non_media(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job_id = data.get("news_job_id")
        await message.answer(
            "Ожидаю фото, видео или документ.",
            reply_markup=news_media_input_keyboard(job_id) if isinstance(job_id, int) else None,
        )

    @router.callback_query(F.data.startswith("news_media_done:"))
    async def finish_media_input(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        data = await state.get_data()
        if (
            await state.get_state() != EditNews.waiting_for_media.state
            or data.get("news_job_id") != job_id
        ):
            await callback.answer("Эта кнопка редактора уже устарела.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text("Редактирование медиа завершено.")
        await show_draft(callback.message, job_id)
        await callback.answer()

    @router.callback_query(F.data.startswith("news_media_input_cancel:"))
    async def cancel_media_input(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        data = await state.get_data()
        if (
            await state.get_state() != EditNews.waiting_for_media.state
            or data.get("news_job_id") != job_id
        ):
            await callback.answer("Эта кнопка редактора уже устарела.", show_alert=True)
            return
        await state.clear()
        job = await jobs.get(job_id)
        count = len(job.post_data.get("media_items", [])) if job else 0
        await callback.message.edit_text(
            "Ввод медиа отменён.", reply_markup=news_media_keyboard(job_id, count),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("news_media_remove:"))
    async def remove_media(callback: CallbackQuery) -> None:
        _, raw_job_id, raw_index = callback.data.rsplit(":", 2)
        job_id, index = int(raw_job_id), int(raw_index)
        try:
            updated = await jobs.remove_media(job_id, index)
        except IndexError:
            updated = None
        if updated is None:
            await callback.answer("Медиа уже отсутствует или недоступно.", show_alert=True)
            return
        count = len(updated.post_data.get("media_items", []))
        await callback.message.edit_text(
            f"Медиа удалено. Осталось: {count}/10.",
            reply_markup=news_media_keyboard(job_id, count),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("news_media_clear:"))
    async def clear_media(callback: CallbackQuery) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        if await jobs.clear_media(job_id) is None:
            await callback.answer("Медиа уже нельзя изменить.", show_alert=True)
            return
        await callback.message.edit_text(
            "Все медиа удалены.", reply_markup=news_media_keyboard(job_id, 0),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("news_channel:"))
    async def choose_channel(callback: CallbackQuery) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        if not _editable_news(job) or job.status != JobStatus.WAITING_CONFIRMATION:
            await callback.answer("Канал уже нельзя изменить.", show_alert=True)
            return
        channels = [channel for channel in await jobs.channels() if channel.is_enabled]
        rows = [[InlineKeyboardButton(
            text=f"{'✓ ' if channel.id == job.target_channel_id else ''}{channel.alias} — {channel.title}"[:64],
            callback_data=f"news_channel_set:{job_id}:{channel.id}",
        )] for channel in channels]
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"news_draft:{job_id}")])
        await callback.message.answer(
            "Выберите канал:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("news_channel_set:"))
    async def set_channel(callback: CallbackQuery) -> None:
        _, raw_job_id, raw_channel_id = callback.data.rsplit(":", 2)
        job_id, channel_id = int(raw_job_id), int(raw_channel_id)
        channel = await jobs.change_channel(job_id, channel_id)
        if channel is None:
            await callback.answer("Канал уже нельзя изменить.", show_alert=True)
            return
        await callback.message.edit_text(f"Выбран канал: {escape(channel.alias)}")
        await show_draft(callback.message, job_id, send_preview=False)
        await callback.answer()

    return router


def _editable_news(job) -> bool:
    return bool(
        job
        and job.content_kind == ContentKind.NEWS
        and job.status == JobStatus.WAITING_CONFIRMATION
    )


def _task_status_text(task_id: int, stage: str, channel_alias: str) -> str:
    return (
        f"📰 Обработка новости #{task_id}\n"
        f"Канал: {channel_alias}\n"
        f"Текущий этап: {stage}"
    )


def _message_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _forwarded_album_key(message: Message) -> tuple[int, int, str]:
    if not message.media_group_id:
        raise ValueError("Сообщение не относится к Telegram-альбому")
    user_id = message.from_user.id if message.from_user is not None else 0
    return message.chat.id, user_id, message.media_group_id


def _forwarded_album_anchor(messages: list[Message]) -> Message:
    if not messages:
        raise ValueError("Пересланный Telegram-альбом пуст")
    return next((message for message in messages if _message_text(message)), messages[0])


def _forwarded_album_request(
    messages: list[Message],
) -> tuple[NewsSourceRequest, dict]:
    anchor = _forwarded_album_anchor(messages)
    request, payload = _forwarded_request(anchor)
    media: list[dict] = []
    seen_media: set[str] = set()
    for message in sorted(messages, key=lambda item: item.message_id):
        for item in _telegram_media_payload(message):
            unique_id = str(
                item.get("metadata", {}).get("telegram_file_unique_id")
                or item.get("telegram_file_id")
                or ""
            )
            if unique_id and unique_id in seen_media:
                continue
            if unique_id:
                seen_media.add(unique_id)
            media.append(item)

    text = _message_text(anchor)
    telegram = dict(payload["telegram"])
    telegram.update({
        "text": text,
        "media": media,
        "has_protected_content": any(
            bool(message.has_protected_content) for message in messages
        ),
    })
    payload.update({"source_text": text, "telegram": telegram, "media": media})
    return request, payload


def _forwarded_request(message: Message) -> tuple[NewsSourceRequest, dict]:
    text = _message_text(message)
    origin = message.forward_origin
    telegram: dict = {
        "text": text,
        "has_protected_content": bool(message.has_protected_content),
        "media": _telegram_media_payload(message),
    }
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        telegram.update({
            "chat_id": str(chat.id),
            "message_id": origin.message_id,
            "chat_title": chat.title,
            "channel_title": chat.title,
            "author_name": origin.author_signature or chat.title,
            "published_at": origin.date.isoformat(),
        })
        if chat.username:
            telegram["source_url"] = f"https://t.me/{chat.username}/{origin.message_id}"
            telegram["author_url"] = f"https://t.me/{chat.username}"
    elif isinstance(origin, MessageOriginChat):
        telegram.update({
            "chat_id": str(origin.sender_chat.id),
            "chat_title": origin.sender_chat.title,
            "author_name": origin.author_signature or origin.sender_chat.title,
            "published_at": origin.date.isoformat(),
        })
    elif isinstance(origin, MessageOriginUser):
        telegram.update({
            "author_name": origin.sender_user.full_name,
            "published_at": origin.date.isoformat(),
        })
    elif isinstance(origin, MessageOriginHiddenUser):
        telegram.update({
            "author_name": origin.sender_user_name,
            "published_at": origin.date.isoformat(),
        })
    request = NewsSourceRequest(NewsSourceKind.TELEGRAM, telegram.get("source_url") or text or "telegram")
    return request, {"source_text": text, "telegram": telegram, "media": telegram["media"]}


def _has_media(message: Message) -> bool:
    return bool(message.photo or message.video or message.document or message.animation)


def _telegram_media_payload(message: Message) -> list[dict]:
    return [_news_media_dict(_media_item(message))] if _has_media(message) else []


def _news_media_dict(item: MediaItem) -> dict:
    kind = {
        MediaType.IMAGE: NewsMediaKind.IMAGE,
        MediaType.VIDEO: NewsMediaKind.VIDEO,
        MediaType.ANIMATION: NewsMediaKind.ANIMATION,
        MediaType.DOCUMENT: NewsMediaKind.DOCUMENT,
    }[item.media_type]
    return {
        "kind": kind.value,
        "telegram_file_id": item.telegram_file_id,
        "filename": item.filename,
        "mime_type": item.mime_type,
        "width": item.width,
        "height": item.height,
        "metadata": {"telegram_file_unique_id": item.telegram_file_unique_id},
    }


def _media_item(message: Message) -> MediaItem:
    if message.photo:
        media = message.photo[-1]
        return MediaItem(
            url=f"telegram-media:{media.file_unique_id}",
            filename=f"photo-{media.file_unique_id}.jpg",
            order=0,
            mime_type="image/jpeg",
            media_type=MediaType.IMAGE,
            width=media.width,
            height=media.height,
            size=media.file_size,
            telegram_file_id=media.file_id,
            telegram_file_unique_id=media.file_unique_id,
        )
    if message.video:
        media = message.video
        return MediaItem(
            url=f"telegram-media:{media.file_unique_id}",
            filename=media.file_name or f"video-{media.file_unique_id}.mp4",
            order=0,
            mime_type=media.mime_type or "video/mp4",
            media_type=MediaType.VIDEO,
            width=media.width,
            height=media.height,
            size=media.file_size,
            telegram_file_id=media.file_id,
            telegram_file_unique_id=media.file_unique_id,
        )
    if message.animation:
        media = message.animation
        return MediaItem(
            url=f"telegram-media:{media.file_unique_id}",
            filename=media.file_name or f"animation-{media.file_unique_id}.mp4",
            order=0,
            mime_type=media.mime_type or "video/mp4",
            media_type=MediaType.ANIMATION,
            width=media.width,
            height=media.height,
            size=media.file_size,
            telegram_file_id=media.file_id,
            telegram_file_unique_id=media.file_unique_id,
        )
    if message.document:
        media = message.document
        return MediaItem(
            url=f"telegram-media:{media.file_unique_id}",
            filename=media.file_name or f"document-{media.file_unique_id}",
            order=0,
            mime_type=media.mime_type,
            media_type=MediaType.DOCUMENT,
            size=media.file_size,
            telegram_file_id=media.file_id,
            telegram_file_unique_id=media.file_unique_id,
        )
    raise ValueError("Сообщение не содержит поддерживаемого медиа")
