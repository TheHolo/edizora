from aiogram.fsm.state import State, StatesGroup


class EditPreview(StatesGroup):
    waiting_for_tags = State()
    waiting_for_caption = State()
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_schedule = State()


class EditAlbum(StatesGroup):
    waiting_for_order = State()


class CreatePublication(StatesGroup):
    waiting_for_urls = State()
    waiting_for_channel = State()


class CreateNews(StatesGroup):
    waiting_for_source = State()
    waiting_for_manual_text = State()


class EditNews(StatesGroup):
    waiting_for_text = State()
    waiting_for_media = State()


class ManageChannel(StatesGroup):
    waiting_for_interval = State()


class ManageQueue(StatesGroup):
    waiting_for_schedule = State()
