import ast
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, MenuButtonCommands, Message, User

from app.bot.menu import (
    ADD_BUTTON,
    BACK_CALLBACK,
    CHANNELS_BUTTON,
    COMMAND_CATALOG,
    HEALTH_BUTTON,
    HELP_BUTTON,
    NEWS_BUTTON,
    PREVIEW_BUTTON,
    QUEUE_BUTTON,
    STATS_BUTTON,
    bot_commands,
    channels_menu_keyboard,
    configure_bot_ui,
    health_menu_keyboard,
    main_menu_keyboard,
    preview_menu_keyboard,
    queue_menu_keyboard,
    render_help,
)
from app.bot.router import build_router
from app.bot.states import CreatePublication, EditPreview
from app.db.models import Channel
from app.domain.enums import JobStatus
from app.domain.exceptions import UnsupportedSourceError
from app.domain.models import MediaItem, SourcePost
from app.news.models import NewsSourceKind
from app.services.health_service import HealthReport
from app.services.job_service import serialize_post


def make_channels() -> list[Channel]:
    return [
        Channel(
            id=7, alias="artwork", telegram_chat_id="-1001",
            title="Artwork", is_enabled=True,
        ),
        Channel(
            id=8, alias="archive", telegram_chat_id="-1002",
            title="Archive", is_enabled=False,
        ),
    ]


def make_many_channels(count: int) -> list[Channel]:
    return [
        Channel(
            id=index,
            alias=f"channel-{index:02d}",
            telegram_chat_id=f"-{1000 + index}",
            title=f"Channel {index:02d}",
            is_enabled=True,
        )
        for index in range(1, count + 1)
    ]


def flatten(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


class RecordingBot:
    def __init__(self) -> None:
        self.requests = []

    async def __call__(self, method, request_timeout=None):
        self.requests.append(method)
        return True


def make_message(bot: RecordingBot, text: str = "menu") -> Message:
    return Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        text=text,
    ).as_(bot)


def make_callback(bot: RecordingBot, message: Message, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="callback-1",
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        chat_instance="private-1",
        message=message,
        data=data,
    ).as_(bot)


def make_router(
    jobs, *, health=None, channel_stats=None, ingest=None, previews=None,
    translator=None, registry=None, news_submissions=None, news_tasks=None,
):
    health = health or SimpleNamespace(check=AsyncMock())
    empty_subscriber_stats = SimpleNamespace(
        count=None, captured_at=None, day=None, week=None, month=None, error=None,
    )
    channel_stats = channel_stats or SimpleNamespace(
        capture_for_display=AsyncMock(return_value=empty_subscriber_stats),
        summary=AsyncMock(return_value=empty_subscriber_stats),
    )
    settings = SimpleNamespace(
        auto_add_source_tags=True,
        max_tags=20,
        max_tag_length=64,
        max_job_attempts=3,
        default_channel_alias="artwork",
        timezone="Asia/Vladivostok",
    )
    return build_router(
        ingest=ingest or SimpleNamespace(),
        jobs=jobs,
        previews=previews or SimpleNamespace(send=AsyncMock()),
        translator=translator or SimpleNamespace(),
        health=health,
        channel_stats=channel_stats,
        wakeup=SimpleNamespace(set=Mock()),
        registry=registry or SimpleNamespace(names=("pixiv", "deviantart", "direct")),
        settings=settings,
        news_submissions=news_submissions,
        news_tasks=news_tasks,
    )


def make_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=1, user_id=1),
    )


def make_post() -> SourcePost:
    return SourcePost(
        provider="direct",
        source_id="image-1",
        source_url="https://example.com/image.jpg",
        normalized_url="https://example.com/image.jpg",
        title="Image",
        author_name="example.com",
        author_url="https://example.com",
        media_items=[MediaItem(
            url="https://example.com/image.jpg", filename="image.jpg", order=0,
        )],
    )


def handler(router, observer: str, name: str):
    handlers = getattr(router, observer).handlers
    return next(item.callback for item in handlers if item.callback.__name__ == name)


