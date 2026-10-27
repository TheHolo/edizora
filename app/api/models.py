from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalExchange(InputModel):
    code: str = Field(min_length=20, max_length=128)


class PasswordLogin(InputModel):
    telegram_id: int
    password: str = Field(min_length=1, max_length=1024)


class TelegramExchange(InputModel):
    init_data: str = Field(min_length=1, max_length=16384)
    exchange_nonce: str = Field(min_length=16, max_length=128)


class PasswordBody(InputModel):
    password: str = Field(min_length=1, max_length=1024)


class ChangePassword(PasswordBody):
    new_password: str = Field(min_length=12, max_length=1024)


class DraftInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    content_kind: Literal["artwork", "news", "text"] = "text"
    channel_id: int | None = None
    target_channel_id: int | None = None
    title: str = Field(default="", max_length=2000)
    text: str = Field(default="", max_length=50000)
    source_url: str | None = Field(default=None, max_length=8192)
    urls: list[str] = Field(default_factory=list, max_length=50)
    user_tags: list[str] = Field(default_factory=list, max_length=100)


class PublicationPatch(BaseModel):
    model_config = ConfigDict(extra="allow")
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=2000)
    title_translation: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=50000)
    user_tags: list[str] | None = Field(default=None, max_length=100)
    manual_caption: str | None = Field(default=None, max_length=4096)


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    expected_version: int = Field(ge=1)
    scheduled_at: str | None = None
    timezone: str = "Asia/Vladivostok"


class UncertainResolution(InputModel):
    expected_version: int = Field(ge=1)
    outcome: Literal["published", "not_published", "partial", "still_unknown"]
    note: str = Field(default="", max_length=4000)
    message_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(default_factory=list, max_length=1000)


class QueueMove(InputModel):
    expected_queue_revision: int = Field(ge=0)
    job_id: int = Field(ge=1)
    direction: Literal["up", "down"]


class ChannelInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    alias: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    telegram_chat_id: str = Field(min_length=1, max_length=64)


class VersionedInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    expected_version: int = Field(ge=1)


class QueueOrder(InputModel):
    expected_queue_revision: int = Field(ge=0)
    ordered_job_ids: list[int] = Field(max_length=10000)


class MutationInput(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChannelDTO(BaseModel):
    model_config=ConfigDict(extra='allow')
    id:int
    version:int
    alias:str
    title:str
    telegram_chat_id:str
    is_enabled:bool
    is_paused:bool
    is_default:bool
    publish_mode:str
    publish_interval_seconds:int
    caption_template:str|None=None
    default_tags:list[str]=Field(default_factory=list)
    next_publish_at:str|None=None
    active_job_id:int|None=None
    permissions:dict[str,Any]=Field(default_factory=dict)
    member_count:int|None=None
    member_count_updated_at:str|None=None


class ChannelPage(BaseModel):
    model_config=ConfigDict(extra='allow')
    items:list[ChannelDTO]
    next_cursor:str|None=None


class ReadinessDTO(BaseModel):
    ready:bool
    reasons:list[str]=Field(default_factory=list)


class AlbumSummary(BaseModel):
    selected:int
    total:int
    cover:str|None=None
    initial_selected_asset_ids:list[str]=Field(default_factory=list)
    selection_limit:int|None=None


class ScheduleDTO(BaseModel):
    kind:str
    utc:str|None=None
    timezone:str
    estimate:str|None=None


class PublicationDTO(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    version: int
    content_kind: str
    status: str
    title: str
    allowed_actions: list[str]
    error:dict[str,Any]|None=None
    source:dict[str,Any]
    channel:ChannelDTO
    tags:list[str]
    album:AlbumSummary
    readiness:ReadinessDTO
    schedule:ScheduleDTO
    last_operation_id:str|None=None
    blocked_reason:str|None=None
    updated_at:str
    created_at:str
    queue_position:int|None=None


class ProgressDTO(BaseModel):
    unit:Literal['bytes','files','items']
    completed:int
    total:int|None=None
    percent:float|None=None
    estimated_seconds_remaining:float|None=None


class StageDTO(BaseModel):
    model_config=ConfigDict(extra='allow')
    key:str
    status:str
    title:str|None=None


class OperationDTO(BaseModel):
    model_config=ConfigDict(extra='allow')
    id:str
    kind:str
    status:Literal['queued','running','waiting_external','waiting_worker','waiting_user','cancelling','cancelled','failed','succeeded']
    stage_key:str
    stages:list[StageDTO]=Field(default_factory=list)
    version:int
    created_at:str
    started_at:str|None=None
    updated_at:str
    heartbeat_at:str|None=None
    message:str|None=None
    progress:ProgressDTO|None=None
    result:dict[str,Any]|None=None
    error:dict[str,Any]|None=None
    allowed_actions:list[str]
    last_event_seq:int


class OperationPage(BaseModel):
    items:list[OperationDTO]
    next_cursor:str|None=None


class PublicationPage(BaseModel):
    model_config = ConfigDict(extra="allow")
    items: list[PublicationDTO]
    next_cursor: str | None = None
    total: int | None = None


class Accepted(BaseModel):
    model_config = ConfigDict(extra="allow")
    operation_id: str | None = None
    resource_id: int | str | None = None


class ErrorDetails(BaseModel):
    code: str
    message: str
    retryable: bool = False
    field_errors: dict[str, Any] | list[Any] | None = None
    current_version: int | None = None
    request_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetails
