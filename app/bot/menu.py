from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
    ReplyKeyboardMarkup,
)

from app.db.models import Channel

ADD_BUTTON = "➕ Добавить публикацию"
NEWS_BUTTON = "📰 Добавить новость"
QUEUE_BUTTON = "📋 Очередь"
PREVIEW_BUTTON = "🖼 Следующий пост"
STATS_BUTTON = "📊 Статистика"
CHANNELS_BUTTON = "📡 Каналы"
HEALTH_BUTTON = "🩺 Здоровье"
HELP_BUTTON = "ℹ️ Помощь"

CHANNELS_PER_PAGE = 10
BACK_CALLBACK = "menu:back"
QUEUE_CALLBACK_PREFIX = "menu:queue:"
QUEUE_PAGE_CALLBACK_PREFIX = "menu:queue_page:"
PREVIEW_CALLBACK_PREFIX = "menu:preview:"
PREVIEW_PAGE_CALLBACK_PREFIX = "menu:preview_page:"
CHANNEL_CALLBACK_PREFIX = "menu:channel:"
CHANNEL_PAGE_CALLBACK_PREFIX = "menu:channel_page:"
HEALTH_CALLBACK_PREFIX = "menu:health:"

MAIN_MENU_TEXT = (
    "🎛 Главное меню\n\n"
    "Выберите действие или отправьте ссылку на публикацию."
)


@dataclass(frozen=True)
class CommandHelp:
    command: str
    usage: str
    summary: str
    details: str
    section: str


COMMAND_CATALOG = (
    CommandHelp("start", "/start", "Открыть главное меню", "открывает главное меню и постоянную клавиатуру.", "Навигация"),
    CommandHelp("menu", "/menu", "Открыть главное меню", "повторно показывает главное меню.", "Навигация"),
    CommandHelp("help", "/help", "Показать памятку", "показывает актуальную памятку по всем командам.", "Навигация"),
    CommandHelp("new", "/new", "Добавить публикацию", "запускает пошаговый мастер публикации изображений.", "Навигация"),
    CommandHelp("news", "/news", "Добавить новость", "принимает статью, YouTube, t.me-ссылку, пересланный пост или ручной текст и отправляет его домашнему AI-worker.", "Навигация"),
    CommandHelp("queue", "/queue [alias]", "Показать очередь", "без аргумента показывает общую очередь; с alias — очередь канала, число постов и время до её завершения.", "Очередь и публикации"),
    CommandHelp("preview", "/preview [job_id|alias]", "Предпросмотр следующего поста", "без аргумента показывает ближайший ожидающий или запланированный пост; с ID — указанное задание; с alias — ближайший пост канала.", "Очередь и публикации"),
    CommandHelp("publish", "/publish [job_id]", "Опубликовать без ожидания", "ставит ближайшее или указанное ожидающее/запланированное задание на ручную публикацию.", "Очередь и публикации"),
    CommandHelp("status", "/status <job_id>", "Показать состояние задания", "показывает текущий статус задания по его ID.", "Очередь и публикации"),
    CommandHelp("cancel", "/cancel <job_id>", "Отменить задание", "отменяет задание, если его текущая стадия это допускает.", "Очередь и публикации"),
    CommandHelp("retry", "/retry <job_id>", "Повторить задание", "повторно ставит доступное для повтора задание в очередь.", "Очередь и публикации"),
    CommandHelp("recent", "/recent", "Показать последние задания", "показывает недавние задания и их статусы.", "Очередь и публикации"),
    CommandHelp("channels", "/channels", "Показать каналы", "показывает зарегистрированные каналы и интервалы публикации.", "Каналы и источники"),
    CommandHelp("channel_interval", "/channel_interval <alias> <30s|15m|2h|1d|0>", "Изменить интервал канала", "задаёт интервал автопубликации; 0 отключает ожидание между постами.", "Каналы и источники"),
    CommandHelp("providers", "/providers", "Показать источники", "показывает поддерживаемые источники изображений.", "Каналы и источники"),
    CommandHelp("models", "/models", "Выбрать модель LM Studio", "показывает модели LM Studio и сохраняет выбор кнопкой.", "Диагностика"),
    CommandHelp("stats", "/stats", "Показать статистику", "показывает текущую статистику заданий.", "Диагностика"),
    CommandHelp("health", "/health [full]", "Проверить здоровье бота", "без аргумента выполняет быструю проверку; full добавляет каналы, storage, размер БД и внешние providers.", "Диагностика"),
)


def bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command=item.command, description=item.summary)
        for item in COMMAND_CATALOG
    ]


async def configure_bot_ui(bot: Bot, *, panel_url: str | None = None) -> None:
    if panel_url:
        from urllib.parse import urlsplit
        parsed=urlsplit(panel_url)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Telegram Mini App требует проверенный HTTPS-адрес панели без пароля и fragment")
    await bot.set_my_commands(bot_commands())
    desired=MenuButtonWebApp(text="Открыть Publisher",web_app=WebAppInfo(url=panel_url)) if panel_url else MenuButtonCommands()
    if panel_url:
        current=await bot.get_chat_menu_button()
        if isinstance(current,MenuButtonWebApp) and current.web_app.url==panel_url:return
    await bot.set_chat_menu_button(menu_button=desired)


