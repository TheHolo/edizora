import inspect
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit, parse_qs

from fastapi import Depends, FastAPI, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException

from app.api.auth import AuthError, AuthStore, Principal
from app.application.platform import PlatformError
from app.host.control import GateClosed
from app.deployment.snapshot import VerificationError
from app.api.models import (ActionInput, ChangePassword, ChannelInput, DraftInput,
    ErrorResponse, LocalExchange, MutationInput, PasswordBody, PasswordLogin, ChannelDTO,ChannelPage,OperationDTO,OperationPage,
    PublicationDTO, PublicationPage, PublicationPatch, QueueOrder, QueueMove, UncertainResolution, TelegramExchange, VersionedInput)


@dataclass
class WebConfig:
    origin: str = "http://127.0.0.1:8000"
    allowed_hosts: set[str] = field(default_factory=lambda: {"127.0.0.1", "localhost", "::1"})
    bot_token: str = ""
    admin_ids: set[int] = field(default_factory=set)
    frontend: Path = Path("frontend/dist")
    desktop: bool = False
    upload_limit: int = 47 * 1024 * 1024


async def invoke(target, method, *args, **kwargs):
    fn = getattr(target, method, None)
    if fn is None:
        raise AuthError("capability_unavailable", "Действие пока недоступно в этой установке", 503)
    result = fn(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def create_app(service, auth: AuthStore, *, config: WebConfig | None = None,
               deployment=None, gate=None, lifespan=None) -> FastAPI:
    config = config or WebConfig()
    app = FastAPI(title="Publisher API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None,
                  responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse},
                             422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}})
    app.state.service, app.state.auth = service, auth

    def error_response(status, code, message, request, **extra):
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message,
             "retryable": status == 503, "request_id": getattr(request.state, "request_id", None), **extra}})

    @app.middleware("http")
    async def security(request: Request, call_next):
        request.state.request_id = secrets.token_hex(12)
        host = request.url.hostname
        if host not in config.allowed_hosts:
            return error_response(400, "invalid_host", "Адрес панели не разрешён", request)
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", 0)) > config.upload_limit + 65536:
            return error_response(413, "payload_too_large", "Превышен допустимый размер запроса", request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != config.origin:
                return error_response(403, "invalid_origin", "Запрос с другого сайта запрещён", request)
        try:
            response = await call_next(request)
        except AuthError as exc:
            return error_response(exc.status, exc.code, exc.message, request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return error_response(exc.status, exc.code, exc.message, request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo password/initData, upload contents or arbitrary input in validation errors.
        fields = [{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in exc.errors()]
        return error_response(422, "validation_error", "Проверьте заполненные поля", request, field_errors=fields)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(exc.status_code, "not_found" if exc.status_code == 404 else "request_error",
                              "Адрес не найден" if exc.status_code == 404 else str(exc.detail), request)

    @app.exception_handler(ValueError)
    async def invalid_value(request,exc):
        return error_response(422,'invalid_parameters','Проверьте параметры и актуальность плана действия',request)

    @app.exception_handler(KeyError)
    async def missing_resource(request,exc):
        return error_response(404,'not_found','Запись не найдена в этой установке',request)

    @app.exception_handler(PlatformError)
    @app.exception_handler(GateClosed)
    @app.exception_handler(VerificationError)
    @app.exception_handler(Exception)
    async def app_error(request, exc):
        status = getattr(exc, "status", getattr(exc, "status_code", 500))
        if isinstance(status, int) and status in {400, 403, 404, 409, 422, 503}:
            extra = {}
            for key in ("current_version", "current", "details"):
                value = getattr(exc, key, None)
                if value is not None:
                    extra["details" if key == "current" else key] = value
            return error_response(status, getattr(exc, "code", "operation_error"),
                                  getattr(exc, "message", "Действие отклонено"), request, **extra)
        return error_response(500, "internal_error", "Не удалось выполнить действие. Сохранённые данные доступны после повторного входа.", request)

    async def principal(request: Request) -> Principal:
        bearer = request.headers.get("authorization", "")
        token = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get("publisher_session", "")
        identity = auth.authenticate(token)
        # Service verifies active/admin against the same internal User on every command.
        if hasattr(service, "validate_actor"):
            try:
                await invoke(service, "validate_actor", identity.user_id)
            except PlatformError as exc:
                control_read = request.url.path.startswith(('/api/v1/deployment/', '/api/v1/backups', '/api/v1/restore/', '/api/v1/operations')) or request.url.path == '/api/v1/auth/session'
                if exc.status != 503 or not control_read:
                    raise
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not bearer.startswith("Bearer "):
            if request.headers.get("origin") != config.origin:
                raise AuthError("invalid_origin", "Не удалось подтвердить адрес запроса", 403)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), identity.csrf):
                raise AuthError("csrf_failed", "Обновите панель и повторите действие", 403)
        return identity

    Actor = Annotated[Principal, Depends(principal)]

    def cookie(response, result):
        response.set_cookie("publisher_session", result["token"], httponly=True,
                            secure=config.origin.startswith("https://"), samesite="lax", max_age=604800)

    def session_result(identity):
        return {"user": {"id": identity.user_id, "telegram_id": identity.telegram_id, "name": str(identity.telegram_id)},
                "csrf_token": identity.csrf, "method": identity.method, "timezone": "Asia/Vladivostok"}

    async def user_for(telegram_id):
        if telegram_id not in config.admin_ids:
            raise AuthError("access_denied", "Пользователь не входит в список администраторов", 403)
        return await invoke(service, "user_for_telegram", telegram_id)

    def key(value):
        import uuid
        try:
            uuid.UUID(value or "")
        except (ValueError, AttributeError):
            raise AuthError("idempotency_key_required", "Нужен уникальный ключ команды", 422) from None
        return value

    @app.get("/api/v1/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/api/v1/health/ready")
    async def ready(response: Response):
        try:
            await invoke(service, "ping")
            if gate:
                gate.assert_publication_allowed()
        except Exception:
            response.status_code = 503
            return {"status": "not_ready"}
        return {"status": "ready"}

    @app.get("/api/v1/auth/session")
    async def session(actor: Actor):
        return session_result(actor)

    @app.post("/api/v1/auth/local-exchange")
    async def local_exchange(body: LocalExchange, request: Request, response: Response):
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise AuthError("local_only", "Этот вход доступен только на компьютере установки", 403)
        result = auth.exchange_local(body.code)
        await invoke(service, "validate_actor", result["user"]["id"])
        cookie(response, result)
        return {k: v for k, v in result.items() if k != "token"}

    @app.post("/api/v1/auth/password")
    async def password(body: PasswordLogin, request: Request, response: Response):
        await user_for(body.telegram_id)
        result = auth.password_login(body.telegram_id, body.password, request.client.host if request.client else "unknown")
        cookie(response, result)
        return {k: v for k, v in result.items() if k != "token"}

    @app.post("/api/v1/auth/telegram")
    async def telegram(body: TelegramExchange, response: Response):
        try:
            fields = dict(parse_qs(body.init_data))
            telegram_id = int(json.loads(fields["user"][0])["id"])
            user_id = await user_for(telegram_id)
            result = auth.telegram_login(body.init_data, body.exchange_nonce, config.bot_token, config.admin_ids, user_id)
        except (ValueError, KeyError, TypeError):
            raise AuthError("invalid_init_data", "Некорректные данные Telegram") from None
        cookie(response, result)
        return result

    @app.post("/api/v1/auth/logout")
    async def logout(actor: Actor, response: Response):
        auth.logout(actor)
        response.delete_cookie("publisher_session")
        return {"status": "signed_out"}

    @app.post("/api/v1/auth/reauth")
    async def reauth(body: PasswordBody, actor: Actor):
        auth.reauth(actor, body.password)
        return {"status": "confirmed"}

    @app.post("/api/v1/auth/change-password")
    async def change_password(body: ChangePassword, actor: Actor):
        auth.reauth(actor, body.password)
        auth.provision(actor.user_id, actor.telegram_id, body.new_password)
        return {"status": "password_changed", "reauthentication_required": True}

    @app.get("/api/v1/openapi.json")
    async def openapi(actor: Actor):
        return app.openapi()

    @app.get("/api/v1/publications", response_model=PublicationPage)
    async def publications(actor: Actor, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                           status: str | None = None, channel_id: int | None = None, search: str | None = Query(None, max_length=500),
                           content_kind: str | None=None,date_from: str | None=None,date_to: str | None=None):
        return await invoke(service, "list_publications", actor.user_id, limit=limit, cursor=cursor,
                            status=status, channel_id=channel_id, search=search,content_kind=content_kind,date_from=date_from,date_to=date_to)

    @app.get("/api/v1/publications/{job_id}", response_model=PublicationDTO)
    async def publication(job_id: int, actor: Actor):
        return await invoke(service, "get_publication", actor.user_id, job_id)

    @app.patch("/api/v1/publications/{job_id}", response_model=PublicationDTO)
    async def patch(job_id: int, body: PublicationPatch, actor: Actor):
        return await invoke(service, "patch_publication", actor.user_id, job_id, body.model_dump(exclude_unset=True))

    def create_route(kind):
        async def create(body: DraftInput, actor: Actor, idempotency_key: str | None = Header(None)):
            payload = body.model_dump(exclude_unset=True)
            payload["content_kind"] = kind
            return await invoke(service, "create_publication", actor.user_id, payload, key(idempotency_key))
        return create

    for path, kind in (("text-draft", "text"), ("news", "news"), ("import", "artwork")):
        app.add_api_route(f"/api/v1/publications/{path}", create_route(kind), methods=["POST"], status_code=202, name=f"create_{kind}")

    def action_route(action):
        async def action_handler(job_id: int, body: ActionInput, actor: Actor, idempotency_key: str | None = Header(None)):
            return await invoke(service, "publication_action", actor.user_id, job_id, action,
                                body.model_dump(exclude_unset=True), key(idempotency_key))
        return action_handler

    for action in ("enqueue", "schedule", "publish-now", "return-to-draft", "cancel", "retry", "confirm-duplicate"):
        app.add_api_route(f"/api/v1/publications/{{job_id}}/actions/{action}", action_route(action), methods=["POST"], name=action)

    @app.post("/api/v1/publications/{job_id}/actions/resolve-uncertain")
    async def resolve_uncertain(job_id: int, body: UncertainResolution, actor: Actor, idempotency_key: str | None = Header(None)):
        return await invoke(service, "publication_action", actor.user_id, job_id, "resolve-uncertain", body.model_dump(), key(idempotency_key))

    @app.get("/api/v1/publications/{job_id}/preview")
    async def preview(job_id: int, actor: Actor):
        return await invoke(service, "preview", actor.user_id, job_id)

    @app.get("/api/v1/publications/{job_id}/album")
    async def album(job_id: int, actor: Actor):
        return await invoke(service, "album", actor.user_id, job_id)

    @app.put("/api/v1/publications/{job_id}/album")
    async def put_album(job_id: int, body: VersionedInput, actor: Actor):
        return await invoke(service, "put_album", actor.user_id, job_id, body.model_dump(exclude_unset=True))

    def preparation_route(kind):
        async def preparation(job_id: int, body: ActionInput, actor: Actor, idempotency_key: str | None = Header(None)):
            return await invoke(service, "prepare", actor.user_id, job_id, kind, body.model_dump(exclude_unset=True), key(idempotency_key))
        return preparation

    for kind in ("prepare", "translate-title", "preview-in-telegram", "reimport"):
        app.add_api_route(f"/api/v1/publications/{{job_id}}/{kind}", preparation_route(kind), methods=["POST"], status_code=202)

    @app.post('/api/v1/publications/{job_id}/reimport/apply')
    async def apply_reimport(job_id: int, body: VersionedInput, actor: Actor):
        return await invoke(service,'apply_reimport',actor.user_id,job_id,body.model_dump(exclude_unset=True))

    @app.get("/api/v1/publications/{job_id}/history")
    async def history(job_id: int, actor: Actor):
        return await invoke(service, "history", actor.user_id, job_id)

    @app.get("/api/v1/queue")
    async def queue(actor: Actor, channel_id: int | None = None, limit: int = Query(50,ge=1,le=100),cursor: str | None = None):
        return await invoke(service, "queue", actor.user_id, channel_id=channel_id, limit=limit, cursor=cursor)

    @app.post("/api/v1/channels/{channel_id}/queue/move")
    async def move_queue(channel_id: int, body: QueueMove, actor: Actor, idempotency_key: str | None = Header(None)):
        return await invoke(service, "move_queue", actor.user_id, channel_id, body.model_dump(), key(idempotency_key))

    @app.get("/api/v1/queue/calendar")
    async def calendar(actor: Actor, channel_id: int | None = None, start: str | None = None, end: str | None = None,
                       limit: int = Query(100,ge=1,le=100),cursor: str | None = None):
        return await invoke(service, "calendar", actor.user_id, channel_id=channel_id, start=start, end=end,limit=limit,cursor=cursor)

    @app.put("/api/v1/channels/{channel_id}/queue/order")
    async def reorder(channel_id: int, body: QueueOrder, actor: Actor, idempotency_key: str | None = Header(None)):
        return await invoke(service, "reorder_queue", actor.user_id, channel_id, body.model_dump(), key(idempotency_key))

    @app.post("/api/v1/queue/bulk")
    async def bulk(body: MutationInput, actor: Actor, idempotency_key: str | None = Header(None)):
        return await invoke(service, "bulk", actor.user_id, body.model_dump(), key(idempotency_key))

    @app.get("/api/v1/channels",response_model=ChannelPage)
    async def channels(actor: Actor):
        return await invoke(service, "list_channels", actor.user_id)

    @app.post("/api/v1/channels", status_code=201)
    async def create_channel(body: ChannelInput, actor: Actor, idempotency_key: str | None = Header(None)):
        return await invoke(service, "create_channel", actor.user_id, body.model_dump(exclude_unset=True), key(idempotency_key))

    @app.get("/api/v1/channels/{channel_id}",response_model=ChannelDTO)
    async def channel(channel_id: int, actor: Actor):
        return await invoke(service, "get_channel", actor.user_id, channel_id)

    @app.patch("/api/v1/channels/{channel_id}")
    async def patch_channel(channel_id: int, body: VersionedInput, actor: Actor):
        return await invoke(service, "patch_channel", actor.user_id, channel_id, body.model_dump(exclude_unset=True))

    @app.post("/api/v1/channels/{channel_id}/archive")
    async def archive(channel_id: int, body: VersionedInput, actor: Actor):
        return await invoke(service, "archive_channel", actor.user_id, channel_id, body.model_dump())

    @app.post("/api/v1/channels/{channel_id}/check")
    async def check_channel(channel_id: int, actor: Actor):
        return await invoke(service, "check_channel", actor.user_id, channel_id)

    @app.get("/api/v1/channels/{channel_id}/statistics")
    async def statistics(channel_id: int, actor: Actor):
        return await invoke(service, "channel_statistics", actor.user_id, channel_id)

    @app.get("/api/v1/operations",response_model=OperationPage)
    async def operations(actor: Actor):
        try:result=await invoke(service, "operations", actor.user_id)
        except PlatformError as exc:
            if exc.status!=503:raise
            result={'items':[],'next_cursor':None}
        if gate and deployment:
            for kind in ('migration','backup','restore'):
                for record in gate.list(kind):
                    op=await invoke(deployment,'operation',record['id'])
                    if op:result['items'].append(op)
        return result

    @app.get("/api/v1/operations/{operation_id}",response_model=OperationDTO)
    async def operation(operation_id: str, actor: Actor):
        if deployment:
            controlled=await invoke(deployment,'operation',operation_id)
            if controlled:return controlled
        return await invoke(service, "operation", actor.user_id, operation_id)

    @app.get("/api/v1/operations/{operation_id}/events")
    async def events(operation_id: str, actor: Actor, after_seq: int = Query(0, ge=0)):
        if deployment and await invoke(deployment,'operation',operation_id):
            return await invoke(deployment,'events',operation_id,after_seq=after_seq)
        return await invoke(service, "operation_events", actor.user_id, operation_id, after_seq=after_seq)

    def operation_action(action):
        async def operation_command(operation_id: str, actor: Actor, idempotency_key: str | None = Header(None)):
            if deployment:
                controlled=await invoke(deployment,'operation',operation_id)
                if controlled:
                    auth.require_operation_action(actor,operation_id,'resume' if action=='retry' else action)
                    if controlled['kind']=='backup':return await invoke(deployment,'backup_action',operation_id,action)
                    if controlled['kind']=='migration':return await invoke(deployment,'resume' if action=='retry' else action,operation_id)
                    raise AuthError('restore_review_required','Откройте восстановление и подтвердите проверку очереди',409)
            return await invoke(service, "operation_action", actor.user_id, operation_id, action, {"idempotency_key": key(idempotency_key)})
        return operation_command

    for action in ("cancel", "resume", "retry"):
        app.add_api_route(f"/api/v1/operations/{{operation_id}}/{action}", operation_action(action), methods=["POST"])

    @app.post('/api/v1/operations/{operation_id}/apply')
    async def apply_proposal(operation_id: str,body: VersionedInput,actor: Actor,idempotency_key: str | None=Header(None)):
        return await invoke(service,'apply_translation_proposal',actor.user_id,operation_id,body.model_dump(exclude_unset=True),key(idempotency_key))

    @app.get("/api/v1/settings")
    async def settings(actor: Actor):
        return await invoke(service, "settings", actor.user_id)

    @app.patch("/api/v1/settings")
    async def patch_settings(body: VersionedInput, actor: Actor):
        return await invoke(service, "patch_settings", actor.user_id, body.model_dump(exclude_unset=True))

    @app.post('/api/v1/settings/config-import/plan')
    async def config_plan(body: MutationInput,actor: Actor):
        return await invoke(service,'config_import_plan',actor.user_id,body.model_dump())

    @app.post('/api/v1/settings/config-import/apply')
    async def config_apply(body: MutationInput,actor: Actor,idempotency_key: str | None=Header(None)):
        return await invoke(service,'config_import_apply',actor.user_id,body.model_dump(),key(idempotency_key))

    @app.get('/api/v1/settings/secrets')
    async def secret_status(actor: Actor):
        if not gate:raise AuthError('managed_host_required','Нужен управляющий host',503)
        from app.host.secrets import SecretStore
        return {'status':SecretStore(gate.root).status()}

    @app.post('/api/v1/settings/secrets/{name}')
    async def secret_write(name: str,body: MutationInput,actor: Actor):
        auth.require_fresh(actor)
        if not gate:raise AuthError('managed_host_required','Нужен управляющий host',503)
        gate.assert_mutation_allowed()
        from app.host.secrets import ALLOWED_SECRETS,SecretStore
        if name not in ALLOWED_SECRETS:raise AuthError('unknown_secret','Неизвестная настройка доступа',422)
        value=body.model_dump().get('value')
        if value is not None and (not isinstance(value,str) or len(value)>65536):raise AuthError('invalid_secret','Некорректное значение',422)
        store=SecretStore(gate.root)
        if name=='BOT_TOKEN' and value:
            from aiogram import Bot
            old=store.read(name) or config.bot_token
            try:
                async with Bot(value).context() as candidate:new_identity=await candidate.get_me()
                if old and old!=value:
                    async with Bot(old).context() as existing:old_identity=await existing.get_me()
                    if old_identity.id!=new_identity.id:raise AuthError('bot_identity_changed','Это другой бот. Смена идентичности требует отдельного плана для истории и Telegram-файлов.',409)
            except AuthError:raise
            except Exception:raise AuthError('bot_check_failed','Не удалось проверить токен через Telegram. Предыдущее значение сохранено.',422) from None
        store.write(name,value)
        if name=='BOT_TOKEN':config.bot_token=value or ''
        return {'name':name,'configured':bool(value),'restart_required':True}

    @app.get("/api/v1/system/status")
    async def status(actor: Actor):
        return await invoke(service, "system_status", actor.user_id)

    @app.get("/api/v1/audit")
    async def audit(actor: Actor, limit: int = Query(50, ge=1, le=100)):
        return await invoke(service, "audit", actor.user_id, limit=limit)

    @app.get("/api/v1/publication-control")
    async def publication_control(actor: Actor):
        return gate.status() if gate else await invoke(service, "publication_control", actor.user_id)

    def control_action(action):
        async def command(actor: Actor, idempotency_key: str | None = Header(None)):
            key(idempotency_key)
            if gate:
                if action == "pause":
                    gate.block("user_pause", "user")
                else:
                    gate.unblock("user_pause", "user")
                return gate.status()
            return await invoke(service, "control_action", actor.user_id, action)
        return command

    for action in ("pause", "resume"):
        app.add_api_route(f"/api/v1/publication-control/{action}", control_action(action), methods=["POST"])

    @app.get("/api/v1/assets/{asset_id}")
    async def asset(asset_id: str, actor: Actor):
        return await invoke(service, "asset_metadata", actor.user_id, asset_id)

    def asset_file(thumbnail):
        async def content(asset_id: str, actor: Actor):
            result = await invoke(service, "asset_content", actor.user_id, asset_id, thumbnail=thumbnail)
            return FileResponse(result["path"], media_type=result["mime_type"], filename=None)
        return content

    app.add_api_route("/api/v1/assets/{asset_id}/content", asset_file(False), methods=["GET"])
    app.add_api_route("/api/v1/assets/{asset_id}/thumbnail", asset_file(True), methods=["GET"])

    @app.post("/api/v1/assets/uploads", status_code=201)
    async def upload(file: UploadFile, actor: Actor):
        data = await file.read(config.upload_limit + 1)
        if len(data) > config.upload_limit:
            raise AuthError("upload_too_large", "Файл превышает допустимый размер", 413)
        return await invoke(service, "upload_asset", actor.user_id, data, filename=file.filename or "upload")

    # These calls all go through the same service and authorization boundary.
    def ai_read_route(collection):
        async def ai_collection(actor: Actor):
            return await invoke(service,"ai_"+collection,actor.user_id)
        return ai_collection

    for collection in ('workers','models','tasks'):
        app.add_api_route('/api/v1/ai/'+collection,ai_read_route(collection),methods=['GET'],name='ai_'+collection)

    @app.post("/api/v1/ai/workers/pair")
    async def pair_worker(body: MutationInput, actor: Actor):
        auth.require_fresh(actor)
        return await invoke(service, "pair_worker", actor.user_id, body.model_dump())

    @app.put("/api/v1/ai/selection")
    async def ai_selection(body: MutationInput, actor: Actor):
        return await invoke(service, "ai_selection", actor.user_id, body.model_dump())

    @app.post("/api/v1/ai/workers/{worker_id}/revoke")
    async def revoke_worker(worker_id: str, actor: Actor):
        return await invoke(service, "revoke_worker", actor.user_id, worker_id)

    @app.post("/api/v1/ai/tasks/{task_id}/{action}")
    async def ai_action(task_id: int, action: str, body: VersionedInput, actor: Actor):
        if action not in {"cancel", "retry", "reassign"}:
            raise HTTPException(404)
        return await invoke(service, "ai_task_action", actor.user_id, task_id, action, body.model_dump())

    if deployment is not None:
        from app.api.deployment_routes import add_deployment_routes
        add_deployment_routes(app, deployment, principal, auth, key)

    def worker_route(operation):
        async def worker(body: MutationInput, request: Request):
            credential = request.headers.get("authorization", "").removeprefix("Bearer ")
            result = await invoke(service, "worker_command", operation, body.model_dump(), credential)
            return Response(status_code=204) if operation == 'lease' and result is None else result
        return worker

    for operation, path, verb in (("register", "register", "POST"), ("heartbeat", "heartbeat", "POST"),
                                    ("capabilities", "capabilities", "PUT"), ("lease", "tasks/lease", "POST")):
        app.add_api_route("/api/worker/v1/" + path, worker_route(operation), methods=[verb])

    @app.get('/api/v1/deployment/probe/{nonce}')
    async def route_probe(nonce: str):
        import time
        if gate is None or len(nonce) > 128:
            raise HTTPException(404)
        probe = gate.load('route-probe', nonce)
        if not probe or probe.get('expires_at', 0) <= time.time():
            raise HTTPException(404)
        return {key: probe[key] for key in ('node_id', 'system_id', 'generation', 'challenge')}

    @app.post("/api/worker/v1/tasks/{task_id}/{action}")
    async def worker_task(task_id: int, action: str, body: MutationInput, request: Request):
        if action not in {"progress", "complete", "fail", "release"}:
            raise HTTPException(404)
        credential = request.headers.get("authorization", "").removeprefix("Bearer ")
        return await invoke(service, "worker_command", action, {**body.model_dump(), "task_id": task_id}, credential)

    @app.get('/api/news/health')
    async def legacy_health(request: Request):
        return await invoke(service,'legacy_worker_command','health',{},request.headers.get('authorization','').removeprefix('Bearer '))

    @app.post('/api/news/tasks/lease')
    async def legacy_lease(body: MutationInput,request: Request):
        result=await invoke(service,'legacy_worker_command','lease',body.model_dump(),request.headers.get('authorization','').removeprefix('Bearer '))
        return Response(status_code=204) if result is None else result

    @app.post('/api/news/tasks/{task_id}/{action}')
    async def legacy_task(task_id: int,action: str,body: MutationInput,request: Request):
        if action not in {'progress','complete','fail'}:raise HTTPException(404)
        return await invoke(service,'legacy_worker_command',action,{**body.model_dump(),'task_id':task_id},request.headers.get('authorization','').removeprefix('Bearer '))

    if config.desktop:
        from app.desktop.routes import install_routes
        install_routes(app, principal, auth, service, gate)

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        root = config.frontend.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(404)
        if target.is_file():
            return FileResponse(target)
        if (root / "index.html").is_file():
            return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
        return JSONResponse({"error": {"code": "frontend_not_built", "message": "Соберите веб-интерфейс по инструкции установки"}}, status_code=503)

    return app