def test_main_menu_has_requested_persistent_layout():
    markup = main_menu_keyboard()

    assert [[button.text for button in row] for row in markup.keyboard] == [
        [ADD_BUTTON, NEWS_BUTTON],
        [QUEUE_BUTTON, PREVIEW_BUTTON],
        [STATS_BUTTON, CHANNELS_BUTTON],
        [HEALTH_BUTTON, HELP_BUTTON],
    ]
    assert markup.resize_keyboard is True
    assert markup.is_persistent is True
    assert markup.one_time_keyboard is False


def test_queue_menu_lists_all_and_only_enabled_channel_buttons():
    buttons = flatten(queue_menu_keyboard(make_channels()))

    assert [button.text for button in buttons] == [
        "🌐 Все каналы",
        "📥 artwork — Artwork",
        "⬅️ Назад",
    ]
    assert [button.callback_data for button in buttons] == [
        "menu:queue:0:all",
        "menu:queue:0:7",
        BACK_CALLBACK,
    ]


def test_preview_menu_lists_nearest_and_only_enabled_channels():
    buttons = flatten(preview_menu_keyboard(make_channels()))

    assert [button.text for button in buttons] == [
        "⏭ Ближайший пост",
        "🖼 artwork — Artwork",
        "⬅️ Назад",
    ]
    assert [button.callback_data for button in buttons] == [
        "menu:preview:0:next",
        "menu:preview:0:7",
        BACK_CALLBACK,
    ]


def test_channels_menu_lists_enabled_and_disabled_registered_channels():
    buttons = flatten(channels_menu_keyboard(make_channels()))

    assert [button.text for button in buttons] == [
        "🟢 artwork — Artwork",
        "⚪ archive — Archive",
        "⬅️ Назад",
    ]
    assert [button.callback_data for button in buttons] == [
        "menu:channel:0:7",
        "menu:channel:0:8",
        BACK_CALLBACK,
    ]


def test_queue_channel_list_is_paginated_by_ten():
    channels = make_many_channels(23)

    first = flatten(queue_menu_keyboard(channels, page=0))
    middle = flatten(queue_menu_keyboard(channels, page=1))
    last = flatten(queue_menu_keyboard(channels, page=2))

    assert sum(
        button.callback_data.startswith("menu:queue:")
        and button.callback_data.rsplit(":", 1)[-1].isdigit()
        for button in first
    ) == 10
    assert "menu:queue_page:1" in [button.callback_data for button in first]
    assert "menu:queue_page:0" in [button.callback_data for button in middle]
    assert "menu:queue_page:2" in [button.callback_data for button in middle]
    assert [
        button.callback_data for button in last
        if button.callback_data.startswith("menu:queue:2:")
        and button.callback_data.rsplit(":", 1)[-1].isdigit()
    ] == ["menu:queue:2:21", "menu:queue:2:22", "menu:queue:2:23"]
    assert "menu:queue_page:1" in [button.callback_data for button in last]
    assert "menu:queue_page:3" not in [button.callback_data for button in last]


def test_preview_and_registered_channel_lists_use_the_requested_page():
    channels = make_many_channels(12)
    channels[10].is_enabled = False

    preview_buttons = flatten(preview_menu_keyboard(channels, page=1))
    channel_buttons = flatten(channels_menu_keyboard(channels, page=1))

    assert [
        button.callback_data for button in preview_buttons
        if button.callback_data.startswith("menu:preview:1:")
    ] == ["menu:preview:1:next", "menu:preview:1:12"]
    assert [
        button.callback_data for button in channel_buttons
        if button.callback_data.startswith("menu:channel:1:")
    ] == ["menu:channel:1:11", "menu:channel:1:12"]
    assert channel_buttons[0].text.startswith("⚪ channel-11")


def test_exactly_ten_channels_do_not_add_page_navigation():
    callbacks = [
        button.callback_data
        for button in flatten(queue_menu_keyboard(make_many_channels(10)))
    ]

    assert not any(callback.startswith("menu:queue_page:") for callback in callbacks)


def test_health_menu_has_refresh_full_and_back_actions():
    buttons = flatten(health_menu_keyboard())

    assert [button.text for button in buttons] == [
        "🔄 Обновить",
        "🔬 Полная проверка",
        "⬅️ Назад",
    ]
    assert [button.callback_data for button in buttons] == [
        "menu:health:refresh",
        "menu:health:full",
        BACK_CALLBACK,
    ]


