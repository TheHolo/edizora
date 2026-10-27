from html import escape

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from app.bot.router import preview_keyboard
from app.bot.states import EditAlbum
from app.domain.album import AlbumState, read_album
from app.domain.exceptions import ApplicationError
from app.services.album_service import AlbumEditor
from app.services.album_thumbnails import PAGE_SIZE, AlbumThumbnails
from app.services.preview_service import PreviewService


def album_caption(job_id: int, album: AlbumState, page: int, focus: int = 0, unavailable: int = 0) -> str:
    pages = max(1, (len(album.catalog) + PAGE_SIZE - 1) // PAGE_SIZE)
    order = ", ".join(map(str, album.selection[:30])) or "пока пусто"
    if len(album.selection) > 30:
        order += ", …"
    text = (
        f"🗂 <b>Альбом · черновик #{job_id}</b>\n"
        f"Выбрано <b>{len(album.selection)} из {len(album.catalog)}</b> · страница {page + 1}/{pages}\n"
        f"Порядок: {order}\n\n"
        "Нажмите номер, чтобы включить, исключить или переместить кадр. "
        "Первый кадр отмечен FIRST. Изменения сохраняются автоматически."
    )
    if focus in album.catalog:
        position = f"позиция {album.selection.index(focus) + 1}" if focus in album.selection else "исключён"
        text += f"\n\n<b>Кадр #{focus}</b> · {position}"
    if len(album.selection) > 10:
        groups = (len(album.selection) + 9) // 10
        text += f"\nTelegram отправит {groups} групп/сообщений, до 10 кадров в каждой."
    if album.original_count > len(album.catalog):
        text += "\nЭто старый черновик: в нём сохранена только часть исходника. Для доступа ко всем кадрам добавьте ссылку заново."
    if unavailable:
        text += f"\nНедоступных миниатюр: {unavailable}. Номера остаются доступны для выбора."
    return text


def album_keyboard(job_id: int, album: AlbumState, page: int, focus: int = 0, *, limit: int | None = 10) -> InlineKeyboardMarkup:
    def button(text: str, action: str, number: int = 0) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"alb:{job_id}:{album.revision}:{page}:{action}:{number}")

    numbers = sorted(album.catalog)[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    tiles = [button(f"{'✅' if n in album.selection else '▫️'} {n}", "focus", n) for n in numbers]
    rows = [tiles[i:i + 3] for i in range(0, len(tiles), 3)]
    if focus in album.catalog:
        selected = focus in album.selection
        rows.append([button(f"{'Исключить' if selected else 'Включить'} #{focus}", "toggle", focus)])
        if selected:
            position = album.selection.index(focus)
            moves = []
            if position > 0:
                moves.append(button("← Раньше", "up", focus))
            if position < len(album.selection) - 1:
                moves.append(button("Позже →", "down", focus))
            if moves:
                rows.append(moves)
            if position > 0:
                rows.append([button("⭐ Сделать первой", "first", focus)])
    pages = max(1, (len(album.catalog) + PAGE_SIZE - 1) // PAGE_SIZE)
    navigation = []
    if page > 0:
        navigation.append(button("← Страница", "page", page - 1))
    if page + 1 < pages:
        navigation.append(button("Страница →", "page", page + 1))
    if navigation:
        rows.append(navigation)
    first_count = min(10, limit) if limit else 10
    rows.extend([
        [button(f"Первые {first_count}", "first10"), button("Очистить выбор", "clear")],
        [button("Исходный выбор", "restore"), button("⌨ Порядок номерами", "order")],
        [button("👁 Предпросмотр", "preview"), button("Готово → черновик", "done")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_album_router(*, editor: AlbumEditor, thumbnails: AlbumThumbnails, previews: PreviewService) -> Router:
    router = Router(name="album_editor")

    async def show(message: Message, job, page: int = 0, focus: int = 0, *, edit: bool = False, caption_only: bool = False):
        album = read_album(job.post_data)
        page = max(0, min(page, max(0, (len(album.catalog) - 1) // PAGE_SIZE)))
        keyboard = album_keyboard(job.id, album, page, focus, limit=editor.limit)
        if caption_only and message.photo:
            await message.edit_caption(caption=album_caption(job.id, album, page, focus), parse_mode="HTML", reply_markup=keyboard)
            return
        rendered = await thumbnails.page(job.id, album, page)
        caption = album_caption(job.id, album, page, focus, rendered.unavailable)
        photo = BufferedInputFile(rendered.image, filename=f"album-{job.id}.jpg")
        if edit and message.photo:
            await message.edit_media(InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"), reply_markup=keyboard)
        else:
            await message.answer_photo(photo, caption=caption, parse_mode="HTML", reply_markup=keyboard)

    @router.callback_query(F.data.startswith("album_open:"))
    async def open_album(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            job = await editor.get(int(callback.data.split(":")[1]))
        except (ApplicationError, ValueError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await show(callback.message, job)

    @router.callback_query(F.data.startswith("alb:"))
    async def operate(callback: CallbackQuery, state: FSMContext) -> None:
        acknowledged = False
        try:
            _, raw_id, raw_revision, raw_page, action, raw_number = callback.data.split(":")
            job_id, revision, page, number = map(int, (raw_id, raw_revision, raw_page, raw_number))
            job = await editor.get(job_id)
            album = read_album(job.post_data)
            if revision != album.revision:
                await callback.answer("Альбом уже изменён. Обновляю редактор.", show_alert=True)
                acknowledged = True
                await state.clear()
                await show(callback.message, job, page, edit=True)
                return
            if action == "order":
                await state.set_state(EditAlbum.waiting_for_order)
                await state.update_data(album_job_id=job_id, album_revision=revision, album_page=page)
                await callback.answer()
                acknowledged = True
                await callback.message.answer(
                    "Пришлите номера в нужном порядке, например <code>7,1,5,3</code>. "
                    "Этот список заменит текущий выбор. Для отмены — /cancel.", parse_mode="HTML",
                )
                return
            if action in {"done", "preview"} and not album.selection:
                await callback.answer("Выберите хотя бы одно изображение.", show_alert=True)
                return
            if action == "preview":
                await callback.answer("Готовлю предпросмотр…")
                acknowledged = True
                await previews.send(job, callback.message.chat.id)
                return
            if action == "done":
                await state.clear()
                await callback.answer()
                acknowledged = True
                caption = await previews.caption(job)
                await callback.message.answer(
                    f"🖼 <b>Черновик #{job.id}</b> · канал {escape(job.channel.alias)}\n"
                    f"Выбрано {len(album.selection)} из {len(album.catalog)}. Изменения сохранены.\n\n{caption}",
                    parse_mode="HTML", reply_markup=preview_keyboard(job.id),
                )
                await callback.message.edit_reply_markup(reply_markup=None)
                return
            if action not in {"page", "focus"}:
                job = await editor.change(job_id, revision, action, number)
            await state.clear()
            await callback.answer()
            acknowledged = True
            await show(
                callback.message, job, number if action == "page" else page,
                number if action in {"focus", "toggle", "up", "down", "first"} else 0,
                edit=True, caption_only=action == "focus",
            )
        except ApplicationError as error:
            if acknowledged:
                await callback.message.answer(str(error))
            else:
                await callback.answer(str(error), show_alert=True)
        except (ValueError, IndexError):
            if acknowledged:
                await callback.message.answer("Не удалось обновить редактор. Откройте его заново.")
            else:
                await callback.answer("Кнопка редактора устарела. Откройте его заново.", show_alert=True)
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error):
                raise

    @router.message(EditAlbum.waiting_for_order)
    async def receive_order(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        command = text.split(maxsplit=1)
        if command and command[0].split("@", 1)[0] == "/cancel" and len(command) == 1:
            await state.clear()
            await message.answer("Ввод порядка отменён. Сохранённый выбор не изменился.")
            return
        if text.startswith("/"):
            # The editor router runs before the general command router. Leave the
            # unfinished input and let /preview, /queue or /cancel <id> reach it.
            await state.clear()
            raise SkipHandler()
        data = await state.get_data()
        try:
            job = await editor.change(
                data["album_job_id"], data["album_revision"], "order", order=message.text or "",
            )
        except ApplicationError as error:
            await message.answer(str(error))
            return
        await state.clear()
        await show(message, job, data.get("album_page", 0))

    return router
