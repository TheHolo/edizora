"""Desktop entry point. The initial form is reachable only with a local launch secret."""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from contextlib import closing

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.runtime import InstanceLock
from app.branding import default_data_root


def root_path():
    return Path(os.environ.get("PUBLISHER_DESKTOP_ROOT", default_data_root())).resolve()


def configure_environment(root):
    # Installed apps never load a repository's .env, TOML or relative storage.
    root.mkdir(parents=True, exist_ok=True)
    if not getattr(sys, 'frozen', False):
        os.environ['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get('PYTHONPATH', '')
    os.chdir(root)
    for name in ('BOT_TOKEN', 'ADMIN_IDS', 'PIXIV_COOKIES', 'NEWS_WORKER_TOKEN', 'HOME_WORKER_TOKEN', 'DESKTOP_AI_CONNECTIONS'):
        os.environ.pop(name, None)
    os.environ.update(PUBLISHER_CONTROL_DIR=str(root / "control"), PUBLISHER_DATA_ROOT=str(root / "data"),
                      PUBLISHER_DATABASE_PATH=str(root / "data/database.db"), PUBLISHER_DESKTOP="1",
                      PUBLISHER_FRONTEND_DIR=str(Path(__file__).resolve().parents[2] / "frontend/dist"))


def configure_logs(root):
    from app.logging_config import SecretFormatter, SecretFilter
    directory = root / "logs"
    directory.mkdir(exist_ok=True)
    handler = RotatingFileHandler(directory / "edizora.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.addFilter(SecretFilter())
    handler.setFormatter(SecretFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def move_directory(source, target):
    # Windows scanners can briefly keep a just-written directory open.
    deadline = time.monotonic() + 3
    while True:
        try:
            source.rename(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


class SetupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admin_id: int = Field(gt=0, lt=2**63)
    password: str = Field(min_length=12, max_length=1024)
    password_repeat: str = Field(min_length=12, max_length=1024)
    mode: str = "new"
    checkout: str = ""
    database: str = ""
    storage: str = ""
    confirmed_stopped: bool = False
    backup: str = ""
    recovery_password: str = Field(default="", max_length=1024)


async def initialize(root: Path, body: SetupInput):
    from argparse import Namespace
    from app.api.auth import AuthStore
    from app.host.control import ControlStore, durable_json
    from app.web import trusted_setup
    if body.password != body.password_repeat:
        raise ValueError("Пароли не совпали")
    control, data = root / "control", root / "data"
    if (control / "node.json").exists() or (data.exists() and any(data.iterdir())):
        raise ValueError("Установка уже содержит данные. Откройте её через Edizora; повторная настройка запрещена")
    if body.mode == "legacy":
        from app.host.import_legacy import import_legacy
        if not all([body.checkout, body.database, body.storage, body.confirmed_stopped]):
            raise ValueError("Выберите старую папку, базу и storage; подтвердите остановку старого бота")
        await import_legacy(checkout=Path(body.checkout), database=Path(body.database), storage=Path(body.storage),
                            data=data, control=control, admin_id=body.admin_id, confirmed_stopped=True)
        import sqlite3
        node = ControlStore(control).identity()
        imported_database = data / node.get('database_key', 'database.db')
        with closing(sqlite3.connect(imported_database)) as db:
            user = db.execute("SELECT id FROM users WHERE telegram_user_id=?", (body.admin_id,)).fetchone()
        AuthStore(control).provision(user[0], body.admin_id, body.password)
    elif body.mode in {"new", "restore"}:
        # Build the whole initial state off-line. A failed form cannot activate a
        # half-configured host, and the next launch can safely retry this stage.
        stage = root / "first-install"
        if stage.exists():
            if stage.is_symlink() or stage.resolve().parent != root.resolve():
                raise ValueError("Папка подготовки установки имеет недопустимый путь")
            shutil.rmtree(stage)
        staged_data, staged_control = stage / "data", stage / "control"
        store, auth = ControlStore(staged_control), AuthStore(staged_control)
        with InstanceLock(staged_control / "host.lock") as lock:
            if body.mode == 'new':
                options = Namespace(initialize=True, upgrade=False, admin_id=body.admin_id, set_password=False,
                    standby=False, adopt_checkout=False, activate_initial_installation=False, _host_lock=lock)
                await trusted_setup(options, store, auth, staged_data, staged_data / "database.db")
                import sqlite3
                with closing(sqlite3.connect(staged_data / "database.db")) as db:
                    user = db.execute("SELECT id FROM users WHERE telegram_user_id=?", (body.admin_id,)).fetchone()
                auth.provision(user[0], body.admin_id, body.password)
            else:
                from app.desktop.restore import restore_initial
                await restore_initial(store, auth, staged_data, body)
            store.block("user_pause", "user")
            import sqlite3
            with closing(sqlite3.connect(staged_data / store.identity().get('database_key', 'database.db'))) as db:
                db.execute("UPDATE application_state SET publication_blocks=? WHERE id=1", (json.dumps({'user_pause': 'user'}),))
                db.commit()
            durable_json(store.node_path, {**store.identity(), "data_root": str(data.resolve())})
        if data.exists():
            data.rmdir()
        if control.exists():
            if any(control.iterdir()):
                raise ValueError("Обнаружены служебные данные. Используйте восстановление установки")
            control.rmdir()
        # Pending marker makes a power loss between these renames recoverable.
        durable_json(root / "install-pending.json", {"format": 1})
        move_directory(staged_data, data)
        move_directory(staged_control, control)
        (root / "install-pending.json").unlink()
    else:
        raise ValueError("Неизвестный способ настройки")


def recover_initial_moves(root):
    if not (root / "install-pending.json").is_file():
        return
    for name in ("data", "control"):
        source, target = root / "first-install" / name, root / name
        if source.exists() and not target.exists():
            move_directory(source, target)
        elif not target.exists() or source.exists():
            raise ValueError("Первоначальная установка прервана; сохраните папку Edizora для диагностики")
    (root / "install-pending.json").unlink()


def setup_app(root, token, origin, on_complete):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    busy = asyncio.Lock()

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.client.host not in {"127.0.0.1", "::1", "testclient"} or request.headers.get("host") != origin.removeprefix("http://"):
            return JSONResponse({"message": "Доступ разрешён только с этого компьютера"}, 403)
        if request.url.path.startswith("/setup-api/"):
            import hmac
            if not hmac.compare_digest(request.headers.get("x-setup-token", ""), token):
                return JSONResponse({"message": "Откройте настройку через ярлык Edizora"}, 403)
            if request.method != "GET" and request.headers.get("origin") != origin:
                return JSONResponse({"message": "Адрес запроса не подтверждён"}, 403)
            try:
                if int(request.headers.get("content-length", "0")) > 16384:
                    return JSONResponse({"message": "Слишком большой запрос"}, 413)
            except ValueError:
                return JSONResponse({"message": "Некорректный запрос"}, 400)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(Exception)
    async def error(request, exc):
        # No raw exception bodies: input can contain passwords or personal paths.
        return JSONResponse({"message": "Не удалось завершить настройку. Данные сохранены; откройте диагностику или повторите запуск."}, 500)

    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"message": "Укажите положительный Telegram ID и пароль от 12 символов"}, 422)

    @app.post("/setup-api/initialize")
    async def submit(body: SetupInput):
        async with busy:
            try:
                await initialize(root, body)
            except ValueError as exc:
                return JSONResponse({"message": str(exc)}, 422)
            on_complete()
            return {"status": "ready", "message": "Приложение установлено. Панель откроется автоматически."}

    @app.get("/{path:path}")
    async def assets(path: str):
        files = {"": "setup.html", "setup.js": "setup.js", "setup.css": "setup.css"}
        if path not in files:
            return JSONResponse({"message": "Страница не найдена"}, 404)
        return FileResponse(Path(__file__).parent / "assets" / files[path])

    return app


def run_setup(root):
    import uvicorn
    port, token = free_port(), secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"
    completed = threading.Event()
    app = setup_app(root, token, origin, completed.set)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_config=None))
    def browser():
        while not server.started and not server.should_exit:
            time.sleep(.1)
        if server.started:
            webbrowser.open(origin + "/#setup=" + token)
        while not completed.wait(.3):
            if server.should_exit:
                return
        time.sleep(1)
        server.should_exit = True
    threading.Thread(target=browser, daemon=True).start()
    server.run()
    return completed.is_set()