def test_menu_callback_data_stays_within_telegram_limit():
    markups = [
        queue_menu_keyboard(make_channels()),
        preview_menu_keyboard(make_channels()),
        channels_menu_keyboard(make_channels()),
        health_menu_keyboard(),
    ]

    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for markup in markups
        for button in flatten(markup)
    )


def test_help_catalog_exactly_covers_router_slash_commands():
    router_paths = [
        Path(__file__).parents[1] / "app" / "bot" / "router.py",
        Path(__file__).parents[1] / "app" / "bot" / "news_ui.py",
        Path(__file__).parents[1] / "app" / "bot" / "model_ui.py",
    ]
    routed_commands = {
        argument.value
        for router_path in router_paths
        for node in ast.walk(ast.parse(router_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Command"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }

    catalog_commands = {item.command for item in COMMAND_CATALOG}

    assert catalog_commands == routed_commands


def test_help_and_telegram_commands_use_the_same_catalog():
    help_text = render_help()
    commands = bot_commands()

    assert len(help_text) <= 4096
    assert [command.command for command in commands] == [
        item.command for item in COMMAND_CATALOG
    ]
    assert all(1 <= len(command.description) <= 256 for command in commands)
    assert all(f"<code>{escape(item.usage)}</code>" in help_text for item in COMMAND_CATALOG)


async def test_configure_bot_ui_registers_commands_and_command_menu_button():
    bot = SimpleNamespace(
        set_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
    )

    await configure_bot_ui(bot)

    bot.set_my_commands.assert_awaited_once()
    registered = bot.set_my_commands.await_args.args[0]
    assert [command.command for command in registered] == [
        item.command for item in COMMAND_CATALOG
    ]
    bot.set_chat_menu_button.assert_awaited_once()
    assert isinstance(
        bot.set_chat_menu_button.await_args.kwargs["menu_button"],
        MenuButtonCommands,
    )


async def test_queue_callback_opens_filtered_management_screen():
    channels = make_many_channels(12)
    jobs = SimpleNamespace(
        channels=AsyncMock(return_value=channels),
        get_channel_by_id=AsyncMock(return_value=channels[-1]),
        managed_queue=AsyncMock(return_value=[]),
    )
    router = make_router(jobs)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "menu:queue:1:12")

    await handler(router, "callback_query", "queue_menu_selection")(callback)

    jobs.get_channel_by_id.assert_awaited_once_with(12)
    jobs.managed_queue.assert_awaited_once_with(12, "active", limit=None)
    assert any(isinstance(request, AnswerCallbackQuery) for request in bot.requests)
    edit = next(request for request in bot.requests if isinstance(request, EditMessageText))
    assert "Очередь: channel-12" in edit.text
    assert "Всего ожидают: 0 · Вся очередь: —" in edit.text
    assert "Заданий с таким статусом нет" in edit.text
    assert "queue_filter:1:12:active" in [
        button.callback_data for button in flatten(edit.reply_markup)
    ]


async def test_opening_registered_channel_captures_and_renders_subscribers():
    channel = make_channels()[0]
    channel.publish_mode = "auto"
    channel.publish_interval_seconds = 0
    channel.is_paused = False
    channel.is_default = True
    jobs = SimpleNamespace(
        get_channel_by_id=AsyncMock(return_value=channel),
        managed_queue=AsyncMock(return_value=[]),
    )
    subscriber_stats = SimpleNamespace(
        count=1234,
        captured_at=datetime.now(UTC),
        day=SimpleNamespace(value=12, percent=0.98),
        week=SimpleNamespace(value=-5, percent=-0.4),
        month=None,
        error=None,
    )
    channel_stats = SimpleNamespace(
        capture_for_display=AsyncMock(return_value=subscriber_stats),
        summary=AsyncMock(return_value=subscriber_stats),
    )
    router = make_router(jobs, channel_stats=channel_stats)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "menu:channel:0:7")

    await handler(router, "callback_query", "registered_channel_details")(callback)

    channel_stats.capture_for_display.assert_awaited_once_with(channel)
    edit = next(request for request in bot.requests if isinstance(request, EditMessageText))
    assert "Подписчики: <b>1 234</b>" in edit.text
    assert "24ч +12 (+0,98%)" in edit.text
    assert "7д -5 (-0,40%)" in edit.text
    assert "30д —" in edit.text
    assert "0 запланировано" in edit.text
    assert "Всего ожидают: 0 · Вся очередь: —" in edit.text


async def test_shuffle_queue_callback_affects_selected_channel_and_refreshes_screen():
    channel = make_channels()[0]
    jobs = SimpleNamespace(
        get_channel_by_id=AsyncMock(return_value=channel),
        shuffle_queued=AsyncMock(return_value=3),
        managed_queue=AsyncMock(return_value=[]),
    )
    router = make_router(jobs)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "queue_shuffle:0:7:active")

    await handler(router, "callback_query", "shuffle_channel_queue")(callback)

    jobs.shuffle_queued.assert_awaited_once_with(7)
    jobs.managed_queue.assert_awaited_once_with(7, "active", limit=None)
    assert any(
        isinstance(request, AnswerCallbackQuery) and "Перемешано заданий: 3" in request.text
        for request in bot.requests
    )


