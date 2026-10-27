from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MenuButtonCommands,MenuButtonWebApp,WebAppInfo

from app.bot.menu import configure_bot_ui


async def test_miniapp_button_requires_https_before_bot_calls():
    bot=SimpleNamespace(set_my_commands=AsyncMock(),set_chat_menu_button=AsyncMock(),get_chat_menu_button=AsyncMock())
    with pytest.raises(ValueError):await configure_bot_ui(bot,panel_url="http://example.com")
    bot.set_my_commands.assert_not_awaited();bot.set_chat_menu_button.assert_not_awaited()


async def test_miniapp_button_uses_verified_url_and_preserves_command_catalogue():
    bot=SimpleNamespace(set_my_commands=AsyncMock(),set_chat_menu_button=AsyncMock(),get_chat_menu_button=AsyncMock(return_value=MenuButtonCommands()))
    await configure_bot_ui(bot,panel_url="https://publisher.example")
    menu=bot.set_chat_menu_button.await_args.kwargs["menu_button"]
    assert isinstance(menu,MenuButtonWebApp) and menu.web_app.url=="https://publisher.example"
    assert bot.set_my_commands.await_args.args[0]
    bot.get_chat_menu_button.return_value=menu
    bot.set_chat_menu_button.reset_mock()
    await configure_bot_ui(bot,panel_url="https://publisher.example")
    bot.set_chat_menu_button.assert_not_awaited()


def test_legacy_entrypoint_cannot_bypass_managed_or_retired_identity(monkeypatch,tmp_path):
    import app.runtime as runtime
    monkeypatch.setattr(runtime,"managed_checkout_marker",lambda:tmp_path/"pointer.json")
    monkeypatch.setenv("PUBLISHER_CONTROL_DIR",str(tmp_path/"control"))
    with pytest.raises(RuntimeError,match="веб-панелью"):runtime.assert_legacy_entrypoint_allowed()
    monkeypatch.delenv("PUBLISHER_CONTROL_DIR")
    (tmp_path/"pointer.json").write_text('{"control_dir":"retired-installation"}',encoding="utf-8")
    with pytest.raises(RuntimeError):runtime.assert_legacy_entrypoint_allowed()
    (tmp_path/"pointer.json").unlink()
    runtime.assert_legacy_entrypoint_allowed()