def stop_host(root):
    record = json.loads((root / "control/host.json").read_text(encoding="utf-8"))
    token = (root / "control/host-control.key").read_text(encoding="utf-8")
    response = httpx.post(record["local_url"] + "/_host/stop", headers={"Authorization": "Bearer " + token}, timeout=10, trust_env=False)
    response.raise_for_status()


def main():
    original_arguments = sys.argv[1:]
    root = root_path()
    configure_environment(root)
    configure_logs(root)
    if "--prepare-update" in original_arguments:
        from app.desktop.maintenance import prepare_update
        prepare_update(root)
        return
    if "--stop" in sys.argv:
        stop_host(root)
        return
    recover_initial_moves(root)
    if not (root / "control/node.json").is_file():
        try:
            with InstanceLock(root / "desktop-setup.lock"):
                if not run_setup(root):
                    return
        except RuntimeError:
            raise RuntimeError("Мастер настройки уже открыт. Завершите настройку в браузере") from None
    # The host itself handles repeated launches under its own instance lock.
    from app.host.secrets import SecretStore
    from app.web import run
    bot_enabled = bool(SecretStore(root / "control").read("BOT_TOKEN"))
    port = 8000
    record_path = root / "control/host.json"
    if record_path.is_file():
        from urllib.parse import urlsplit
        port = urlsplit(json.loads(record_path.read_text(encoding="utf-8"))["local_url"]).port or port
    else:
        port = free_port()
    sys.argv = [sys.argv[0], "--port", str(port), "--upgrade", *( ["--enable-bot"] if bot_enabled else [])]
    if "--background" in original_arguments:
        sys.argv.append('--no-browser')
    run()


def run():
    mutex = None
    try:
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            mutex = kernel.CreateMutexW(None, False, 'Local\\Edizora.Application')
        main()
    except Exception:
        logging.exception("Не удалось запустить Edizora")
        message = "Could not start Edizora. Log: " + str(root_path() / "logs/edizora.log")
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, message, "Edizora", 0x10)
        else:
            raise
        raise SystemExit(1)
    finally:
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    run()