async def test_queue_page_callback_replaces_markup_with_requested_page():
    jobs = SimpleNamespace(channels=AsyncMock(return_value=make_many_channels(12)))
    router = make_router(jobs)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "menu:queue_page:1")

    await handler(router, "callback_query", "queue_menu_page")(callback)

    edit = next(
        request for request in bot.requests
        if isinstance(request, EditMessageReplyMarkup)
    )
    assert [button.callback_data for button in flatten(edit.reply_markup)] == [
        "menu:queue:1:all",
        "menu:queue:1:11",
        "menu:queue:1:12",
        "menu:queue_page:0",
        BACK_CALLBACK,
    ]


async def test_preview_callback_starts_loading_only_after_channel_selection():
    jobs = SimpleNamespace(
        channels=AsyncMock(return_value=make_channels()),
        queue=AsyncMock(return_value=[]),
    )
    router = make_router(jobs)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "menu:preview:0:7")

    await handler(router, "callback_query", "preview_menu_selection")(callback)

    jobs.queue.assert_awaited_once_with("artwork", limit=None)
    edits = [request for request in bot.requests if isinstance(request, EditMessageText)]
    answers = [request for request in bot.requests if isinstance(request, SendMessage)]
    assert edits[0].text == "⏳ Ищу ближайший пост · канал artwork"
    assert answers[-1].text == "В очереди канала artwork нет заданий для предпросмотра."


async def test_change_channel_page_callback_uses_second_batch_of_ten():
    jobs = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(
            id=42,
            status=JobStatus.WAITING_CONFIRMATION,
            target_channel_id=15,
        )),
        channels=AsyncMock(return_value=make_many_channels(23)),
    )
    router = make_router(jobs)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "channel_select_page:42:1")

    await handler(router, "callback_query", "channel_selection_page")(callback)

    edit = next(
        request for request in bot.requests
        if isinstance(request, EditMessageReplyMarkup)
    )
    assert [
        button.callback_data for button in flatten(edit.reply_markup)
        if button.callback_data.startswith("channel_select:42:")
    ] == [f"channel_select:42:{index}" for index in range(11, 21)]


async def test_health_full_callback_runs_full_check_and_restores_controls():
    jobs = SimpleNamespace()
    health = SimpleNamespace(check=AsyncMock(return_value=HealthReport(
        healthy=True,
        uptime_seconds=3600,
        elapsed_ms=12,
        lines=(),
    )))
    router = make_router(jobs, health=health)
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), "menu:health:full")

    await handler(router, "callback_query", "health_menu_selection")(callback)

    health.check.assert_awaited_once_with(full=True)
    edits = [request for request in bot.requests if isinstance(request, EditMessageText)]
    assert edits[0].text == "⏳ Выполняю полную проверку здоровья бота…"
    assert "Bot healthy" in edits[-1].text
    assert [button.callback_data for button in flatten(edits[-1].reply_markup)] == [
        "menu:health:refresh",
        "menu:health:full",
        BACK_CALLBACK,
    ]


