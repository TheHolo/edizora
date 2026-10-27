import asyncio
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.menu import (
    ADD_BUTTON,
    BACK_CALLBACK,
    CHANNEL_CALLBACK_PREFIX,
    CHANNEL_PAGE_CALLBACK_PREFIX,
    CHANNELS_BUTTON,
    CHANNELS_PER_PAGE,
    HEALTH_BUTTON,
    HEALTH_CALLBACK_PREFIX,
    HELP_BUTTON,
    MAIN_MENU_TEXT,
    PREVIEW_BUTTON,
    PREVIEW_CALLBACK_PREFIX,
    PREVIEW_PAGE_CALLBACK_PREFIX,
    QUEUE_BUTTON,
    QUEUE_CALLBACK_PREFIX,
    QUEUE_PAGE_CALLBACK_PREFIX,
    STATS_BUTTON,
    back_menu_keyboard,
    channels_menu_keyboard,
    health_menu_keyboard,
    main_menu_keyboard,
    paginate_channels,
    pagination_row,
    preview_menu_keyboard,
    queue_menu_keyboard,
    render_help,
)
from app.bot.news_ui import news_preview_keyboard, news_task_keyboard
from app.bot.states import CreatePublication, EditPreview, ManageChannel, ManageQueue
from app.db.models import Channel, Job
from app.domain.album import read_album
from app.domain.enums import ACTIVE_JOB_STATUSES, ContentKind, JobStatus
from app.domain.exceptions import ApplicationError, UnsupportedSourceError
from app.domain.models import SourcePost
from app.news.classifier import classify_news_input
from app.news.models import NewsSourceRequest
from app.services.channel_stats_service import (
    ChannelStatsService,
    ChannelSubscriberStats,
    SubscriberChange,
)
from app.services.health_service import HealthService, render_health_report
from app.services.ingest_service import IngestService
from app.services.job_service import QUEUE_FILTER_STATUSES, JobService, serialize_post
from app.services.news_submission_service import NewsSubmissionService
from app.services.news_task_service import NewsTaskService
from app.services.preview_service import PreviewService, deserialize_post
from app.services.translation_service import TranslationService
from app.utils.datetime_input import format_schedule_datetime, parse_schedule_datetime
from app.utils.durations import format_duration, parse_duration
from app.utils.input import split_source_selection
from app.utils.queue_schedule import (
    estimate_queue_schedule,
    format_countdown,
    next_queued_by_schedule,
    regular_queue_completion,
)
from app.utils.tags import hashtags, merge_tags, normalize_tags, publication_tags
from app.utils.text import provider_label, telegram_html_length

logger = logging.getLogger(__name__)


def format_subscriber_change(change: SubscriberChange | None) -> str:
    if change is None:
        return "—"
    value = f"{change.value:+,}".replace(",", " ")
    if change.percent is None:
        return value
    percent = f"{change.percent:+.2f}".replace(".", ",")
    return f"{value} ({percent}%)"


def render_channel_subscriber_stats(stats: ChannelSubscriberStats) -> str:
    if stats.count is None:
        lines = ["Подписчики: —"]
    else:
        count = f"{stats.count:,}".replace(",", " ")
        lines = [
            f"Подписчики: <b>{count}</b>",
            (
                "Изменение: "
                f"24ч {format_subscriber_change(stats.day)} · "
                f"7д {format_subscriber_change(stats.week)} · "
                f"30д {format_subscriber_change(stats.month)}"
            ),
        ]
    if stats.error:
        lines.append(f"Обновление подписчиков: ⚠️ {escape(stats.error)}")
    return "\n".join(lines)


def preview_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать", callback_data=f"publish:{job_id}")],
        [InlineKeyboardButton(text="Изменить теги", callback_data=f"tags:{job_id}"),
         InlineKeyboardButton(text="Сменить канал", callback_data=f"channel:{job_id}")],
        [InlineKeyboardButton(text="Изменить заголовок", callback_data=f"title:{job_id}"),
         InlineKeyboardButton(text="Изменить описание", callback_data=f"description:{job_id}")],
        [InlineKeyboardButton(text="Изменить подпись", callback_data=f"caption:{job_id}")],
        [InlineKeyboardButton(
            text="🕒 Назначить время", callback_data=f"schedule:{job_id}",
        )],
        [InlineKeyboardButton(text="🗂 Редактор альбома", callback_data=f"album_open:{job_id}")],
        [InlineKeyboardButton(text="Показать медиа", callback_data=f"media:{job_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{job_id}")],
    ])


def duplicate_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать повторно", callback_data=f"repeat:{job_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"cancel:{job_id}")],
    ])


def queued_preview_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опубликовать сейчас", callback_data=f"preview_publish:{job_id}")],
        [InlineKeyboardButton(text="Заменить теги", callback_data=f"preview_tags_replace:{job_id}"),
         InlineKeyboardButton(text="Добавить теги", callback_data=f"preview_tags_add:{job_id}")],
        [InlineKeyboardButton(text="Изменить подпись", callback_data=f"preview_caption:{job_id}")],
        [InlineKeyboardButton(
            text="🕒 Назначить / изменить время",
            callback_data=f"preview_schedule:{job_id}",
        )],
        [InlineKeyboardButton(text="Отменить публикацию", callback_data=f"preview_cancel:{job_id}")],
    ])


def cancel_tag_input_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена ввода", callback_data=f"tags_input_cancel:{job_id}")],
    ])


def caption_input_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Использовать автоподпись", callback_data=f"caption_auto:{job_id}",
        )],
        [InlineKeyboardButton(
            text="Отмена ввода", callback_data=f"caption_input_cancel:{job_id}",
        )],
    ])


def post_field_input_keyboard(job_id: int, field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Отмена ввода", callback_data=f"post_field_cancel:{field}:{job_id}",
        )],
    ])


def preview_schedule_input_keyboard(job_id: int, context: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Отмена ввода",
            callback_data=f"preview_schedule_cancel:{context}:{job_id}",
        )],
    ])


def channel_details_keyboard(channel: Channel, page: int) -> InlineKeyboardMarkup:
    pause_text = "▶️ Возобновить" if channel.is_paused else "⏸ Приостановить"
    rows = [
        [InlineKeyboardButton(
            text=pause_text, callback_data=f"channel_pause:{page}:{channel.id}",
        )],
        [InlineKeyboardButton(
            text="⏱ Изменить интервал",
            callback_data=f"channel_interval_edit:{page}:{channel.id}",
        )],
        [InlineKeyboardButton(
            text="⭐ Сделать основным",
            callback_data=f"channel_default:{page}:{channel.id}",
        )],
        [InlineKeyboardButton(
            text="🚀 Опубликовать следующий",
            callback_data=f"channel_publish:{page}:{channel.id}",
        )],
        [InlineKeyboardButton(
            text="🔄 Обновить", callback_data=f"channel_refresh:{page}:{channel.id}",
        )],
        [InlineKeyboardButton(text="⬅️ К списку каналов", callback_data=f"channel_back:{page}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_interval_input_keyboard(channel_id: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Отмена", callback_data=f"channel_interval_cancel:{page}:{channel_id}",
        )],
    ])


QUEUE_FILTER_LABELS = {
    "active": "Активные",
    "queued": "Ожидают",
    "scheduled": "Запланировано",
    "processing": "В работе",
    "failed": "Ошибки",
    "completed": "Готово",
    "cancelled": "Отменено",
}

JOB_STATUS_LABELS = {
    JobStatus.WAITING_CONFIRMATION: "Подтверждение",
    JobStatus.QUEUED: "Ожидает",
    JobStatus.SCHEDULED: "Запланировано",
    JobStatus.DOWNLOADING: "Загрузка",
    JobStatus.PROCESSING: "Обработка",
    JobStatus.PUBLISHING: "Публикация",
    JobStatus.COMPLETED: "Опубликовано",
    JobStatus.FAILED: "Ошибка",
    JobStatus.CANCELLED: "Отменено",
}


def job_status_label(status: str) -> str:
    try:
        return JOB_STATUS_LABELS.get(JobStatus(status), status)
    except ValueError:
        return status


def queue_view_keyboard(
    rows: list[Job], *, page: int, scope: int, status_filter: str,
) -> InlineKeyboardMarkup:
    filter_buttons = [
        InlineKeyboardButton(
            text=f"{'✓ ' if key == status_filter else ''}{label}",
            callback_data=f"queue_filter:{page}:{scope}:{key}",
        )
        for key, label in QUEUE_FILTER_LABELS.items()
    ]
    buttons = [filter_buttons[:3], filter_buttons[3:]]
    buttons.extend([
        [InlineKeyboardButton(
            text=f"#{job.id} · {job_status_label(job.status)} · {job.channel.alias}"[:64],
            callback_data=f"queue_job:{page}:{scope}:{status_filter}:{job.id}",
        )]
        for job in rows[:15]
    ])
    if rows and status_filter not in {"completed", "cancelled"}:
        buttons.append([InlineKeyboardButton(
            text="🗑 Отменить выбранные",
            callback_data=f"queue_bulk_prompt:cancel:{page}:{scope}:{status_filter}",
        )])
    if status_filter == "failed":
        buttons.append([InlineKeyboardButton(
            text="🔁 Повторить все неудачные",
            callback_data=f"queue_bulk_prompt:retry:{page}:{scope}:{status_filter}",
        )])
    if scope:
        buttons.append([InlineKeyboardButton(
            text="🔀 Перемешать ожидающие",
            callback_data=f"queue_shuffle:{page}:{scope}:{status_filter}",
        )])
    buttons.extend([
        [InlineKeyboardButton(
            text="🔄 Обновить", callback_data=f"queue_filter:{page}:{scope}:{status_filter}",
        )],
        [InlineKeyboardButton(text="⬅️ К выбору канала", callback_data=f"queue_back:{page}")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def queue_job_keyboard(
    job: Job, *, page: int, scope: int, status_filter: str,
) -> InlineKeyboardMarkup:
    context = f"{page}:{scope}:{status_filter}:{job.id}"
    rows: list[list[InlineKeyboardButton]] = []
    if job.status in {JobStatus.QUEUED, JobStatus.SCHEDULED}:
        if job.status == JobStatus.QUEUED:
            rows.append(
            [
                InlineKeyboardButton(text="⬆️ Выше", callback_data=f"queue_move:{context}:up"),
                InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"queue_move:{context}:down"),
            ]
            )
        rows.append([InlineKeyboardButton(
            text=(
                "🕒 Изменить точное время"
                if job.status == JobStatus.SCHEDULED
                else "🕒 Назначить точное время"
            ),
            callback_data=f"queue_schedule:{context}",
        )])
        if job.scheduled_at is not None:
            rows.append([InlineKeyboardButton(
                text="Сбросить точное время", callback_data=f"queue_schedule_clear:{context}",
            )])
        rows.extend([
            [InlineKeyboardButton(
                text="🚀 Опубликовать сейчас", callback_data=f"queue_force:{context}",
            )],
            [InlineKeyboardButton(
                text="🖼 Показать медиа", callback_data=f"queue_job_preview:{context}",
            )],
        ])
    if job.status == JobStatus.FAILED:
        rows.append([InlineKeyboardButton(
            text="🔁 Повторить", callback_data=f"queue_retry:{context}",
        )])
    if job.status not in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
        rows.append([InlineKeyboardButton(
            text="🗑 Отменить", callback_data=f"queue_cancel_job:{context}",
        )])
    rows.append([InlineKeyboardButton(
        text="⬅️ К очереди", callback_data=f"queue_filter:{page}:{scope}:{status_filter}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def schedule_input_keyboard(
    job_id: int, *, page: int, scope: int, status_filter: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Отмена",
            callback_data=f"queue_schedule_cancel:{page}:{scope}:{status_filter}:{job_id}",
        )],
    ])


def bulk_confirmation_keyboard(
    action: str, *, page: int, scope: int, status_filter: str,
) -> InlineKeyboardMarkup:
    context = f"{action}:{page}:{scope}:{status_filter}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Подтвердить", callback_data=f"queue_bulk_confirm:{context}",
        )],
        [InlineKeyboardButton(
            text="Отмена", callback_data=f"queue_filter:{page}:{scope}:{status_filter}",
        )],
    ])


