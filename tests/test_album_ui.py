from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Router
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption, EditMessageMedia, SendMessage, SendPhoto
from aiogram.filters import CommandObject
from aiogram.types import PhotoSize

from app.bot.album_ui import album_caption, album_keyboard, build_album_router
from app.bot.states import EditAlbum
from app.domain.album import initialize_album, read_album, select_album
from app.domain.enums import ContentKind, JobStatus
from app.services.album_thumbnails import AlbumPage
from app.services.job_service import serialize_post
from test_ingest_service import _post
from test_menu import RecordingBot, flatten, handler, make_callback, make_message, make_router, make_state


def context():
    post = _post("pixiv", 15)
    initialize_album(post, [1, 3, 12])
    job = SimpleNamespace(id=8, post_data=serialize_post(post), channel=SimpleNamespace(alias="art"))
    editor = SimpleNamespace(get=AsyncMock(return_value=job), change=AsyncMock(return_value=job), limit=10)
    thumbnails = SimpleNamespace(page=AsyncMock(return_value=AlbumPage(b"image", 0, 2, list(range(1, 10)), 0)))
    previews = SimpleNamespace(send=AsyncMock(), caption=AsyncMock(return_value="<b>Caption</b>"))
    router = build_album_router(editor=editor, thumbnails=thumbnails, previews=previews)
    bot = RecordingBot()
    message = make_message(bot).model_copy(update={"photo": [PhotoSize(
        file_id="photo", file_unique_id="unique", width=872, height=872,
    )]}).as_(bot)
    return job, editor, thumbnails, previews, router, bot, message


def test_keyboard_pages_selection_and_moves_are_unambiguous():
    job, *_ = context()
    album = read_album(job.post_data)
    buttons = flatten(album_keyboard(8, album, 1, 12))
    assert any(b.callback_data == "alb:8:0:1:first:12" for b in buttons)
    assert any(b.text == "Исключить #12" for b in buttons)
    assert any(b.callback_data == "alb:8:0:1:focus:15" for b in buttons)
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert "Выбрано <b>3 из 15</b>" in album_caption(8, album, 1)
    assert "2/2" in album_caption(8, album, 1)


async def test_open_sends_contact_sheet_and_clears_previous_input():
    job, editor, thumbnails, _, router, bot, message = context()
    state = make_state()
    await state.set_state(EditAlbum.waiting_for_order)
    await handler(router, "callback_query", "open_album")(
        make_callback(bot, message, "album_open:8"), state,
    )
    assert await state.get_state() is None
    assert any(isinstance(r, SendPhoto) for r in bot.requests)
    thumbnails.page.assert_awaited_once()


async def test_focus_updates_only_controls_without_downloading_or_mutating():
    _, editor, thumbnails, _, router, bot, message = context()
    await handler(router, "callback_query", "operate")(
        make_callback(bot, message, "alb:8:0:0:focus:3"), make_state(),
    )
    editor.change.assert_not_awaited()
    thumbnails.page.assert_not_awaited()
    edit = next(r for r in bot.requests if isinstance(r, EditMessageCaption))
    assert "Кадр #3" in edit.caption
    assert any(b.text == "⭐ Сделать первой" for b in flatten(edit.reply_markup))


async def test_stale_keyboard_refreshes_without_applying_action():
    job, editor, thumbnails, _, router, bot, message = context()
    job.post_data = select_album(job.post_data, [12, 1], limit=10)
    await handler(router, "callback_query", "operate")(
        make_callback(bot, message, "alb:8:0:0:clear:0"), make_state(),
    )
    editor.change.assert_not_awaited()
    assert any(isinstance(r, EditMessageMedia) for r in bot.requests)
    assert sum(isinstance(r, AnswerCallbackQuery) for r in bot.requests) == 1


async def test_manual_order_keeps_revision_and_sends_new_editor():
    _, editor, _, _, router, bot, message = context()
    state = make_state()
    await handler(router, "callback_query", "operate")(
        make_callback(bot, message, "alb:8:0:1:order:0"), state,
    )
    assert await state.get_state() == EditAlbum.waiting_for_order.state
    await handler(router, "message", "receive_order")(make_message(bot, "12,3,1"), state)
    editor.change.assert_awaited_once_with(8, 0, "order", order="12,3,1")
    assert await state.get_state() is None
    assert any(isinstance(r, SendPhoto) for r in bot.requests)