async def test_back_callback_reinstalls_persistent_main_keyboard():
    router = make_router(SimpleNamespace())
    bot = RecordingBot()
    callback = make_callback(bot, make_message(bot), BACK_CALLBACK)

    await handler(router, "callback_query", "return_to_main_menu")(callback)

    assert any(isinstance(request, DeleteMessage) for request in bot.requests)
    answer = next(request for request in bot.requests if isinstance(request, SendMessage))
    assert answer.text.startswith("🎛 Главное меню")
    assert answer.reply_markup.is_persistent is True


async def test_stats_button_runs_existing_stats_action():
    jobs = SimpleNamespace(stats=AsyncMock(return_value={"queued": 3, "failed": 1}))
    router = make_router(jobs)
    bot = RecordingBot()
    message = make_message(bot, STATS_BUTTON)

    await handler(router, "message", "stats_button")(message)

    jobs.stats.assert_awaited_once()
    answer = next(request for request in bot.requests if isinstance(request, SendMessage))
    assert answer.text == "failed: 1\nqueued: 3"


async def test_new_publication_button_starts_url_step():
    router = make_router(SimpleNamespace())
    bot = RecordingBot()
    message = make_message(bot, ADD_BUTTON)
    state = make_state()

    await handler(router, "message", "new_publication")(message, state)

    assert await state.get_state() == CreatePublication.waiting_for_urls.state
    answer = next(request for request in bot.requests if isinstance(request, SendMessage))
    assert answer.text.startswith("Шаг 1 из 3")
    assert answer.reply_markup.inline_keyboard[0][0].callback_data == "wizard_cancel"


async def test_plain_url_starts_wizard_and_requests_channel():
    post = make_post()
    ingest = SimpleNamespace(
        parse=Mock(return_value=([post.source_url], [], None)),
        fetch=AsyncMock(return_value=post),
    )
    translator = SimpleNamespace(enrich_title=AsyncMock())
    jobs = SimpleNamespace(
        ensure_user=AsyncMock(return_value=SimpleNamespace(id=12)),
        channels=AsyncMock(return_value=make_channels()),
    )
    router = make_router(jobs, ingest=ingest, translator=translator)
    bot = RecordingBot()
    message = make_message(bot, post.source_url)
    state = make_state()

    await handler(router, "message", "submission")(message, state)

    assert await state.get_state() == CreatePublication.waiting_for_channel.state
    data = await state.get_data()
    assert data["wizard_user_id"] == 12
    assert data["wizard_posts"][0]["post"]["source_id"] == "image-1"
    answers = [request for request in bot.requests if isinstance(request, SendMessage)]
    assert answers[-1].text.startswith("Шаг 2 из 3")
    assert [
        button.callback_data
        for row in answers[-1].reply_markup.inline_keyboard
        for button in row
    ] == ["wizard_channel:0:7", "wizard_cancel"]


async def test_direct_news_url_is_queued_for_home_worker():
    url = "https://example.com/news/story"
    ingest = SimpleNamespace(parse=Mock(return_value=([url], ["важное"], None)))
    registry = SimpleNamespace(
        names=("pixiv", "deviantart", "direct"),
        resolve=Mock(side_effect=UnsupportedSourceError("unsupported")),
    )
    queued = SimpleNamespace(
        task=SimpleNamespace(id=91),
        channel=SimpleNamespace(alias="news"),
    )
    submissions = SimpleNamespace(create=AsyncMock(return_value=queued))
    tasks = SimpleNamespace(set_status_message=AsyncMock())
    router = make_router(
        SimpleNamespace(),
        ingest=ingest,
        registry=registry,
        news_submissions=submissions,
        news_tasks=tasks,
    )

    class NewsBot(RecordingBot):
        async def __call__(self, method, request_timeout=None):
            self.requests.append(method)
            return SimpleNamespace(message_id=77)

    bot = NewsBot()
    message = make_message(bot, url)
    state = make_state()

    await handler(router, "message", "submission")(message, state)

    request = submissions.create.await_args.kwargs["request"]
    assert request.kind == NewsSourceKind.WEBSITE
    assert request.value == url
    assert submissions.create.await_args.kwargs["user_tags"] == ["важное"]
    tasks.set_status_message.assert_awaited_once_with(91, 77)