def wizard_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить создание", callback_data="wizard_cancel")],
    ])


def wizard_channel_keyboard(channels, page: int = 0) -> InlineKeyboardMarkup:
    visible, page, page_count = paginate_channels(
        [channel for channel in channels if channel.is_enabled], page,
    )
    rows = [[InlineKeyboardButton(
        text=f"{channel.alias} — {channel.title}"[:64],
        callback_data=f"wizard_channel:{page}:{channel.id}",
    )] for channel in visible]
    navigation = pagination_row(page, page_count, "wizard_channel_page:")
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Отменить создание", callback_data="wizard_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_selection_keyboard(
    job_id: int, channels, current_channel_id: int, page: int = 0,
) -> InlineKeyboardMarkup:
    visible, page, page_count = paginate_channels(channels, page)
    rows = [
        [InlineKeyboardButton(
            text=f"{'✓ ' if channel.id == current_channel_id else ''}{channel.alias} — {channel.title}"[:64],
            callback_data=f"channel_select:{job_id}:{channel.id}",
        )]
        for channel in visible
    ]
    navigation = pagination_row(page, page_count, f"channel_select_page:{job_id}:")
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"channel_select_cancel:{job_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def replace_channel_line(text: str, alias: str) -> str:
    lines = text.splitlines()
    return "\n".join(f"Канал: {alias}" if line.startswith("Канал: ") else line for line in lines)


def queue_summary_line(post_count: int, completion: datetime, now: datetime) -> str:
    return f"Всего постов: {post_count} · Вся очередь: {format_countdown(completion, now)}"


def queue_total_line(rows: list[Job], channel: Channel, now: datetime) -> str:
    count, completion = regular_queue_completion(rows, now)
    if count == 0 or completion is None:
        return "Всего ожидают: 0 · Вся очередь: —"
    if channel.is_paused:
        return f"Всего ожидают: {count} · Вся очередь: после возобновления"
    return queue_summary_line(count, completion, now)


