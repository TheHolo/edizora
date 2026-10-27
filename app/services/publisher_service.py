from datetime import UTC, datetime
from typing import Any
from contextvars import ContextVar
from uuid import uuid4
import hashlib

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from app.db.models import Channel, Job, PublicationAttempt, PublicationPart, utcnow
from app.domain.enums import ContentKind, MediaType
from app.domain.exceptions import (
    ChannelPermissionError,
    PublishError,
    UncertainPublishError,
)
from app.domain.models import PreparedMedia, PublicationResult, SourcePost
from app.utils.text import telegram_html_length


_send_context = ContextVar("publication_send_context", default=None)


class _JournalBot:
    def __init__(self, publisher, bot):
        self.publisher, self.original = publisher, bot

    def __getattr__(self, name):
        method = getattr(self.original, name)
        if not name.startswith("send_"):
            return method
        async def send(*args, **kwargs):
            context = _send_context.get()
            if context is None:
                return await method(*args, **kwargs)
            publisher = self.publisher
            if publisher.gate:
                publisher.gate.validate_permit(context["permit"])
            part_id = None
            if publisher.sessions and context.get("attempt_id"):
                async with publisher.sessions() as session, session.begin():
                    part = PublicationPart(attempt_id=context["attempt_id"], sequence=context["sequence"],
                        request_digest=hashlib.sha256(repr((name,args,kwargs)).encode()).hexdigest(), status="unknown",message_ids=[])
                    context["sequence"] += 1
                    session.add(part)
                    await session.flush()
                    part_id=part.id
            try:
                result = await method(*args, **kwargs)
                context["sent_any"]=True
            except BaseException as error:
                if part_id:
                    try:
                        async with publisher.sessions() as session,session.begin():
                            part=await session.get(PublicationPart,part_id)
                            # Bot API's explicit rejection proves this call did not send.
                            part.status="rejected" if isinstance(error,(TelegramBadRequest,TelegramForbiddenError)) else "unknown"
                    except Exception:
                        pass
                if not isinstance(error,(TelegramBadRequest,TelegramForbiddenError)):
                    raise UncertainPublishError("Ответ Telegram не подтверждён; автоматический повтор запрещён") from error
                raise
            if part_id:
                ids=[m.message_id for m in result] if isinstance(result,list) else [result.message_id]
                try:
                    async with publisher.sessions() as session,session.begin():
                        part=await session.get(PublicationPart,part_id)
                        part.status="confirmed";part.message_ids=ids
                except Exception as error:
                    raise UncertainPublishError("Telegram ответил, но результат не сохранён; проверьте канал перед повтором") from error
            return result
        return send