async def test_wizard_channel_selection_creates_editable_confirmation():
    post = make_post()
    channel = make_channels()[0]
    created = SimpleNamespace(id=42)
    stored = SimpleNamespace(
        id=42,
        post_data=serialize_post(post),
        user_tags=[],
        source_tags=[],
        channel=channel,
    )
    jobs = SimpleNamespace(
        channels=AsyncMock(return_value=[channel]),
        create_preview=AsyncMock(return_value=created),
        duplicate_state_for=AsyncMock(return_value=None),
        get=AsyncMock(return_value=stored),
    )
    previews = SimpleNamespace(caption=AsyncMock(return_value="Final caption"))
    router = make_router(jobs, previews=previews)
    bot = RecordingBot()
    message = make_message(bot, "choose channel")
    callback = make_callback(bot, message, "wizard_channel:0:7")
    state = make_state()
    await state.set_state(CreatePublication.waiting_for_channel)
    await state.set_data({
        "wizard_user_id": 12,
        "wizard_posts": [{"position": 1, "post": serialize_post(post)}],
        "wizard_tags": [],
        "wizard_total": 1,
    })

    await handler(router, "callback_query", "wizard_channel_selection")(callback, state)

    assert await state.get_state() is None
    jobs.create_preview.assert_awaited_once()
    answers = [request for request in bot.requests if isinstance(request, SendMessage)]
    assert "<b>Итоговая подпись</b>\nFinal caption" in answers[-1].text
    callbacks = [
        button.callback_data
        for row in answers[-1].reply_markup.inline_keyboard
        for button in row
    ]
    assert "caption:42" in callbacks
    assert "publish:42" in callbacks


async def test_custom_caption_is_saved_and_returned_to_confirmation():
    post = make_post()
    channel = make_channels()[0]
    updated = SimpleNamespace(id=42)
    stored = SimpleNamespace(
        id=42,
        post_data=serialize_post(post),
        user_tags=[],
        source_tags=[],
        channel=channel,
    )
    jobs = SimpleNamespace(
        set_caption_override=AsyncMock(return_value=updated),
        get=AsyncMock(return_value=stored),
    )
    previews = SimpleNamespace(
        validate_custom_caption=Mock(),
        caption=AsyncMock(return_value="Custom &amp; safe"),
    )
    router = make_router(jobs, previews=previews)
    bot = RecordingBot()
    message = make_message(bot, "Custom & safe")
    state = make_state()
    await state.set_state(EditPreview.waiting_for_caption)
    await state.set_data({"job_id": 42, "edit_context": "initial_preview"})

    await handler(router, "message", "receive_caption")(message, state)

    previews.validate_custom_caption.assert_called_once_with("Custom & safe")
    jobs.set_caption_override.assert_awaited_once_with(42, "Custom & safe")
    assert await state.get_state() is None
    answers = [request for request in bot.requests if isinstance(request, SendMessage)]
    assert "Custom &amp; safe" in answers[-1].text
    assert answers[-1].parse_mode == "HTML"


async def test_initial_preview_schedule_is_saved_and_shown_in_confirmation():
    post = make_post()
    channel = make_channels()[0]
    scheduled_at = datetime(2099, 12, 31, 8, 30, tzinfo=UTC)
    updated = SimpleNamespace(id=42)
    stored = SimpleNamespace(
        id=42,
        scheduled_at=scheduled_at,
        post_data=serialize_post(post),
        user_tags=[],
        source_tags=[],
        channel=channel,
    )
    jobs = SimpleNamespace(
        set_schedule=AsyncMock(return_value=updated),
        get=AsyncMock(return_value=stored),
    )
    previews = SimpleNamespace(caption=AsyncMock(return_value="Caption"))
    router = make_router(jobs, previews=previews)
    bot = RecordingBot()
    message = make_message(bot, "31.12.2099 18:30")
    state = make_state()
    await state.set_state(EditPreview.waiting_for_schedule)
    await state.set_data({"job_id": 42, "schedule_context": "initial"})

    await handler(router, "message", "receive_preview_schedule")(message, state)

    saved_time = jobs.set_schedule.await_args.args[1]
    assert saved_time == scheduled_at
    answers = [request for request in bot.requests if isinstance(request, SendMessage)]
    assert any("Публикация запланирована" in answer.text for answer in answers)
    assert "Время публикации: 31.12.2099 18:30" in answers[-1].text