async def test_empty_selection_cannot_finish_or_preview():
    job, _, _, previews, router, bot, message = context()
    job.post_data = select_album(job.post_data, [], limit=10)
    for action in ("done", "preview"):
        await handler(router, "callback_query", "operate")(
            make_callback(bot, message, f"alb:8:1:0:{action}:0"), make_state(),
        )
    previews.send.assert_not_awaited()
    previews.caption.assert_not_awaited()
    assert all(isinstance(r, AnswerCallbackQuery) and r.show_alert for r in bot.requests)


async def test_done_returns_to_publishable_draft_without_sending_to_channel():
    _, editor, _, previews, router, bot, message = context()
    await handler(router, "callback_query", "operate")(
        make_callback(bot, message, "alb:8:0:0:done:0"), make_state(),
    )
    confirmation = next(r for r in bot.requests if isinstance(r, SendMessage))
    assert "Caption" in confirmation.text
    assert any(b.callback_data == "publish:8" for b in flatten(confirmation.reply_markup))
    assert confirmation.chat_id == message.chat.id
    previews.send.assert_not_awaited()


async def test_saved_empty_draft_can_be_reopened_by_id_after_restart():
    job, _, _, previews, _, bot, message = context()
    job.post_data = select_album(job.post_data, [], limit=10)
    job.status = JobStatus.WAITING_CONFIRMATION
    job.content_kind = ContentKind.ARTWORK
    jobs = SimpleNamespace(get=AsyncMock(return_value=job))
    router = make_router(jobs, previews=previews)
    await handler(router, "message", "preview_job")(
        message, CommandObject(command="preview", args="8"),
    )
    confirmation = next(r for r in bot.requests if isinstance(r, SendMessage))
    assert "Выбрано: 0 из 15" in confirmation.text
    assert any(b.callback_data == "album_open:8" for b in flatten(confirmation.reply_markup))
    previews.send.assert_not_awaited()


async def test_preview_command_can_leave_manual_album_order_input():
    job, editor, _, previews, album_router, bot, _ = context()
    job.status = JobStatus.WAITING_CONFIRMATION
    job.content_kind = ContentKind.ARTWORK
    jobs = SimpleNamespace(get=AsyncMock(return_value=job))
    root = Router()
    root.include_router(album_router)
    root.include_router(make_router(jobs, previews=previews))
    state = make_state()
    await state.set_state(EditAlbum.waiting_for_order)
    await state.set_data({"album_job_id": 8, "album_revision": 0, "album_page": 0})

    await root.propagate_event(
        "message", make_message(bot, "/preview 8"),
        state=state, raw_state=await state.get_state(), bot=bot,
    )

    editor.change.assert_not_awaited()
    assert await state.get_state() is None
    confirmation = next(r for r in bot.requests if isinstance(r, SendMessage))
    assert "Черновик #8" in confirmation.text
    assert any(b.callback_data == "album_open:8" for b in flatten(confirmation.reply_markup))


@pytest.mark.parametrize("text", ["/cancel", "/cancel@publisher_bot"])
async def test_bare_cancel_only_cancels_manual_order_input(text):
    _, editor, _, _, router, bot, _ = context()
    state = make_state()
    await state.set_state(EditAlbum.waiting_for_order)
    await state.set_data({"album_job_id": 8, "album_revision": 0, "album_page": 0})

    await handler(router, "message", "receive_order")(make_message(bot, text), state)

    editor.change.assert_not_awaited()
    assert await state.get_state() is None
    assert "Ввод порядка отменён" in bot.requests[0].text


async def test_cancel_with_job_id_reaches_the_job_command_from_album_input():
    _, editor, _, previews, album_router, bot, _ = context()
    jobs = SimpleNamespace(request_cancel=AsyncMock(return_value=True))
    root = Router()
    root.include_router(album_router)
    root.include_router(make_router(jobs, previews=previews))
    state = make_state()
    await state.set_state(EditAlbum.waiting_for_order)
    await state.set_data({"album_job_id": 8, "album_revision": 0, "album_page": 0})

    await root.propagate_event(
        "message", make_message(bot, "/cancel 8"),
        state=state, raw_state=await state.get_state(), bot=bot,
    )

    editor.change.assert_not_awaited()
    jobs.request_cancel.assert_awaited_once_with(8)
    assert await state.get_state() is None