class TelegramPublisher:
    def __init__(self, bot: Bot, *, sessions=None, gate=None) -> None:
        self.sessions,self.gate=sessions,gate
        self.bot = _JournalBot(self,bot) if sessions is not None or gate is not None else bot

    async def publish(self, job: Job, post: SourcePost, media: list[PreparedMedia], channel: Channel, caption: str) -> PublicationResult:
        permit=self.gate.acquire_permit(job.id,job.version,channel.id) if self.gate else None
        attempt_id=str(uuid4()) if self.sessions else None
        if attempt_id:
            try:
                async with self.sessions() as session,session.begin():
                    session.add(PublicationAttempt(id=attempt_id,job_id=job.id,job_version=job.version,
                        node_id=permit.get("node_id","legacy") if permit else "legacy",
                        generation=permit.get("generation",0) if permit else 0,
                        render_plan=job.render_plan or {"caption_hash":hashlib.sha256(caption.encode()).hexdigest(),"media_count":len(media)},stage="sending"))
            except BaseException:
                if self.gate:self.gate.release_permit(permit)
                raise
        context={"permit":permit,"attempt_id":attempt_id,"sequence":1,"sent_any":False}
        token=_send_context.set(context)
        try:
            result=await self._send(channel.telegram_chat_id, media, caption, post.content_kind)
            if attempt_id:
                try:
                    async with self.sessions() as session,session.begin():
                        attempt=await session.get(PublicationAttempt,attempt_id);attempt.stage="confirmed";attempt.completed_at=utcnow()
                except Exception as error:
                    raise UncertainPublishError("Telegram принял отправку, но финальная запись не сохранена; проверьте канал") from error
            return result
        except BaseException as error:
            if context["sent_any"] and not isinstance(error,UncertainPublishError):
                error=UncertainPublishError("Отправка подтверждена частично; автоматический повтор запрещён")
            if attempt_id:
                try:
                    async with self.sessions() as session,session.begin():
                        attempt=await session.get(PublicationAttempt,attempt_id)
                        attempt.stage="unknown" if isinstance(error,UncertainPublishError) else "failed"
                        attempt.error=getattr(error,"code",type(error).__name__)
                except Exception:
                    # Keep the uncertainty error even while the database itself is unavailable.
                    pass
            raise error
        finally:
            _send_context.reset(token)
            if self.gate:self.gate.release_permit(permit)

    async def preview(
        self, chat_id: int | str, media: list[PreparedMedia], caption: str,
        post: SourcePost | None = None,
    ) -> PublicationResult:
        kind = post.content_kind if post is not None else ContentKind.ARTWORK
        permit=self.gate.acquire_permit(0,0) if self.gate else None
        token=_send_context.set({"permit":permit,"attempt_id":None,"sequence":1})
        try:return await self._send(chat_id, media, caption, kind)
        finally:
            _send_context.reset(token)
            if self.gate:self.gate.release_permit(permit)

    async def _send(
        self, chat_id: int | str, media: list[PreparedMedia], caption: str,
        content_kind: ContentKind = ContentKind.ARTWORK,
    ) -> PublicationResult:
        if content_kind == ContentKind.NEWS:
            return await self._send_news(chat_id, media, caption)
        if not media:
            raise PublishError("Публикация не содержит медиафайлов")
        return await self._send_media(chat_id, media, caption)

    async def _send_news(
        self, chat_id: int | str, media: list[PreparedMedia], text: str,
    ) -> PublicationResult:
        media_result: PublicationResult | None = None
        try:
            if not media:
                message = await self.bot.send_message(chat_id, text, parse_mode="HTML")
                return PublicationResult(
                    str(chat_id), [message.message_id], datetime.now(UTC), 0,
                )
            if self._plain_length(text) <= 1024:
                return await self._send_media(chat_id, media, text)

            media_result = await self._send_media(chat_id, media, "")
            message = await self.bot.send_message(chat_id, text, parse_mode="HTML")
            return PublicationResult(
                str(chat_id), [*media_result.message_ids, message.message_id],
                datetime.now(UTC), len(media),
            )
        except (ChannelPermissionError, PublishError, UncertainPublishError):
            raise
        except TelegramForbiddenError as error:
            if media_result is not None and media_result.message_ids:
                raise self._partial_publish_error() from error
            raise ChannelPermissionError("Бот не имеет прав на публикацию в канале") from error
        except TelegramNetworkError as error:
            if media_result is not None and media_result.message_ids:
                raise self._partial_publish_error() from error
            raise UncertainPublishError(
                "Ответ Telegram потерян; проверьте канал перед ручным повтором"
            ) from error
        except TelegramBadRequest as error:
            if media_result is not None and media_result.message_ids:
                raise self._partial_publish_error() from error
            raise PublishError(str(error)) from error
        except TelegramAPIError as error:
            if media_result is not None and media_result.message_ids:
                raise self._partial_publish_error() from error
            raise PublishError(str(error)) from error

    async def _send_media(
        self, chat_id: int | str, media: list[PreparedMedia], caption: str,
    ) -> PublicationResult:
        message_ids: list[int] = []
        try:
            if len(media) == 1:
                item = media[0]
                payload = self._payload(item)
                if item.media_type == MediaType.VIDEO and not item.as_document:
                    message = await self.bot.send_video(
                        chat_id, payload, caption=caption or None,
                        parse_mode="HTML" if caption else None,
                    )
                elif item.media_type == MediaType.ANIMATION and not item.as_document:
                    message = await self.bot.send_animation(
                        chat_id, payload, caption=caption or None,
                        parse_mode="HTML" if caption else None,
                    )
                elif item.as_document or item.media_type == MediaType.DOCUMENT:
                    message = await self.bot.send_document(
                        chat_id, payload, caption=caption or None,
                        parse_mode="HTML" if caption else None,
                    )
                else:
                    message = await self.bot.send_photo(
                        chat_id, payload, caption=caption or None,
                        parse_mode="HTML" if caption else None,
                    )
                message_ids.append(message.message_id)
            else:
                has_documents = any(
                    item.as_document or item.media_type == MediaType.DOCUMENT for item in media
                )
                send_as_documents = has_documents and all(
                    not item.telegram_file_id
                    or item.as_document
                    or item.media_type == MediaType.DOCUMENT
                    for item in media
                )
                for offset in range(0, len(media), 10):
                    chunk = media[offset : offset + 10]
                    chunk_caption = caption if offset == 0 else None
                    can_group = self._can_group(chunk, send_as_documents)
                    if len(chunk) == 1 or not can_group:
                        if len(chunk) > 1 and not can_group:
                            for index, item in enumerate(chunk):
                                single_caption = chunk_caption if index == 0 else None
                                result = await self._send_media(
                                    chat_id, [item], single_caption or "",
                                )
                                message_ids.extend(result.message_ids)
                            continue
                        item = chunk[0]
                        payload = self._payload(item)
                        if item.media_type == MediaType.VIDEO and not send_as_documents:
                            message = await self.bot.send_video(
                                chat_id, payload, caption=chunk_caption,
                                parse_mode="HTML" if chunk_caption else None,
                            )
                        elif item.media_type == MediaType.ANIMATION and not send_as_documents:
                            message = await self.bot.send_animation(
                                chat_id, payload, caption=chunk_caption,
                                parse_mode="HTML" if chunk_caption else None,
                            )
                        elif send_as_documents:
                            message = await self.bot.send_document(
                                chat_id, payload, caption=chunk_caption,
                                parse_mode="HTML" if chunk_caption else None,
                            )
                        else:
                            message = await self.bot.send_photo(
                                chat_id, payload, caption=chunk_caption,
                                parse_mode="HTML" if chunk_caption else None,
                            )
                        message_ids.append(message.message_id)
                        continue
                    group: list[Any] = []
                    for index, item in enumerate(chunk):
                        item_caption = chunk_caption if index == 0 else None
                        if send_as_documents:
                            group.append(InputMediaDocument(
                                media=self._payload(item), caption=item_caption,
                                parse_mode="HTML" if item_caption else None,
                            ))
                        elif item.media_type == MediaType.VIDEO:
                            group.append(InputMediaVideo(
                                media=self._payload(item), caption=item_caption,
                                parse_mode="HTML" if item_caption else None,
                            ))
                        else:
                            group.append(InputMediaPhoto(
                                media=self._payload(item), caption=item_caption,
                                parse_mode="HTML" if item_caption else None,
                            ))
                    messages = await self.bot.send_media_group(chat_id, group)
                    message_ids.extend(message.message_id for message in messages)
        except UncertainPublishError:
            raise
        except (ChannelPermissionError, PublishError) as error:
            if message_ids:
                raise self._partial_publish_error() from error
            raise
        except TelegramForbiddenError as error:
            if message_ids:
                raise self._partial_publish_error() from error
            raise ChannelPermissionError("Бот не имеет прав на публикацию в канале") from error
        except TelegramNetworkError as error:
            raise UncertainPublishError(
                "Ответ Telegram потерян; проверьте канал перед ручным повтором"
            ) from error
        except TelegramBadRequest as error:
            if message_ids:
                raise self._partial_publish_error() from error
            raise PublishError(str(error)) from error
        except TelegramAPIError as error:
            if message_ids:
                raise self._partial_publish_error() from error
            raise PublishError(str(error)) from error
        return PublicationResult(str(chat_id), message_ids, datetime.now(UTC), len(media))

    @staticmethod
    def _payload(item: PreparedMedia) -> str | FSInputFile:
        if item.telegram_file_id:
            return item.telegram_file_id
        if item.path is None:
            raise PublishError("Медиафайл недоступен")
        return FSInputFile(item.path)

    @staticmethod
    def _can_group(items: list[PreparedMedia], as_documents: bool) -> bool:
        if as_documents:
            return True
        return all(item.media_type in {MediaType.IMAGE, MediaType.VIDEO} for item in items)

    @staticmethod
    def _plain_length(value: str) -> int:
        return telegram_html_length(value)

    @staticmethod
    def _partial_publish_error() -> UncertainPublishError:
        return UncertainPublishError(
            "Публикация отправлена частично; проверьте канал перед ручным повтором"
        )
