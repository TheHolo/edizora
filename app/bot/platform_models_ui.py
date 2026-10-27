"""Telegram model selection over the same registered-worker commands as the panel."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

PAGE_SIZE = 8


@dataclass(frozen=True)
class WorkerModel:
    worker_id: str
    worker_name: str
    model_id: str
    status: str
    selected: bool

    @property
    def token(self) -> str:
        return hashlib.sha256((self.worker_id + "\0" + self.model_id).encode()).hexdigest()[:24]


class RegisteredModels:
    """Resolve the common service lazily, after bootstrap has bound the runtime."""

    def __init__(self, jobs):
        self.jobs = jobs

    def service(self):
        platform = getattr(self.jobs, "platform", None)
        if platform is None:
            raise ValueError("Общий сервис ещё не подключён")
        return platform

    async def catalog(self, telegram_id: int) -> tuple[int, list[WorkerModel]]:
        platform = self.service()
        actor = await platform.user_for_telegram(telegram_id)
        workers = (await platform.ai_workers(actor)).get("items", [])
        models = (await platform.ai_models(actor)).get("items", [])
        settings = await platform.settings(actor)
        worker_map = {str(worker["id"]): worker for worker in workers if worker.get("status") != "revoked"}
        selected_worker = str(settings.get("ai_worker_id") or "")
        selected_model = str(settings.get("ai_model") or "")
        values: dict[tuple[str, str], WorkerModel] = {}
        for model in models:
            worker_id, model_id = str(model.get("worker_id", "")), str(model.get("id", ""))
            worker = worker_map.get(worker_id)
            if not worker or not model_id or "embed" in str(model.get("type", "")).lower() or "embed" in model_id.lower():
                continue
            selected = (worker_id == selected_worker and model_id == selected_model) if selected_worker else bool(model.get("selected"))
            values[(worker_id, model_id)] = WorkerModel(
                worker_id, str(worker.get("name") or worker.get("machine_name") or "Домашний исполнитель"),
                model_id, str(worker.get("status", "offline")), selected,
            )
        return actor, sorted(values.values(), key=lambda item: (item.worker_name.casefold(), item.model_id.casefold(), item.worker_id))

    async def select(self, telegram_id: int, token: str) -> None:
        actor, models = await self.catalog(telegram_id)
        matches = [model for model in models if model.token == token]
        if len(matches) != 1:
            raise ValueError("Кнопка устарела: модели больше нет в зарегистрированном каталоге")
        choice = matches[0]
        await self.service().ai_selection(actor, {"worker_id": choice.worker_id, "model_id": choice.model_id})


def registered_model_menu(models: list[WorkerModel], page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    pages = max(1, (len(models) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    selected = [item for item in models if item.selected]
    text = "<b>Модели домашнего AI</b>\n\n"
    if selected:
        selected_lines = [
            f"{escape(item.worker_name[:60])} · <code>{escape(item.model_id[:180])}</code>"
            for item in selected[:3]
        ]
        text += "Выбор для новых задач:\n" + "\n".join(selected_lines)
    else:
        text += "Модель для новых задач ещё не выбрана."
    text += (
        "\n\nКаталог получен от зарегистрированных исполнителей. "
        "Панель и команды используют один сохранённый выбор. "
        "Начатые задачи сохраняют свою модель; ожидающие переназначаются отдельно.\n\n"
        "● — исполнитель на связи; ◷ — ждём его возвращения."
    )
    if not models:
        text += "\n\nТекстовых моделей пока нет. Подключите домашний исполнитель и обновите его каталог в панели."
    rows = []
    for item in models[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        icon = "✅" if item.selected else "●" if item.status == "online" else "◷"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {item.worker_name[:18]} · {item.model_id}"[:64],
            callback_data=f"pm:select:{item.token}",
        )])
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"pm:page:{page - 1}"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"pm:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
        text += f"\n\nСтраница {page + 1} из {pages}"
    rows.append([InlineKeyboardButton(text="🔄 Обновить каталог", callback_data="pm:page:0")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_platform_model_router(jobs) -> Router:
    router = Router(name="registered_worker_models")
    registered = RegisteredModels(jobs)

    async def render(telegram_id: int, page: int = 0):
        try:
            _, models = await registered.catalog(telegram_id)
            return registered_model_menu(models, page)
        except Exception:
            return (
                "Не удалось прочитать каталог домашнего AI. Проверьте состояние установки "
                "и доступ исполнителей в панели. Сохранённый выбор не изменён.",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="🔄 Повторить", callback_data="pm:page:0",
                )]]),
            )

    @router.message(Command("models"))
    async def show_models(message: Message) -> None:
        if message.from_user is None:
            return
        text, markup = await render(message.from_user.id)
        await message.answer(text, reply_markup=markup, parse_mode="HTML")

    @router.callback_query(F.data.startswith("pm:") | F.data.startswith("lm:"))
    async def select_model(callback: CallbackQuery) -> None:
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3:
            return
        page = 0
        if parts[1] == "select":
            try:
                await registered.select(callback.from_user.id, parts[2])
            except Exception:
                await callback.message.answer(
                    "Не удалось изменить модель. Кнопка могла устареть, исполнитель отозван "
                    "или установка остановлена для обслуживания. Обновите /models; прежний выбор сохранён."
                )
                return
        elif parts[1] == "page" and parts[2].isdigit():
            page = min(int(parts[2]), 10000)
        else:
            return
        text, markup = await render(callback.from_user.id, page)
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest as error:
            if "message is not modified" not in str(error):
                raise

    return router