def render_help() -> str:
    lines = [
        "📚 <b>Памятка по командам</b>",
        "",
        "<b>Добавление публикации</b>",
        "Отправьте ссылку без дополнительных параметров или используйте <code>/new</code> — бот предложит выбрать канал кнопкой.",
        "Для альбома Pixiv или DeviantArt можно выбрать изображения после ссылки: <code>URL [1,3,5-7]</code>. Нумерация начинается с 1.",
        "",
        "<b>Быстрый режим</b>",
        "<code>URL [1,3,5-7] теги --channel alias</code>",
        "Можно передать несколько ссылок; теги и выбранный канал применятся ко всем без запуска мастера.",
    ]
    sections = dict.fromkeys(item.section for item in COMMAND_CATALOG)
    for section in sections:
        lines.extend(("", f"<b>{escape(section)}</b>"))
        lines.extend(
            f"<code>{escape(item.usage)}</code> — {escape(item.details)}"
            for item in COMMAND_CATALOG
            if item.section == section
        )
    lines.extend((
        "",
        "<i>[аргумент]</i> — необязательный, <i>&lt;аргумент&gt;</i> — обязательный.",
    ))
    return "\n".join(lines)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADD_BUTTON), KeyboardButton(text=NEWS_BUTTON)],
            [KeyboardButton(text=QUEUE_BUTTON), KeyboardButton(text=PREVIEW_BUTTON)],
            [KeyboardButton(text=STATS_BUTTON), KeyboardButton(text=CHANNELS_BUTTON)],
            [KeyboardButton(text=HEALTH_BUTTON), KeyboardButton(text=HELP_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Отправьте ссылку или выберите действие",
    )


def queue_menu_keyboard(channels: Sequence[Channel], page: int = 0) -> InlineKeyboardMarkup:
    visible, page, page_count = paginate_channels(
        [channel for channel in channels if channel.is_enabled], page,
    )
    rows = [[InlineKeyboardButton(
        text="🌐 Все каналы", callback_data=f"{QUEUE_CALLBACK_PREFIX}{page}:all",
    )]]
    rows.extend([
        InlineKeyboardButton(
            text=_channel_button_text(channel, "📥"),
            callback_data=f"{QUEUE_CALLBACK_PREFIX}{page}:{channel.id}",
        )
    ] for channel in visible)
    navigation = pagination_row(page, page_count, QUEUE_PAGE_CALLBACK_PREFIX)
    if navigation:
        rows.append(navigation)
    rows.append(_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def preview_menu_keyboard(channels: Sequence[Channel], page: int = 0) -> InlineKeyboardMarkup:
    visible, page, page_count = paginate_channels(
        [channel for channel in channels if channel.is_enabled], page,
    )
    rows = [[InlineKeyboardButton(
        text="⏭ Ближайший пост", callback_data=f"{PREVIEW_CALLBACK_PREFIX}{page}:next",
    )]]
    rows.extend([
        InlineKeyboardButton(
            text=_channel_button_text(channel, "🖼"),
            callback_data=f"{PREVIEW_CALLBACK_PREFIX}{page}:{channel.id}",
        )
    ] for channel in visible)
    navigation = pagination_row(page, page_count, PREVIEW_PAGE_CALLBACK_PREFIX)
    if navigation:
        rows.append(navigation)
    rows.append(_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channels_menu_keyboard(channels: Sequence[Channel], page: int = 0) -> InlineKeyboardMarkup:
    visible, page, page_count = paginate_channels(channels, page)
    rows = [[
        InlineKeyboardButton(
            text=_channel_button_text(
                channel,
                "⏸" if channel.is_enabled and channel.is_paused
                else "🟢" if channel.is_enabled else "⚪",
            ),
            callback_data=f"{CHANNEL_CALLBACK_PREFIX}{page}:{channel.id}",
        )
    ] for channel in visible]
    navigation = pagination_row(page, page_count, CHANNEL_PAGE_CALLBACK_PREFIX)
    if navigation:
        rows.append(navigation)
    rows.append(_back_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def health_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"{HEALTH_CALLBACK_PREFIX}refresh"),
            InlineKeyboardButton(text="🔬 Полная проверка", callback_data=f"{HEALTH_CALLBACK_PREFIX}full"),
        ],
        _back_row(),
    ])


def back_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_back_row()])


def paginate_channels(
    channels: Sequence[Channel], page: int,
) -> tuple[list[Channel], int, int]:
    page_count = max(1, (len(channels) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
    page = min(max(page, 0), page_count - 1)
    start = page * CHANNELS_PER_PAGE
    return list(channels[start:start + CHANNELS_PER_PAGE]), page, page_count


def pagination_row(
    page: int, page_count: int, callback_prefix: str,
) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(InlineKeyboardButton(
            text="⬅️ Предыдущая страница",
            callback_data=f"{callback_prefix}{page - 1}",
        ))
    if page + 1 < page_count:
        buttons.append(InlineKeyboardButton(
            text="Следующая страница ➡️",
            callback_data=f"{callback_prefix}{page + 1}",
        ))
    return buttons


def _back_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ Назад", callback_data=BACK_CALLBACK)]


def _channel_button_text(channel: Channel, icon: str) -> str:
    return f"{icon} {channel.alias} — {channel.title}"[:64]
