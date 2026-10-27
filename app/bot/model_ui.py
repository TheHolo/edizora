from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.news.worker_errors import LlmError
from app.services.model_selection import LMStudioModels, LocalModel

PAGE_SIZE = 8


def model_menu(models: list[LocalModel], selected: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (len(models) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    text = (
        "🧠 <b>Модели LM Studio</b>\n\n"
        f"Выбрана: <code>{escape(selected or 'не выбрана')}</code>\n"
        "Выбор сохраняется и применяется к следующим переводам на английский и новостям. "
        "Начатая обработка завершится на прежней модели.\n\n"
        "● — загружена в память; ○ — установлена. "
        "Для незагруженной модели включите JIT loading в LM Studio или загрузите её вручную."
    )
    if not models:
        text += "\n\nТекстовых моделей нет. Скачайте модель в LM Studio и обновите список."
    elif selected and selected not in {model.id for model in models}:
        text += "\n\n⚠️ Выбранная модель отсутствует в текущем списке. Выберите доступную."
    rows = []
    for model in models[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        icon = "✅" if model.id == selected else "●" if model.loaded else "○" if model.loaded is False else "•"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {model.id}"[:64], callback_data=f"lm:select:{model.token}",
        )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"lm:page:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"lm:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
        text += f"\n\nСтраница {page + 1} из {pages}"
    rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="lm:page:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_model_router(models: LMStudioModels, *, local_news: bool = True) -> Router:
    router = Router(name="lmstudio_models")

    async def render(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
        try:
            text, markup = model_menu(await models.list(), models.selection.selected, page)
            if not local_news:
                text = text.replace("переводам на английский и новостям", "переводам названий на английский")
            return text, markup
        except (LlmError, ValueError):
            return (
                "Не удалось получить модели LM Studio. Включите Local Server, проверьте адрес, порт "
                "и API-ключ в настройках. Сохранённый выбор: "
                f"<code>{escape(models.selection.selected or 'не выбрана')}</code>",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="🔄 Повторить", callback_data="lm:page:0",
                )]]),
            )

    @router.message(Command("models"))
    async def show_models(message: Message) -> None:
        text, markup = await render()
        await message.answer(text, reply_markup=markup, parse_mode="HTML")

    @router.callback_query(F.data.startswith("lm:"))
    async def model_action(callback: CallbackQuery) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer()
            return
        # Catalog requests may take longer than Telegram's callback deadline.
        await callback.answer()
        parts = (callback.data or "").split(":")
        page = 0
        if len(parts) != 3:
            return
        if parts[1] == "select":
            try:
                await models.select(parts[2], token=True)
            except (LlmError, ValueError, OSError):
                await callback.message.answer(
                    "Не удалось сохранить выбор. Проверьте LM Studio и доступ к storage, затем обновите /models."
                )
                return
        elif parts[1] == "page" and parts[2].isdigit():
            page = int(parts[2])
        else:
            return
        text, markup = await render(page)
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error):
                raise

    return router