def build_router(
    ingest: IngestService, jobs: JobService, previews: PreviewService,
    translator: TranslationService, health: HealthService,
    channel_stats: ChannelStatsService, wakeup, registry, settings,
    *, news_submissions: NewsSubmissionService | None = None,
    news_tasks: NewsTaskService | None = None,
) -> Router:
    router = Router()

    def effective_tags(user_tags: list[str], source_tags: list[str], channel: Channel) -> list[str]:
        return publication_tags(
            user_tags, source_tags, getattr(channel, "default_tags", None),
            include_source=settings.auto_add_source_tags,
            max_tags=settings.max_tags, max_length=settings.max_tag_length,
        )

    def is_news_job(job: Job) -> bool:
        value = getattr(job, "content_kind", job.post_data.get("content_kind", "artwork"))
        return ContentKind(value) == ContentKind.NEWS

    def schedule_example() -> str:
        tomorrow = datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)
        return tomorrow.replace(second=0, microsecond=0).strftime("%d.%m.%Y %H:%M")

    async def channel_permission_text(channel: Channel, bot) -> str:
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=5)
            member = await asyncio.wait_for(
                bot.get_chat_member(channel.telegram_chat_id, me.id), timeout=5,
            )
            member_status = getattr(member.status, "value", str(member.status))
            can_post = member_status == "creator" or (
                member_status == "administrator"
                and bool(getattr(member, "can_post_messages", False))
            )
            return (
                f"{'✅' if can_post else '❌'} {escape(member_status)} · "
                f"{'может публиковать' if can_post else 'нет права публикации'}"
            )
        except Exception as error:
            logger.warning(
                "channel_permission_check_failed channel=%s", channel.alias, exc_info=True,
            )
            return f"❌ проверка не выполнена · {escape(type(error).__name__)}"

    async def channel_details_text(
        channel: Channel, bot, *, capture_subscribers: bool = False,
    ) -> str:
        rows = await jobs.managed_queue(channel.id, "active", limit=None)
        now = datetime.now(UTC)
        queued_count = sum(job.status == JobStatus.QUEUED for job in rows)
        scheduled_count = sum(job.status == JobStatus.SCHEDULED for job in rows)
        processing_count = len(rows) - queued_count - scheduled_count
        nearest = next_queued_by_schedule(rows, now)
        if nearest is None:
            nearest_text = "—"
        elif channel.is_paused and not nearest[0].force_publish:
            nearest_text = f"#{nearest[0].id} · после возобновления"
        else:
            nearest_text = f"#{nearest[0].id} · {format_countdown(nearest[1], now)}"
        if not channel.is_enabled:
            channel_status = "⚪ отключён конфигурацией"
        elif channel.is_paused:
            channel_status = "⏸ приостановлен"
        elif channel.active_job_id is not None:
            channel_status = f"⚙️ обрабатывается задание #{channel.active_job_id}"
        else:
            channel_status = "🟢 активен"
        default = " · основной" if channel.is_default else ""
        rights = await channel_permission_text(channel, bot)
        subscriber_stats = (
            await channel_stats.capture_for_display(channel)
            if capture_subscribers
            else await channel_stats.summary(channel.id)
        )
        subscribers = render_channel_subscriber_stats(subscriber_stats)
        return (
            f"📡 <b>{escape(channel.title)}</b> · <code>{escape(channel.alias)}</code>{default}\n\n"
            f"Статус: {channel_status}\n"
            f"Очередь: {queued_count} ожидают · {scheduled_count} запланировано · "
            f"{processing_count} в работе\n"
            f"{queue_total_line(rows, channel, now)}\n"
            f"Ближайшая публикация: {nearest_text}\n"
            f"Режим: <code>{escape(channel.publish_mode)}</code>\n"
            f"Интервал: {format_duration(channel.publish_interval_seconds)}\n"
            f"Теги канала: {escape(hashtags(getattr(channel, 'default_tags', None) or [])) or 'не заданы'}\n"
            f"{subscribers}\n"
            f"Права бота: {rights}"
        )

    async def edit_channel_screen(
        message: Message, channel: Channel, page: int, *,
        capture_subscribers: bool = False,
    ) -> None:
        text = await channel_details_text(
            channel, message.bot, capture_subscribers=capture_subscribers,
        )
        try:
            await message.edit_text(
                text, parse_mode="HTML", reply_markup=channel_details_keyboard(channel, page),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise

    async def queue_view_text(
        rows: list[Job], *, channel: Channel | None, status_filter: str,
        schedule_rows: list[Job] | None = None,
    ) -> str:
        scope_label = escape(channel.alias) if channel is not None else "все каналы"
        lines = [
            f"📋 <b>Очередь: {scope_label}</b>",
            f"Фильтр: {QUEUE_FILTER_LABELS[status_filter]} · найдено {len(rows)}",
        ]
        now = datetime.now(UTC)
        if channel is not None:
            lines.append(queue_total_line(schedule_rows or [], channel, now))
        lines.append("")
        estimates = {
            job.id: estimate
            for job, estimate in estimate_queue_schedule(schedule_rows or [])
        }
        for job in rows[:15]:
            title = str(job.post_data.get("title") or "Без названия")
            suffix = ""
            if (
                job.channel.is_paused
                and job.status in {JobStatus.QUEUED, JobStatus.SCHEDULED}
                and not job.force_publish
            ):
                suffix = " · канал на паузе"
            elif job.status == JobStatus.SCHEDULED and job.scheduled_at is not None:
                suffix = (
                    " · " + format_schedule_datetime(job.scheduled_at, settings.timezone)
                    + (f" · {format_countdown(estimates[job.id], now)}" if job.id in estimates else "")
                )
            elif job.id in estimates:
                suffix = f" · {format_countdown(estimates[job.id], now)}"
            lines.append(
                f"#{job.id} · {escape(job_status_label(job.status))} · "
                f"{escape(job.channel.alias)}{suffix}\n"
                f"  {escape(title[:80])}"
            )
        if not rows:
            lines.append("Заданий с таким статусом нет.")
        elif len(rows) > 15:
            lines.append(f"\n…показаны первые 15 из {len(rows)} заданий.")
        return "\n".join(lines)

    async def resolve_queue_scope(scope: int) -> Channel | None:
        return None if scope == 0 else await jobs.get_channel_by_id(scope)

    async def edit_queue_view(
        message: Message, *, page: int, scope: int, status_filter: str,
    ) -> None:
        channel = await resolve_queue_scope(scope)
        if scope and channel is None:
            await message.edit_text("Канал больше не существует.", reply_markup=back_menu_keyboard())
            return
        rows = await jobs.managed_queue(scope or None, status_filter, limit=None)
        schedule_rows = (
            rows
            if status_filter == "active"
            else await jobs.managed_queue(scope or None, "active", limit=None)
            if scope or status_filter in {"queued", "scheduled", "processing"}
            else None
        )
        text = await queue_view_text(
            rows,
            channel=channel,
            status_filter=status_filter,
            schedule_rows=schedule_rows,
        )
        try:
            await message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=queue_view_keyboard(
                    rows, page=page, scope=scope, status_filter=status_filter,
                ),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise

    async def queue_job_text(job: Job) -> str:
        post = deserialize_post(job.post_data)
        estimate = None
        if job.status in ACTIVE_JOB_STATUSES:
            channel_rows = await jobs.managed_queue(job.target_channel_id, "active", limit=None)
            estimate = next(
                (value for row, value in estimate_queue_schedule(channel_rows) if row.id == job.id),
                None,
            )
        scheduled = (
            format_schedule_datetime(job.scheduled_at, settings.timezone)
            if job.scheduled_at is not None else "—"
        )
        lines = [
            f"📌 <b>Задание #{job.id}</b>",
            f"Статус: {escape(job_status_label(job.status))}",
            f"Канал: {escape(job.channel.alias)}",
            f"Название: {escape(post.title)}",
            f"Позиция: {job.queue_position or '—'}",
            f"Точное время ({escape(settings.timezone)}): {scheduled}",
            "Расчётное время: после возобновления"
            if (
                job.channel.is_paused
                and job.status in {JobStatus.QUEUED, JobStatus.SCHEDULED}
                and not job.force_publish
            )
            else f"Расчётное время: {format_countdown(estimate) if estimate else '—'}",
            f"Попытки: {job.attempts}/{job.max_attempts}",
        ]
        if job.error_message:
            lines.extend(("", f"Ошибка: {escape(job.error_message[:500])}"))
        return "\n".join(lines)

    async def fetch_posts(
        urls: list[str],
    ) -> tuple[list[tuple[int, SourcePost]], list[tuple[str, str]]]:
        posts: list[tuple[int, SourcePost]] = []
        failures: list[tuple[str, str]] = []
        for position, url in enumerate(urls, start=1):
            try:
                post = await ingest.fetch(url)
                await translator.enrich_title(post)
                posts.append((position, post))
            except ApplicationError as error:
                failures.append((url, str(error)))
            except Exception as error:
                logger.error(
                    "batch_ingest_failed url=%s", url,
                    exc_info=(type(error), error, error.__traceback__),
                )
                failures.append((url, "Не удалось получить публикацию"))
        return posts, failures

    async def send_failures(message: Message, failures: list[tuple[str, str]], total: int) -> None:
        if not failures:
            return
        details = "\n".join(f"• {escape(url)} — {escape(reason)}" for url, reason in failures)
        await message.answer(
            f"Не удалось обработать {len(failures)} из {total} ссылок:\n{details}",
        )

    async def confirmation_payload(
        job: Job, *, position: int = 1, total: int = 1,
    ) -> tuple[Job, str] | None:
        stored = await jobs.get(job.id)
        if stored is None:
            return None
        post = deserialize_post(stored.post_data)
        prefix = f"Ссылка {position}/{total}\n\n" if total > 1 else ""
        publication_time = (
            format_schedule_datetime(stored.scheduled_at, settings.timezone)
            if getattr(stored, "scheduled_at", None) is not None else "по очереди"
        )
        if post.content_kind == ContentKind.NEWS:
            return stored, (
                prefix
                + f"📰 <b>Черновик новости #{stored.id}</b>\n"
                + f"Название: {escape(post.title[:160])}\n"
                + f"Медиа: {len(post.media_items)}\n"
                + f"Канал: <code>{escape(stored.channel.alias)}</code>\n"
                + f"Время публикации: {escape(publication_time)}\n"
                + f"Длина текста: {telegram_html_length(await previews.caption(stored))} / 4096 символов\n\n"
                + "Полный предпросмотр отправляется отдельным сообщением."
            )
        caption = await previews.caption(stored)
        return stored, (
            prefix
            + f"🖼 <b>Черновик #{stored.id}</b>\n\n"
            + f"Источник: {escape(provider_label(post.provider))}\n"
            + f"Выбрано: {len(post.media_items)} из {read_album(stored.post_data).original_count}\n"
            + ("Пока выбраны первые кадры. Полный альбом доступен в редакторе.\n" if post.metadata.get("artwork_album", {}).get("automatic_selection") else "")
            + f"Канал: {escape(stored.channel.alias)}\n"
            + f"Время публикации: {escape(publication_time)}\n"
            + f"Подпись: {telegram_html_length(caption)} / 1024 символов\n\n"
            + "<b>Итоговая подпись</b>\n"
            + caption
        )

    async def send_confirmation(
        message: Message, job: Job, *, position: int = 1, total: int = 1,
        queued: bool = False,
    ) -> None:
        payload = await confirmation_payload(job, position=position, total=total)
        if payload is None:
            await message.answer(f"Не удалось открыть созданное задание #{job.id}.")
            return
        stored, text = payload
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=(
                news_preview_keyboard(
                    stored.id,
                    len(stored.post_data.get("media_items", [])),
                    queued=queued or getattr(stored, "status", None) in {
                        JobStatus.QUEUED, JobStatus.SCHEDULED,
                    },
                )
                if is_news_job(stored)
                else queued_preview_keyboard(stored.id) if queued else preview_keyboard(stored.id)
            ),
        )

    async def queue_news_requests(
        message: Message, requests: list[NewsSourceRequest], tags: list[str],
        alias: str | None,
    ) -> None:
        if news_submissions is None or news_tasks is None:
            await message.answer("Обработка новостей в этой сборке не настроена.")
            return
        for request in requests:
            try:
                queued = await news_submissions.create(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                    display_name=message.from_user.full_name,
                    origin_chat_id=message.chat.id,
                    request=request,
                    user_tags=tags,
                    channel_alias=alias,
                )
            except (TypeError, ValueError) as error:
                await message.answer(str(error))
                continue
            status = await message.answer(
                f"📰 Обработка новости #{queued.task.id}\n"
                f"Канал: {queued.channel.alias}\n"
                "Текущий этап: ожидаем домашний обработчик",
                reply_markup=news_task_keyboard(queued.task.id),
            )
            await news_tasks.set_status_message(queued.task.id, status.message_id)

    async def create_preview_messages(
        message: Message, user_id: int, posts: list[tuple[int, SourcePost]], channel: Channel,
        tags: list[str], total: int,
    ) -> None:
        for position, post in posts:
            job = await jobs.create_preview(
                user_id, post, channel.id, tags, settings.max_job_attempts,
            )
            duplicate_state = await jobs.duplicate_state_for(
                post.provider, post.source_id, channel.id, job.id,
            )
            prefix = f"Ссылка {position}/{total}\n\n" if total > 1 else ""
            if duplicate_state:
                duplicate_message = (
                    f"Эта публикация уже была отправлена в канал {escape(channel.alias)}. "
                    "Повторить публикацию?"
                    if duplicate_state == "published"
                    else f"Эта публикация уже ожидает обработку для канала {escape(channel.alias)}. "
                    "Добавить её повторно?"
                )
                await message.answer(
                    prefix + duplicate_message,
                    reply_markup=duplicate_keyboard(job.id),
                )
                continue
            await send_confirmation(message, job, position=position, total=total)

    async def begin_wizard(
        message: Message, state: FSMContext, urls: list[str], tags: list[str],
    ) -> None:
        await state.clear()
        user = await jobs.ensure_user(
            message.from_user.id, message.from_user.username, message.from_user.full_name,
        )
        await message.answer(
            f"⏳ Шаг 1 из 3 · получаю данные по {len(urls)} "
            f"{'ссылке' if len(urls) == 1 else 'ссылкам'}…",
        )
        posts, failures = await fetch_posts(urls)
        await send_failures(message, failures, len(urls))
        if not posts:
            await state.clear()
            return
        channels = [channel for channel in await jobs.channels() if channel.is_enabled]
        if not channels:
            await state.clear()
            await message.answer("Нет активных каналов для публикации.")
            return
        await state.set_state(CreatePublication.waiting_for_channel)
        await state.set_data({
            "wizard_user_id": user.id,
            "wizard_posts": [
                {"position": position, "post": serialize_post(post)}
                for position, post in posts
            ],
            "wizard_tags": tags,
            "wizard_total": len(urls),
        })
        await message.answer(
            f"Шаг 2 из 3 · выберите канал для {len(posts)} "
            f"{'публикации' if len(posts) == 1 else 'публикаций'}:",
            reply_markup=wizard_channel_keyboard(channels),
        )

    async def menu_callback_message(callback: CallbackQuery) -> Message | None:
        if isinstance(callback.message, Message):
            return callback.message
        await callback.answer("Сообщение меню больше недоступно.", show_alert=True)
        return None

    def parse_page(data: str, prefix: str) -> int | None:
        value = data.removeprefix(prefix)
        return int(value) if value.isdigit() else None

    def parse_paginated_selection(data: str, prefix: str) -> tuple[int, str] | None:
        parts = data.removeprefix(prefix).split(":", 1)
        if len(parts) == 1:
            return 0, parts[0]  # Compatibility with buttons sent before pagination was added.
        if not parts[0].isdigit():
            return None
        return int(parts[0]), parts[1]

    async def edit_menu_markup(message: Message, markup: InlineKeyboardMarkup) -> None:
        try:
            await message.edit_reply_markup(reply_markup=markup)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise

    @router.message(Command("start", "menu"))
    async def main_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())

    @router.message(Command("new"))
    @router.message(F.text == ADD_BUTTON)
    async def new_publication(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(CreatePublication.waiting_for_urls)
        await message.answer(
            "Шаг 1 из 3 · отправьте одну или несколько ссылок на публикации.\n\n"
            "Для альбома Pixiv или DeviantArt можно дописать номера изображений, "
            "например <code>[1,3,5-7]</code>. Нумерация начинается с 1.\n\n"
            "Канал будет выбран на следующем шаге.",
            parse_mode="HTML",
            reply_markup=wizard_cancel_keyboard(),
        )

    @router.message(CreatePublication.waiting_for_urls)
    async def wizard_urls(message: Message, state: FSMContext) -> None:
        try:
            urls, tags, _ = ingest.parse(message.text or "")
        except ApplicationError as error:
            await message.answer(str(error), reply_markup=wizard_cancel_keyboard())
            return
        await begin_wizard(message, state, urls, tags)

    @router.message(CreatePublication.waiting_for_channel)
    async def wizard_waiting_for_channel(message: Message) -> None:
        await message.answer(
            "Выберите канал кнопкой под сообщением или отмените создание.",
            reply_markup=wizard_cancel_keyboard(),
        )

    @router.callback_query(F.data == "wizard_cancel")
    async def wizard_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        message = await menu_callback_message(callback)
        if message is None:
            return
        current_state = await state.get_state()
        if current_state not in {
            CreatePublication.waiting_for_urls.state,
            CreatePublication.waiting_for_channel.state,
        }:
            await callback.answer("Этот мастер уже завершён.", show_alert=True)
            return
        await state.clear()
        await message.edit_text("Создание публикации отменено.")
        await callback.answer()

    @router.callback_query(
        CreatePublication.waiting_for_channel,
        F.data.startswith("wizard_channel_page:"),
    )
    async def wizard_channel_page(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        page = parse_page(callback.data, "wizard_channel_page:")
        if page is None:
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        channels = await jobs.channels()
        await callback.answer()
        await edit_menu_markup(message, wizard_channel_keyboard(channels, page))

    @router.callback_query(
        CreatePublication.waiting_for_channel,
        F.data.startswith("wizard_channel:"),
    )
    async def wizard_channel_selection(callback: CallbackQuery, state: FSMContext) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parsed = parse_paginated_selection(callback.data, "wizard_channel:")
        if parsed is None or not parsed[1].isdigit():
            await callback.answer("Некорректный канал.", show_alert=True)
            return
        channel_id = int(parsed[1])
        channel = next(
            (
                item for item in await jobs.channels()
                if item.id == channel_id and item.is_enabled
            ),
            None,
        )
        if channel is None:
            await callback.answer("Канал удалён или отключён.", show_alert=True)
            return
        data = await state.get_data()
        stored_posts = data.get("wizard_posts")
        user_id = data.get("wizard_user_id")
        if not isinstance(stored_posts, list) or not isinstance(user_id, int):
            await state.clear()
            await callback.answer("Мастер устарел. Начните создание заново.", show_alert=True)
            return
        posts = [
            (int(item["position"]), deserialize_post(item["post"]))
            for item in stored_posts
        ]
        tags = list(data.get("wizard_tags") or [])
        total = int(data.get("wizard_total") or len(posts))
        await state.clear()
        await callback.answer("Создаю предпросмотр…")
        await message.edit_text(
            f"Шаг 3 из 3 · канал {escape(channel.alias)} выбран.\n\n"
            "Проверьте итоговую подпись ниже. При необходимости измените её, затем нажмите «Опубликовать».",
        )
        await create_preview_messages(message, user_id, posts, channel, tags, total)

    async def show_help(message: Message) -> None:
        await message.answer(
            render_help(), parse_mode="HTML", reply_markup=back_menu_keyboard(),
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await show_help(message)

    @router.message(F.text == HELP_BUTTON)
    async def help_button(message: Message) -> None:
        await show_help(message)

    @router.callback_query(F.data == BACK_CALLBACK)
    async def return_to_main_menu(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None:
            return
        await callback.answer()
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())

    @router.message(Command("providers"))
    async def providers(message: Message) -> None:
        await message.answer("Поддерживаемые источники: " + ", ".join(registry.names))

    @router.message(Command("channels"))
    async def channels(message: Message) -> None:
        rows = await jobs.channels()
        await message.answer("\n".join(
            f"{item.alias} — {item.title}{' (основной)' if item.is_default else ''}; "
            f"интервал: {format_duration(item.publish_interval_seconds)}"
            f"{' · на паузе' if item.is_paused else ''}"
            for item in rows
        ) or "Каналы не настроены.")

    @router.message(F.text == CHANNELS_BUTTON)
    async def channels_button(message: Message) -> None:
        rows = await jobs.channels()
        await message.answer(
            "📡 Зарегистрированные каналы\n🟢 активен · ⏸ приостановлен · ⚪ отключён",
            reply_markup=channels_menu_keyboard(rows),
        )

    @router.callback_query(F.data.startswith(CHANNEL_CALLBACK_PREFIX))
    async def registered_channel_details(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parsed = parse_paginated_selection(callback.data, CHANNEL_CALLBACK_PREFIX)
        if parsed is None or not parsed[1].isdigit():
            await callback.answer("Некорректный канал.", show_alert=True)
            return
        page, raw_channel_id = parsed
        channel = await jobs.get_channel_by_id(int(raw_channel_id))
        if channel is None:
            await callback.answer("Канал больше не существует.", show_alert=True)
            return
        await callback.answer("Обновляю данные канала…")
        await edit_channel_screen(
            message, channel, page, capture_subscribers=True,
        )

    @router.callback_query(F.data.startswith("channel_refresh:"))
    async def refresh_channel_details(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_channel_id = callback.data.split(":", 2)
        channel = await jobs.get_channel_by_id(int(raw_channel_id))
        if channel is None:
            await callback.answer("Канал больше не существует.", show_alert=True)
            return
        await callback.answer("Обновляю…")
        await edit_channel_screen(
            message, channel, int(raw_page), capture_subscribers=True,
        )

    @router.callback_query(F.data.startswith("channel_back:"))
    async def channel_details_back(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        page = int(callback.data.rsplit(":", 1)[1])
        channels = await jobs.channels()
        await callback.answer()
        await message.edit_text(
            "📡 Зарегистрированные каналы\n🟢 активен · ⏸ приостановлен · ⚪ отключён",
            reply_markup=channels_menu_keyboard(channels, page),
        )

    @router.callback_query(F.data.startswith("channel_pause:"))
    async def toggle_channel_pause(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_channel_id = callback.data.split(":", 2)
        channel = await jobs.get_channel_by_id(int(raw_channel_id))
        if channel is None or not channel.is_enabled:
            await callback.answer("Отключённым каналом нельзя управлять.", show_alert=True)
            return
        updated = await jobs.set_channel_paused(channel.id, not channel.is_paused)
        if updated is None:
            await callback.answer("Не удалось изменить состояние канала.", show_alert=True)
            return
        if not updated.is_paused:
            wakeup.set()
        await callback.answer("Канал приостановлен." if updated.is_paused else "Канал возобновлён.")
        await edit_channel_screen(message, updated, int(raw_page))

    @router.callback_query(F.data.startswith("channel_default:"))
    async def make_channel_default(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_channel_id = callback.data.split(":", 2)
        channel = await jobs.set_default_channel(int(raw_channel_id))
        if channel is None:
            await callback.answer("Канал недоступен.", show_alert=True)
            return
        await callback.answer(f"Основной канал: {channel.alias}")
        await edit_channel_screen(message, channel, int(raw_page))

    @router.callback_query(F.data.startswith("channel_publish:"))
    async def publish_next_for_channel(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_channel_id = callback.data.split(":", 2)
        channel_id = int(raw_channel_id)
        job = await jobs.force_next_publish(channel_id)
        if job is None:
            await callback.answer("В канале нет ожидающих заданий.", show_alert=True)
            return
        wakeup.set()
        channel = await jobs.get_channel_by_id(channel_id)
        await callback.answer(f"Задание #{job.id} отправится следующим.")
        if channel is not None:
            await edit_channel_screen(message, channel, int(raw_page))

    @router.callback_query(F.data.startswith("channel_interval_edit:"))
    async def edit_channel_interval(callback: CallbackQuery, state: FSMContext) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_channel_id = callback.data.split(":", 2)
        channel = await jobs.get_channel_by_id(int(raw_channel_id))
        if channel is None or not channel.is_enabled:
            await callback.answer("Канал недоступен.", show_alert=True)
            return
        await state.set_state(ManageChannel.waiting_for_interval)
        await state.set_data({
            "channel_id": channel.id,
            "channel_page": int(raw_page),
        })
        await message.edit_text(
            f"Текущий интервал {escape(channel.alias)}: "
            f"{format_duration(channel.publish_interval_seconds)}.\n\n"
            "Отправьте новый интервал: 30s, 15m, 2h, 1d или 0.",
            reply_markup=channel_interval_input_keyboard(channel.id, int(raw_page)),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("channel_interval_cancel:"))
    async def cancel_channel_interval(callback: CallbackQuery, state: FSMContext) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_channel_id = callback.data.split(":", 2)
        await state.clear()
        channel = await jobs.get_channel_by_id(int(raw_channel_id))
        if channel is None:
            await callback.answer("Канал больше не существует.", show_alert=True)
            return
        await callback.answer()
        await edit_channel_screen(message, channel, int(raw_page))

    @router.message(ManageChannel.waiting_for_interval)
    async def receive_channel_interval(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, int):
            await state.clear()
            await message.answer("Экран управления каналом устарел.")
            return
        try:
            seconds = parse_duration((message.text or "").strip())
        except ValueError as error:
            await message.answer(
                str(error),
                reply_markup=channel_interval_input_keyboard(
                    channel_id, int(data.get("channel_page", 0)),
                ),
            )
            return
        channel = await jobs.set_channel_interval_by_id(channel_id, seconds)
        await state.clear()
        if channel is None:
            await message.answer("Канал больше недоступен.")
            return
        wakeup.set()
        await message.answer(
            f"Интервал {escape(channel.alias)} изменён: {format_duration(seconds)}.",
            parse_mode="HTML",
            reply_markup=channel_details_keyboard(channel, int(data.get("channel_page", 0))),
        )

    @router.callback_query(F.data.startswith(CHANNEL_PAGE_CALLBACK_PREFIX))
    async def channels_menu_page(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        page = parse_page(callback.data, CHANNEL_PAGE_CALLBACK_PREFIX)
        if page is None:
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        channels = await jobs.channels()
        await callback.answer()
        await edit_menu_markup(message, channels_menu_keyboard(channels, page))

    @router.message(Command("channel_interval"))
    async def channel_interval(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split()
        if len(parts) != 2:
            await message.answer("Использование: /channel_interval <alias> <30s|15m|2h|1d|0>")
            return
        alias = parts[0].strip().lower()
        try:
            seconds = parse_duration(parts[1])
        except ValueError as error:
            await message.answer(str(error))
            return
        channel = await jobs.set_channel_interval(alias, seconds)
        if not channel:
            await message.answer("Канал не найден или отключён.")
            return
        wakeup.set()
        await message.answer(f"Интервал канала {escape(alias)}: {format_duration(seconds)}.")

    @router.message(Command("status"))
    async def status(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.isdigit():
            await message.answer("Использование: /status <job_id>")
            return
        job = await jobs.get(int(command.args))
        await message.answer(f"Задание #{job.id}: {job.status}" if job else "Задание не найдено.")

    async def queue_text(alias: str | None) -> str:
        # The display limit must be applied after schedules from all channels
        # are merged. Limiting the database query here can hide whole channels
        # because JobService.queue orders rows by channel alias.
        rows = await jobs.queue(alias, limit=None)
        if rows is None:
            return f"Канал {escape(alias or '')} не найден или отключён."
        if not rows:
            return (
                f"Очередь канала {escape(alias)} пуста."
                if alias else "Очередь пуста."
            )
        now = datetime.now(UTC)
        schedule = estimate_queue_schedule(rows, now)
        lines = []
        for job, estimate in schedule[:50]:
            timing = (
                "после возобновления"
                if job.channel.is_paused and not job.force_publish
                else format_countdown(estimate, now)
            )
            exact = (
                f" · точно {format_schedule_datetime(job.scheduled_at, settings.timezone)}"
                if job.scheduled_at is not None else ""
            )
            lines.append(
                f"#{job.id} · {job.status} · {escape(job.channel.alias)} · {timing}"
                f"{' · вручную' if job.force_publish else ''}{exact}"
            )
        if alias:
            lines.insert(0, queue_total_line(rows, rows[0].channel, now))
        if len(schedule) > 50:
            lines.append(f"…показаны ближайшие 50 из {len(schedule)} заданий.")
        return "\n".join(lines)

    @router.message(Command("queue"))
    async def queue(message: Message, command: CommandObject) -> None:
        alias = (command.args or "").strip().lower() or None
        if alias and len(alias.split()) != 1:
            await message.answer("Использование: /queue [alias]")
            return
        await message.answer(await queue_text(alias))

    @router.message(F.text == QUEUE_BUTTON)
    async def queue_button(message: Message) -> None:
        channels = await jobs.channels()
        await message.answer(
            "📋 Выберите общую очередь или активный канал:",
            reply_markup=queue_menu_keyboard(channels),
        )

    @router.callback_query(F.data.startswith(QUEUE_PAGE_CALLBACK_PREFIX))
    async def queue_menu_page(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        page = parse_page(callback.data, QUEUE_PAGE_CALLBACK_PREFIX)
        if page is None:
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        channels = await jobs.channels()
        await callback.answer()
        await edit_menu_markup(message, queue_menu_keyboard(channels, page))

    @router.callback_query(F.data.startswith(QUEUE_CALLBACK_PREFIX))
    async def queue_menu_selection(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parsed = parse_paginated_selection(callback.data, QUEUE_CALLBACK_PREFIX)
        if parsed is None:
            await callback.answer("Некорректный пункт меню.", show_alert=True)
            return
        page, selection = parsed
        channels = await jobs.channels()
        scope = 0
        if selection != "all":
            if not selection.isdigit():
                await callback.answer("Некорректный пункт меню.", show_alert=True)
                return
            channel = next(
                (item for item in channels if item.id == int(selection) and item.is_enabled),
                None,
            )
            if channel is None:
                await callback.answer("Канал удалён или отключён.", show_alert=True)
                return
            scope = channel.id
        await callback.answer()
        await edit_queue_view(
            message, page=page, scope=scope, status_filter="active",
        )

    @router.callback_query(F.data.startswith("queue_back:"))
    async def queue_view_back(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        page = int(callback.data.rsplit(":", 1)[1])
        channels = await jobs.channels()
        await callback.answer()
        await message.edit_text(
            "📋 Выберите общую очередь или активный канал:",
            reply_markup=queue_menu_keyboard(channels, page),
        )

    @router.callback_query(F.data.startswith("queue_filter:"))
    async def queue_filter_selection(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 3)
        if (
            len(parts) != 4 or not parts[1].isdigit() or not parts[2].isdigit()
            or parts[3] not in QUEUE_FILTER_STATUSES
        ):
            await callback.answer("Некорректный фильтр.", show_alert=True)
            return
        await callback.answer()
        await edit_queue_view(
            message,
            page=int(parts[1]),
            scope=int(parts[2]),
            status_filter=parts[3],
        )

    @router.callback_query(F.data.startswith("queue_job:"))
    async def queue_job_details(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 4)
        if (
            len(parts) != 5 or not all(value.isdigit() for value in (parts[1], parts[2], parts[4]))
            or parts[3] not in QUEUE_FILTER_STATUSES
        ):
            await callback.answer("Некорректное задание.", show_alert=True)
            return
        page, scope, status_filter, job_id = (
            int(parts[1]), int(parts[2]), parts[3], int(parts[4])
        )
        job = await jobs.get(job_id)
        if (
            job is None or job.status not in QUEUE_FILTER_STATUSES[status_filter]
            or (scope and job.target_channel_id != scope)
        ):
            await callback.answer("Задание больше не входит в этот список.", show_alert=True)
            return
        await callback.answer()
        await message.edit_text(
            await queue_job_text(job),
            parse_mode="HTML",
            reply_markup=queue_job_keyboard(
                job, page=page, scope=scope, status_filter=status_filter,
            ),
        )

    @router.callback_query(F.data.startswith("queue_move:"))
    async def move_queue_job(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 6)
        if len(parts) != 6:
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        _, raw_page, raw_scope, status_filter, raw_job_id, direction = parts
        if (
            not all(value.isdigit() for value in (raw_page, raw_scope, raw_job_id))
            or status_filter not in QUEUE_FILTER_STATUSES or direction not in {"up", "down"}
        ):
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        job = await jobs.move_queued(int(raw_job_id), direction)
        if job is None:
            await callback.answer("Задание уже нельзя переместить.", show_alert=True)
            return
        refreshed = await jobs.get(job.id)
        await callback.answer("Порядок очереди обновлён.")
        if refreshed is not None:
            await message.edit_text(
                await queue_job_text(refreshed),
                parse_mode="HTML",
                reply_markup=queue_job_keyboard(
                    refreshed,
                    page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
                ),
            )

    @router.callback_query(F.data.startswith("queue_schedule:"))
    async def schedule_queue_job(callback: CallbackQuery, state: FSMContext) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 4)
        if len(parts) != 5:
            await callback.answer("Некорректное задание.", show_alert=True)
            return
        _, raw_page, raw_scope, status_filter, raw_job_id = parts
        if (
            not all(value.isdigit() for value in (raw_page, raw_scope, raw_job_id))
            or status_filter not in QUEUE_FILTER_STATUSES
        ):
            await callback.answer("Некорректное задание.", show_alert=True)
            return
        job = await jobs.get(int(raw_job_id))
        if job is None or job.status not in {JobStatus.QUEUED, JobStatus.SCHEDULED}:
            await callback.answer("Задание уже нельзя запланировать.", show_alert=True)
            return
        await state.set_state(ManageQueue.waiting_for_schedule)
        await state.set_data({
            "job_id": job.id,
            "queue_page": int(raw_page),
            "queue_scope": int(raw_scope),
            "queue_filter": status_filter,
        })
        await message.edit_text(
            f"Введите точное время публикации в часовом поясе {escape(settings.timezone)}.\n"
            f"Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например {schedule_example()}.",
            parse_mode="HTML",
            reply_markup=schedule_input_keyboard(
                job.id,
                page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("queue_schedule_cancel:"))
    async def cancel_queue_schedule(callback: CallbackQuery, state: FSMContext) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 4)
        if len(parts) != 5:
            await callback.answer("Некорректное задание.", show_alert=True)
            return
        _, raw_page, raw_scope, status_filter, raw_job_id = parts
        await state.clear()
        job = await jobs.get(int(raw_job_id))
        if job is None:
            await callback.answer("Задание больше не существует.", show_alert=True)
            return
        await callback.answer()
        await message.edit_text(
            await queue_job_text(job),
            parse_mode="HTML",
            reply_markup=queue_job_keyboard(
                job,
                page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
            ),
        )

    @router.callback_query(F.data.startswith("queue_schedule_clear:"))
    async def clear_queue_schedule(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_scope, status_filter, raw_job_id = callback.data.split(":", 4)
        job = await jobs.set_schedule(int(raw_job_id), None)
        if job is None:
            await callback.answer("Точное время уже нельзя изменить.", show_alert=True)
            return
        refreshed = await jobs.get(job.id)
        await callback.answer("Точное время сброшено.")
        if refreshed is not None:
            await message.edit_text(
                await queue_job_text(refreshed),
                parse_mode="HTML",
                reply_markup=queue_job_keyboard(
                    refreshed,
                    page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
                ),
            )

    @router.callback_query(F.data.startswith("queue_force:"))
    async def force_queue_job(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_scope, status_filter, raw_job_id = callback.data.split(":", 4)
        job = await jobs.force_publish(int(raw_job_id))
        if job is None:
            await callback.answer("Задание уже нельзя опубликовать вручную.", show_alert=True)
            return
        wakeup.set()
        refreshed = await jobs.get(job.id)
        await callback.answer("Задание отправится следующим.")
        if refreshed is not None:
            await message.edit_text(
                await queue_job_text(refreshed),
                parse_mode="HTML",
                reply_markup=queue_job_keyboard(
                    refreshed,
                    page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
                ),
            )

    @router.callback_query(F.data.startswith("queue_shuffle:"))
    async def shuffle_channel_queue(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 3)
        if (
            len(parts) != 4
            or not parts[1].isdigit()
            or not parts[2].isdigit()
            or int(parts[2]) == 0
            or parts[3] not in QUEUE_FILTER_STATUSES
        ):
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        page, scope, status_filter = int(parts[1]), int(parts[2]), parts[3]
        channel = await jobs.get_channel_by_id(scope)
        if channel is None or not channel.is_enabled:
            await callback.answer("Канал удалён или отключён.", show_alert=True)
            return
        count = await jobs.shuffle_queued(scope)
        if count < 2:
            await callback.answer(
                "Для перемешивания нужно минимум два ожидающих задания.",
                show_alert=True,
            )
        else:
            await callback.answer(f"Перемешано заданий: {count}.")
        await edit_queue_view(
            message, page=page, scope=scope, status_filter=status_filter,
        )

    @router.callback_query(F.data.startswith("queue_job_preview:"))
    async def preview_queue_job(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        if job is None or job.status not in {JobStatus.QUEUED, JobStatus.SCHEDULED}:
            await callback.answer("Предпросмотр уже недоступен.", show_alert=True)
            return
        await callback.answer("Готовлю медиа…")
        try:
            await previews.send(job, message.chat.id)
        except ApplicationError as error:
            await message.answer(f"Не удалось показать медиа: {error}")

    @router.callback_query(F.data.startswith("queue_cancel_job:"))
    async def cancel_queue_job(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_scope, status_filter, raw_job_id = callback.data.split(":", 4)
        if not await jobs.request_cancel(int(raw_job_id)):
            await callback.answer("Задание уже нельзя отменить.", show_alert=True)
            return
        wakeup.set()
        await callback.answer("Задание отменено.")
        await edit_queue_view(
            message,
            page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
        )

    @router.callback_query(F.data.startswith("queue_retry:"))
    async def retry_queue_job(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        _, raw_page, raw_scope, status_filter, raw_job_id = callback.data.split(":", 4)
        if not await jobs.enqueue(int(raw_job_id)):
            await callback.answer("Задание уже нельзя повторить.", show_alert=True)
            return
        wakeup.set()
        await callback.answer("Задание возвращено в очередь.")
        await edit_queue_view(
            message,
            page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
        )

    @router.callback_query(F.data.startswith("queue_bulk_prompt:"))
    async def prompt_queue_bulk_action(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 5)
        if len(parts) != 5:
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        _, action, raw_page, raw_scope, status_filter = parts
        if action not in {"cancel", "retry"} or status_filter not in QUEUE_FILTER_STATUSES:
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        action_text = (
            "Отменить все задания, попавшие под текущий фильтр?"
            if action == "cancel"
            else "Повторить все неудачные задания в выбранной области?"
        )
        await callback.answer()
        await message.edit_text(
            f"⚠️ {action_text}",
            reply_markup=bulk_confirmation_keyboard(
                action,
                page=int(raw_page), scope=int(raw_scope), status_filter=status_filter,
            ),
        )

    @router.callback_query(F.data.startswith("queue_bulk_confirm:"))
    async def confirm_queue_bulk_action(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 5)
        if len(parts) != 5:
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        _, action, raw_page, raw_scope, status_filter = parts
        scope = int(raw_scope)
        if action == "cancel":
            count = await jobs.cancel_filtered(scope or None, status_filter)
            result_text = f"Отменено или отмечено для отмены: {count}."
        elif action == "retry":
            result = await jobs.retry_failed(scope or None)
            result_text = f"Возвращено в очередь: {result.retried}."
            if result.skipped_uncertain:
                result_text += (
                    f" Пропущено uncertain_publish: {result.skipped_uncertain} — "
                    "их нужно проверить вручную."
                )
        else:
            await callback.answer("Некорректная операция.", show_alert=True)
            return
        wakeup.set()
        await callback.answer(result_text, show_alert=True)
        await edit_queue_view(
            message,
            page=int(raw_page), scope=scope, status_filter=status_filter,
        )

    @router.message(ManageQueue.waiting_for_schedule)
    async def receive_queue_schedule(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job_id = data.get("job_id")
        if not isinstance(job_id, int):
            await state.clear()
            await message.answer("Экран планирования устарел.")
            return
        try:
            scheduled_at = parse_schedule_datetime(
                message.text or "", settings.timezone,
            )
        except ValueError as error:
            await message.answer(
                str(error),
                reply_markup=schedule_input_keyboard(
                    job_id,
                    page=int(data.get("queue_page", 0)),
                    scope=int(data.get("queue_scope", 0)),
                    status_filter=str(data.get("queue_filter", "active")),
                ),
            )
            return
        job = await jobs.set_schedule(job_id, scheduled_at)
        await state.clear()
        if job is None:
            await message.answer("Задание уже нельзя запланировать.")
            return
        wakeup.set()
        refreshed = await jobs.get(job.id)
        if refreshed is None:
            await message.answer("Задание больше не существует.")
            return
        status_filter = str(data.get("queue_filter", "active"))
        await message.answer(
            await queue_job_text(refreshed),
            parse_mode="HTML",
            reply_markup=queue_job_keyboard(
                refreshed,
                page=int(data.get("queue_page", 0)),
                scope=int(data.get("queue_scope", 0)),
                status_filter=status_filter,
            ),
        )

    @router.message(Command("publish"))
    async def publish_job(message: Message, command: CommandObject) -> None:
        if command.args and not command.args.isdigit():
            await message.answer("Использование: /publish [job_id]")
            return
        job = (
            await jobs.force_publish(int(command.args))
            if command.args
            else await jobs.force_next_publish()
        )
        if not job:
            await message.answer(
                "Ручная публикация доступна только для задания со статусом queued."
                if command.args else "В очереди нет заданий для публикации."
            )
            return
        wakeup.set()
        await message.answer(
            f"Задание #{job.id} поставлено на ручную публикацию. "
            "После публикации интервал канала начнётся заново."
        )

    async def show_preview(message: Message, argument: str) -> None:
        if argument and len(argument.split()) != 1:
            await message.answer("Использование: /preview [job_id|alias]")
            return
        estimate = None
        if argument.isdigit():
            job = await jobs.get(int(argument))
        else:
            alias = argument or None
            rows = await jobs.queue(alias, limit=None)
            if rows is None:
                await message.answer(f"Канал {escape(alias or '')} не найден или отключён.")
                return
            scheduled = next_queued_by_schedule(rows)
            job, estimate = scheduled if scheduled else (None, None)
        if not job:
            await message.answer(
                "Задание не найдено."
                if argument.isdigit()
                else (
                    f"В очереди канала {escape(argument)} нет заданий для предпросмотра."
                    if argument
                    else "В очереди нет заданий для предпросмотра."
                )
            )
            return
        waiting_news = job.status == JobStatus.WAITING_CONFIRMATION and is_news_job(job)
        if job.status == JobStatus.WAITING_CONFIRMATION and not waiting_news:
            # Saved artwork drafts must remain reachable after FSM state/messages are lost,
            # including an empty selection that still needs the album editor.
            await send_confirmation(message, job)
            return
        if job.status not in {JobStatus.QUEUED, JobStatus.SCHEDULED} and not waiting_news:
            await message.answer(f"Задание #{job.id} уже не находится в очереди.")
            return
        preview_error: str | None = None
        try:
            await previews.send(job, message.chat.id)
        except ApplicationError as error:
            if not waiting_news:
                await message.answer(
                    f"Не удалось подготовить предпросмотр задания #{job.id}: {error}"
                )
                return
            preview_error = str(error)
        news = is_news_job(job)
        await message.answer(
            f"Предпросмотр задания #{job.id} · {escape(job.channel.alias)}"
            f"{' · ' + format_countdown(estimate) if estimate else ''}. "
            + (
                f"Предпросмотр медиа не удался: {escape(preview_error)}"
                if preview_error else "Очередь и интервал не изменены."
            ),
            reply_markup=(
                news_preview_keyboard(
                    job.id,
                    len(job.post_data.get("media_items", [])),
                    queued=not waiting_news,
                )
                if news else queued_preview_keyboard(job.id)
            ),
        )

    @router.message(Command("preview"))
    async def preview_job(message: Message, command: CommandObject) -> None:
        await show_preview(message, (command.args or "").strip().lower())

    @router.message(F.text == PREVIEW_BUTTON)
    async def preview_button(message: Message) -> None:
        channels = await jobs.channels()
        await message.answer(
            "🖼 Выберите активный канал или ближайший пост во всей очереди:",
            reply_markup=preview_menu_keyboard(channels),
        )

    @router.callback_query(F.data.startswith(PREVIEW_PAGE_CALLBACK_PREFIX))
    async def preview_menu_page(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        page = parse_page(callback.data, PREVIEW_PAGE_CALLBACK_PREFIX)
        if page is None:
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        channels = await jobs.channels()
        await callback.answer()
        await edit_menu_markup(message, preview_menu_keyboard(channels, page))

    @router.callback_query(F.data.startswith(PREVIEW_CALLBACK_PREFIX))
    async def preview_menu_selection(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parsed = parse_paginated_selection(callback.data, PREVIEW_CALLBACK_PREFIX)
        if parsed is None:
            await callback.answer("Некорректный пункт меню.", show_alert=True)
            return
        _, selection = parsed
        argument = ""
        if selection != "next":
            if not selection.isdigit():
                await callback.answer("Некорректный пункт меню.", show_alert=True)
                return
            channels = await jobs.channels()
            channel = next(
                (item for item in channels if item.id == int(selection) and item.is_enabled),
                None,
            )
            if channel is None:
                await callback.answer("Канал удалён или отключён.", show_alert=True)
                return
            argument = channel.alias
        await callback.answer("Подготавливаю предпросмотр…")
        target = f"канал {argument}" if argument else "вся очередь"
        await message.edit_text(f"⏳ Ищу ближайший пост · {target}")
        await show_preview(message, argument)

    @router.callback_query(F.data.startswith("preview_publish:"))
    async def publish_from_preview(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.force_publish(job_id)
        if not job:
            await callback.answer("Задание уже не находится в очереди.", show_alert=True)
            return
        await state.clear()
        wakeup.set()
        await callback.message.edit_text(
            f"Задание #{job_id} поставлено на публикацию сейчас. "
            "После отправки интервал канала начнётся заново."
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("preview_tags_replace:") | F.data.startswith("preview_tags_add:"))
    async def edit_queued_tags(callback: CallbackQuery, state: FSMContext) -> None:
        action, raw_job_id = callback.data.rsplit(":", 1)
        job_id = int(raw_job_id)
        job = await jobs.get(job_id)
        if not job or job.status not in {JobStatus.QUEUED, JobStatus.SCHEDULED}:
            await callback.answer("Задание уже не находится в очереди.", show_alert=True)
            return
        mode = "add" if action == "preview_tags_add" else "replace"
        await state.set_state(EditPreview.waiting_for_tags)
        await state.set_data({
            "job_id": job_id,
            "tag_context": "queued_preview",
            "tag_mode": mode,
            "control_chat_id": callback.message.chat.id,
            "control_message_id": callback.message.message_id,
        })
        prompt = (
            "Введите дополнительные пользовательские теги через пробел. "
            "Они будут добавлены перед текущими."
            if mode == "add"
            else "Введите новый список пользовательских тегов через пробел. Текущие теги будут заменены."
        )
        await callback.message.edit_text(prompt, reply_markup=cancel_tag_input_keyboard(job_id))
        await callback.answer()

    @router.callback_query(F.data.startswith("caption:") | F.data.startswith("preview_caption:"))
    async def edit_caption(callback: CallbackQuery, state: FSMContext) -> None:
        is_queued = callback.data.startswith("preview_caption:")
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        expected_statuses = (
            {JobStatus.QUEUED, JobStatus.SCHEDULED}
            if is_queued else {JobStatus.WAITING_CONFIRMATION}
        )
        if not job or job.status not in expected_statuses:
            await callback.answer("Подпись этого задания уже нельзя изменить.", show_alert=True)
            return
        current_caption = await previews.caption(job)
        await state.set_state(EditPreview.waiting_for_caption)
        await state.set_data({
            "job_id": job_id,
            "edit_context": "queued_preview" if is_queued else "initial_preview",
        })
        await callback.message.answer(
            "<b>Текущая подпись</b>\n"
            f"{current_caption}\n\n"
            "Отправьте новую подпись обычным текстом. Максимум — 1024 символа.",
            parse_mode="HTML",
            reply_markup=caption_input_keyboard(job_id),
        )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("schedule:") | F.data.startswith("preview_schedule:")
    )
    async def edit_preview_schedule(callback: CallbackQuery, state: FSMContext) -> None:
        queued = callback.data.startswith("preview_schedule:")
        job_id = int(callback.data.rsplit(":", 1)[1])
        job = await jobs.get(job_id)
        allowed_statuses = (
            {JobStatus.QUEUED, JobStatus.SCHEDULED}
            if queued else {JobStatus.WAITING_CONFIRMATION}
        )
        if not job or job.status not in allowed_statuses:
            await callback.answer("Время этого задания уже нельзя изменить.", show_alert=True)
            return
        context = "queued" if queued else "initial"
        await state.set_state(EditPreview.waiting_for_schedule)
        await state.set_data({"job_id": job_id, "schedule_context": context})
        current = (
            "\nТекущее время: "
            + format_schedule_datetime(job.scheduled_at, settings.timezone)
            if job.scheduled_at is not None else ""
        )
        await callback.message.answer(
            f"Введите точное время публикации в часовом поясе {escape(settings.timezone)}.\n"
            f"Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например {schedule_example()}.\n"
            "Отправьте <code>-</code>, чтобы вернуть публикацию в обычную очередь."
            f"{current}",
            parse_mode="HTML",
            reply_markup=preview_schedule_input_keyboard(job_id, context),
        )
        await callback.answer()

    @router.message(EditPreview.waiting_for_schedule)
    async def receive_preview_schedule(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job_id = data.get("job_id")
        context = data.get("schedule_context")
        if not isinstance(job_id, int) or context not in {"initial", "queued"}:
            await state.clear()
            await message.answer("Экран назначения времени устарел.")
            return
        raw_value = (message.text or "").strip()
        try:
            scheduled_at = (
                None
                if raw_value == "-"
                else parse_schedule_datetime(raw_value, settings.timezone)
            )
        except ValueError as error:
            await message.answer(
                str(error),
                reply_markup=preview_schedule_input_keyboard(job_id, context),
            )
            return
        job = await jobs.set_schedule(job_id, scheduled_at)
        await state.clear()
        if job is None:
            await message.answer("Время этого задания уже нельзя изменить.")
            return
        wakeup.set()
        await message.answer(
            "Точное время сброшено — публикация вернулась в обычную очередь."
            if scheduled_at is None
            else (
                "Публикация запланирована на "
                f"{format_schedule_datetime(scheduled_at, settings.timezone)}."
            )
        )
        await send_confirmation(message, job, queued=context == "queued")

    @router.callback_query(F.data.startswith("preview_schedule_cancel:"))
    async def cancel_preview_schedule(callback: CallbackQuery, state: FSMContext) -> None:
        _, context, raw_job_id = callback.data.split(":", 2)
        job_id = int(raw_job_id)
        data = await state.get_data()
        if data.get("job_id") != job_id or data.get("schedule_context") != context:
            await callback.answer("Ввод времени уже завершён.", show_alert=True)
            return
        job = await jobs.get(job_id)
        await state.clear()
        news = job is not None and is_news_job(job)
        await callback.message.edit_text(
            "Назначение времени отменено.",
            reply_markup=(
                news_preview_keyboard(
                    job_id,
                    len(job.post_data.get("media_items", [])) if job is not None else 0,
                    queued=context == "queued",
                )
                if news
                else queued_preview_keyboard(job_id)
                if context == "queued" else preview_keyboard(job_id)
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("title:") | F.data.startswith("description:"))
    async def edit_post_field(callback: CallbackQuery, state: FSMContext) -> None:
        field, raw_job_id = callback.data.split(":", 1)
        job_id = int(raw_job_id)
        job = await jobs.get(job_id)
        if not job or job.status != JobStatus.WAITING_CONFIRMATION:
            await callback.answer("Метаданные этого задания уже нельзя изменить.", show_alert=True)
            return
        post = deserialize_post(job.post_data)
        current = post.title if field == "title" else post.description
        state_value = (
            EditPreview.waiting_for_title
            if field == "title" else EditPreview.waiting_for_description
        )
        await state.set_state(state_value)
        await state.set_data({"job_id": job_id, "post_field": field})
        field_label = "заголовок" if field == "title" else "описание"
        clear_hint = " Отправьте <code>-</code>, чтобы удалить описание." if field == "description" else ""
        await callback.message.answer(
            f"<b>Текущий {field_label}</b>\n{escape(current) or '—'}\n\n"
            f"Отправьте новый {field_label} обычным текстом.{clear_hint}",
            parse_mode="HTML",
            reply_markup=post_field_input_keyboard(job_id, field),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("post_field_cancel:"))
    async def cancel_post_field_input(callback: CallbackQuery, state: FSMContext) -> None:
        _, field, raw_job_id = callback.data.split(":", 2)
        job_id = int(raw_job_id)
        data = await state.get_data()
        if data.get("job_id") != job_id or data.get("post_field") != field:
            await callback.answer("Редактирование уже завершено.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text(
            "Изменение отменено.", reply_markup=preview_keyboard(job_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("media:"))
    async def show_initial_preview_media(callback: CallbackQuery) -> None:
        job_id = int(callback.data.split(":", 1)[1])
        job = await jobs.get(job_id)
        if not job or job.status != JobStatus.WAITING_CONFIRMATION:
            await callback.answer("Предпросмотр уже недоступен.", show_alert=True)
            return
        await callback.answer("Готовлю облегчённое медиа…")
        try:
            await previews.send(job, callback.message.chat.id)
        except ApplicationError as error:
            await callback.message.answer(f"Не удалось показать медиа: {error}")

    @router.callback_query(F.data.startswith("caption_auto:"))
    async def restore_automatic_caption(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        data = await state.get_data()
        context = data.get("edit_context")
        job = await jobs.set_caption_override(job_id, None)
        if job is None:
            await callback.answer("Подпись этого задания уже нельзя изменить.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text("Автоматическая подпись восстановлена.")
        await send_confirmation(
            callback.message, job, queued=context == "queued_preview",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("caption_input_cancel:"))
    async def cancel_caption_input(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        data = await state.get_data()
        if (
            await state.get_state() != EditPreview.waiting_for_caption.state
            or data.get("job_id") != job_id
        ):
            await callback.answer("Редактирование подписи уже завершено.", show_alert=True)
            return
        await state.clear()
        keyboard = (
            queued_preview_keyboard(job_id)
            if data.get("edit_context") == "queued_preview"
            else preview_keyboard(job_id)
        )
        await callback.message.edit_text(
            "Изменение подписи отменено.", reply_markup=keyboard,
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("preview_cancel:"))
    async def cancel_from_preview(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        if not await jobs.request_cancel(job_id):
            await callback.answer("Задание уже нельзя отменить.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text(f"Публикация задания #{job_id} отменена и убрана из очереди.")
        await callback.answer()

    @router.message(Command("recent"))
    async def recent(message: Message) -> None:
        rows = await jobs.recent()
        await message.answer("\n".join(f"#{job.id} · {job.status} · {job.provider}" for job in rows) or "История пуста.")

    async def show_stats(message: Message) -> None:
        values = await jobs.stats()
        await message.answer("\n".join(f"{key}: {value}" for key, value in sorted(values.items())) or "Заданий пока нет.")

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        await show_stats(message)

    @router.message(F.text == STATS_BUTTON)
    async def stats_button(message: Message) -> None:
        await show_stats(message)

    async def show_health(message: Message, *, full: bool) -> None:
        report = await health.check(full=full)
        await message.answer(
            render_health_report(report),
            parse_mode="HTML",
            reply_markup=health_menu_keyboard(),
        )

    @router.message(Command("health"))
    async def health_status(message: Message, command: CommandObject) -> None:
        argument = (command.args or "").strip().lower()
        if argument not in {"", "full"}:
            await message.answer("Использование: /health [full]")
            return
        await show_health(message, full=argument == "full")

    @router.message(F.text == HEALTH_BUTTON)
    async def health_button(message: Message) -> None:
        await show_health(message, full=False)

    @router.callback_query(F.data.startswith(HEALTH_CALLBACK_PREFIX))
    async def health_menu_selection(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        selection = callback.data.removeprefix(HEALTH_CALLBACK_PREFIX)
        if selection not in {"refresh", "full"}:
            await callback.answer("Некорректный пункт меню.", show_alert=True)
            return
        full = selection == "full"
        await callback.answer("Выполняю полную проверку…" if full else "Обновляю…")
        if full:
            await message.edit_text("⏳ Выполняю полную проверку здоровья бота…")
        report = await health.check(full=full)
        try:
            await message.edit_text(
                render_health_report(report),
                parse_mode="HTML",
                reply_markup=health_menu_keyboard(),
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise

    @router.message(Command("cancel", "retry"))
    async def control(message: Message, command: CommandObject) -> None:
        if not command.args or not command.args.isdigit():
            await message.answer(f"Использование: /{command.command} <job_id>")
            return
        job_id = int(command.args)
        ok = await jobs.request_cancel(job_id) if command.command == "cancel" else await jobs.enqueue(job_id)
        if ok and command.command == "retry":
            wakeup.set()
        await message.answer("Готово." if ok else "Операция недоступна для этого задания.")

    @router.callback_query(F.data.startswith("publish:"))
    async def publish(callback: CallbackQuery) -> None:
        job_id = int(callback.data.split(":", 1)[1])
        if await jobs.enqueue(job_id):
            wakeup.set()
            job = await jobs.get(job_id)
            if job is not None and job.status == JobStatus.SCHEDULED:
                await callback.message.edit_text(
                    f"Задание #{job_id} запланировано на "
                    f"{format_schedule_datetime(job.scheduled_at, settings.timezone)}."
                )
            else:
                await callback.message.edit_text(f"Задание #{job_id} добавлено в очередь.")
        else:
            await callback.answer("Не удалось поставить в очередь: проверьте статус и выберите хотя бы одно изображение.", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("repeat:"))
    async def repeat(callback: CallbackQuery) -> None:
        job_id = int(callback.data.split(":", 1)[1])
        if await jobs.allow_duplicate_and_enqueue(job_id):
            wakeup.set()
            job = await jobs.get(job_id)
            if job is not None and job.status == JobStatus.SCHEDULED:
                await callback.message.edit_text(
                    f"Повторная публикация #{job_id} запланирована на "
                    f"{format_schedule_datetime(job.scheduled_at, settings.timezone)}."
                )
            else:
                await callback.message.edit_text(
                    f"Повторная публикация #{job_id} подтверждена и добавлена в очередь."
                )
        else:
            await callback.answer("Не удалось поставить в очередь: проверьте статус и выберите хотя бы одно изображение.", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel(callback: CallbackQuery) -> None:
        job_id = int(callback.data.split(":", 1)[1])
        await jobs.request_cancel(job_id)
        await callback.message.edit_text(f"Задание #{job_id} отменено.")
        await callback.answer()

    @router.callback_query(F.data.startswith("tags:"))
    async def edit_tags(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.split(":", 1)[1])
        await state.set_state(EditPreview.waiting_for_tags)
        await state.set_data({"job_id": job_id, "tag_context": "initial_preview", "tag_mode": "replace"})
        await callback.message.answer(
            "Отправьте новый список тегов через пробел.",
            reply_markup=cancel_tag_input_keyboard(job_id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tags_input_cancel:"))
    async def cancel_tag_input(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        data = await state.get_data()
        await state.clear()
        keyboard = (
            queued_preview_keyboard(job_id)
            if data.get("tag_context") == "queued_preview"
            else preview_keyboard(job_id)
        )
        await callback.message.edit_text("Ввод тегов отменён.", reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data.startswith("channel:"))
    async def edit_channel(callback: CallbackQuery, state: FSMContext) -> None:
        job_id = int(callback.data.split(":", 1)[1])
        job = await jobs.get(job_id)
        if not job or job.status != JobStatus.WAITING_CONFIRMATION:
            await callback.answer("Предпросмотр уже недоступен.", show_alert=True)
            return
        channels = [channel for channel in await jobs.channels() if channel.is_enabled]
        if not channels:
            await callback.answer("Нет доступных каналов.", show_alert=True)
            return
        await state.clear()
        current_page = next(
            (
                index // CHANNELS_PER_PAGE
                for index, channel in enumerate(channels)
                if channel.id == job.target_channel_id
            ),
            0,
        )
        await callback.message.edit_reply_markup(
            reply_markup=channel_selection_keyboard(
                job_id, channels, job.target_channel_id, current_page,
            )
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("channel_select_page:"))
    async def channel_selection_page(callback: CallbackQuery) -> None:
        message = await menu_callback_message(callback)
        if message is None or callback.data is None:
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        job_id, page = int(parts[1]), int(parts[2])
        job = await jobs.get(job_id)
        if not job or job.status != JobStatus.WAITING_CONFIRMATION:
            await callback.answer("Предпросмотр уже недоступен.", show_alert=True)
            return
        channels = [channel for channel in await jobs.channels() if channel.is_enabled]
        if not channels:
            await callback.answer("Нет доступных каналов.", show_alert=True)
            return
        await callback.answer()
        await edit_menu_markup(
            message,
            channel_selection_keyboard(job_id, channels, job.target_channel_id, page),
        )

    @router.callback_query(F.data.startswith("channel_select:"))
    async def select_channel(callback: CallbackQuery) -> None:
        _, raw_job_id, raw_channel_id = callback.data.split(":", 2)
        job_id, channel_id = int(raw_job_id), int(raw_channel_id)
        channel = await jobs.change_channel(job_id, channel_id)
        if not channel:
            await callback.answer("Канал недоступен или предпросмотр уже закрыт.", show_alert=True)
            return
        updated_job = await jobs.get(job_id)
        payload = await confirmation_payload(updated_job) if updated_job is not None else None
        if payload is None:
            await callback.answer("Не удалось обновить предпросмотр.", show_alert=True)
            return
        _, updated_text = payload
        await callback.message.edit_text(
            updated_text, parse_mode="HTML", reply_markup=preview_keyboard(job_id),
        )
        await callback.answer(f"Выбран канал: {channel.alias}")

    @router.callback_query(F.data.startswith("channel_select_cancel:"))
    async def cancel_channel_selection(callback: CallbackQuery) -> None:
        job_id = int(callback.data.rsplit(":", 1)[1])
        await callback.message.edit_reply_markup(reply_markup=preview_keyboard(job_id))
        await callback.answer()

    @router.message(EditPreview.waiting_for_tags)
    async def receive_tags(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job = await jobs.get(data["job_id"])
        expected_statuses = (
            {JobStatus.QUEUED, JobStatus.SCHEDULED}
            if data.get("tag_context") == "queued_preview"
            else {JobStatus.WAITING_CONFIRMATION}
        )
        if not job or job.status not in expected_statuses:
            await state.clear()
            await message.answer("Предпросмотр уже недоступен.")
            return
        tags = normalize_tags((message.text or "").split(), settings.max_tags, settings.max_tag_length)
        if data.get("tag_mode") == "add":
            tags = merge_tags(tags, job.user_tags, settings.max_tags, settings.max_tag_length)
        if getattr(jobs,"gate",None):jobs.gate.assert_mutation_allowed()
        async with jobs.sessions() as session, session.begin():
            stored = await session.get(type(job), job.id)
            if stored.version != job.version or stored.status not in expected_statuses:
                await message.answer("Публикация уже изменилась; откройте редактор повторно.")
                return
            stored.user_tags = tags
        await state.clear()
        combined = effective_tags(tags, job.source_tags, job.channel)
        if data.get("tag_context") != "queued_preview":
            await message.answer(
                f"Теги обновлены: {hashtags(combined) or 'без тегов'}",
                reply_markup=preview_keyboard(job.id),
            )
            return
        try:
            await message.bot.edit_message_text(
                f"Теги задания #{job.id} обновлены. Ниже показан новый предпросмотр.",
                chat_id=data["control_chat_id"], message_id=data["control_message_id"],
            )
        except Exception:
            logger.debug("preview_control_edit_failed job_id=%s", job.id, exc_info=True)
        refreshed = await jobs.get(job.id)
        try:
            await previews.send(refreshed, message.chat.id)
        except ApplicationError as error:
            await message.answer(
                f"Теги сохранены, но обновить предпросмотр не удалось: {error}",
                reply_markup=queued_preview_keyboard(job.id),
            )
            return
        await message.answer(
            f"Теги обновлены: {hashtags(combined) or 'без тегов'}",
            reply_markup=queued_preview_keyboard(job.id),
        )

    @router.message(EditPreview.waiting_for_caption)
    async def receive_caption(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        job_id = data.get("job_id")
        if not isinstance(job_id, int):
            await state.clear()
            await message.answer("Редактирование подписи устарело. Откройте предпросмотр заново.")
            return
        value = (message.text or "").strip()
        try:
            previews.validate_custom_caption(value)
        except ApplicationError as error:
            await message.answer(str(error), reply_markup=caption_input_keyboard(job_id))
            return
        job = await jobs.set_caption_override(job_id, value)
        if job is None:
            await state.clear()
            await message.answer("Подпись этого задания уже нельзя изменить.")
            return
        queued = data.get("edit_context") == "queued_preview"
        await state.clear()
        await message.answer("Подпись обновлена. Проверьте итоговый вариант:")
        await send_confirmation(message, job, queued=queued)

    async def receive_post_field_value(message: Message, state: FSMContext, field: str) -> None:
        data = await state.get_data()
        job_id = data.get("job_id")
        if not isinstance(job_id, int) or data.get("post_field") != field:
            await state.clear()
            await message.answer("Редактирование устарело. Откройте предпросмотр заново.")
            return
        value = (message.text or "").strip()
        if field == "description" and value == "-":
            value = ""
        if field == "title" and not value:
            await message.answer(
                "Заголовок не может быть пустым.",
                reply_markup=post_field_input_keyboard(job_id, field),
            )
            return
        limit = 300 if field == "title" else 4000
        if len(value) > limit:
            await message.answer(
                f"Слишком длинный текст: {len(value)} из {limit} символов.",
                reply_markup=post_field_input_keyboard(job_id, field),
            )
            return
        job = await jobs.set_post_field(job_id, field, value)
        if job is None:
            await state.clear()
            await message.answer("Метаданные этого задания уже нельзя изменить.")
            return
        await state.clear()
        await message.answer("Данные обновлены. Проверьте итоговый вариант:")
        await send_confirmation(message, job)

    @router.message(EditPreview.waiting_for_title)
    async def receive_title(message: Message, state: FSMContext) -> None:
        await receive_post_field_value(message, state, "title")

    @router.message(EditPreview.waiting_for_description)
    async def receive_description(message: Message, state: FSMContext) -> None:
        await receive_post_field_value(message, state, "description")

    @router.message(F.text)
    async def submission(message: Message, state: FSMContext) -> None:
        try:
            urls, tags, alias = ingest.parse(message.text)
            artwork_urls: list[str] = []
            news_requests: list[NewsSourceRequest] = []
            for source in urls:
                url, selection = split_source_selection(source)
                try:
                    if hasattr(registry, "resolve"):
                        registry.resolve(url)
                except UnsupportedSourceError:
                    if selection is not None:
                        await message.answer(
                            "Выбор отдельных медиа в квадратных скобках доступен только "
                            "для художественных источников."
                        )
                        return
                    news_requests.append(classify_news_input(url))
                else:
                    artwork_urls.append(source)
            if artwork_urls and news_requests:
                await message.answer(
                    "Ссылки на новости и изображения отправьте отдельными сообщениями."
                )
                return
            if news_requests:
                await state.clear()
                await queue_news_requests(message, news_requests, tags, alias)
                return
            if alias is None and not tags:
                await begin_wizard(message, state, artwork_urls, tags)
                return
            user = await jobs.ensure_user(
                message.from_user.id, message.from_user.username, message.from_user.full_name,
            )
            channel = (
                await jobs.get_channel(alias)
                if alias
                else await jobs.get_preferred_channel(user.id, settings.default_channel_alias)
            )
            if not channel:
                await message.answer("Канал не найден или отключён. Проверьте --channel и CHANNELS_JSON.")
                return
            posts, failures = await fetch_posts(artwork_urls)
            await create_preview_messages(
                message, user.id, posts, channel, tags, len(artwork_urls),
            )
            await send_failures(message, failures, len(artwork_urls))
        except ApplicationError as error:
            await message.answer(str(error))
        except Exception:
            await message.answer("Не удалось получить публикацию. Подробности записаны в лог.")
            raise

    return router
