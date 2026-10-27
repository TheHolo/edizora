"""Durable Telegram inbox and wizard state for the managed runtime."""
import asyncio
import json
from contextvars import ContextVar
from dataclasses import asdict
from aiogram import Dispatcher
from aiogram.types import Update
from aiogram.fsm.storage.base import BaseStorage
from sqlalchemy import select, func, text
from app.db.models import TelegramInbox, TelegramState, utcnow

current_update = ContextVar("current_telegram_update",default=None)


class PersistentStorage(BaseStorage):
    def __init__(self,sessions):self.sessions=sessions
    @staticmethod
    def _key(key):return json.dumps(asdict(key),sort_keys=True,separators=(",",":"))
    async def _get(self,key):
        async with self.sessions() as session:
            row=await session.get(TelegramState,self._key(key))
            return (row.state,dict(row.data)) if row else (None,{})
    async def get_state(self,key):return (await self._get(key))[0]
    async def get_data(self,key):return (await self._get(key))[1]
    async def set_state(self,key,state=None):await self._set(key,state=state.state if hasattr(state,"state") else state)
    async def set_data(self,key,data):await self._set(key,data=dict(data))
    async def _set(self,key,**values):
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row=await session.get(TelegramState,self._key(key))
            if not row:row=TelegramState(key=self._key(key),state=None,data={});session.add(row)
            for field,value in values.items():setattr(row,field,value)
            await session.commit()
    async def update_data(self,key,data):
        async with self.sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            row=await session.get(TelegramState,self._key(key))
            if not row:row=TelegramState(key=self._key(key),state=None,data={});session.add(row)
            row.data={**row.data,**dict(data)};result=dict(row.data);await session.commit();return result
    async def close(self):pass


class DurableDispatcher(Dispatcher):
    def __init__(self,*,sessions,gate=None,**kwargs):
        self.inbox_sessions=sessions;self.gate=gate;self.accepting=True;self.inflight=False
        super().__init__(**kwargs)

    async def persist_updates(self,bot_id,updates):
        # Commit the complete received group before any later getUpdates offset can acknowledge it.
        async with self.inbox_sessions() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            for update in updates:
                existing=await session.scalar(select(TelegramInbox.id).where(TelegramInbox.bot_id==str(bot_id),TelegramInbox.update_id==update.update_id))
                if existing is None:session.add(TelegramInbox(bot_id=str(bot_id),update_id=update.update_id,payload=update.model_dump(mode="json",exclude_none=True),status="received"))
            await session.commit()

    async def process_inbox(self,bot,**kwargs):
        async with self.inbox_sessions() as session:
            rows=list((await session.scalars(select(TelegramInbox).where(TelegramInbox.bot_id==str(bot.id),TelegramInbox.status!="processed").order_by(TelegramInbox.update_id))).all())
        processed=[]
        self.inflight=bool(rows)
        try:
            for row in rows:
                token=current_update.set((str(bot.id),row.update_id))
                try:
                    await self.feed_update(bot,Update.model_validate(row.payload,context={"bot":bot}),**kwargs)
                    processed.append(row.id)
                finally:current_update.reset(token)
            # Album collectors expose their tasks. Their persisted update group remains
            # unacknowledged in our inbox until the final business command commits.
            for router in self.sub_routers:
                pending=getattr(router,"forwarded_album_tasks",None)
                if pending:
                    await asyncio.gather(*list(pending))
            if processed:
                async with self.inbox_sessions() as session,session.begin():
                    for row_id in processed:
                        row=await session.get(TelegramInbox,row_id);row.status="processed";row.processed_at=utcnow()
        finally:self.inflight=False

    async def _polling(self,bot,polling_timeout=15,allowed_updates=None,**kwargs):
        # No drop_pending_updates: inbox survives restart and migration.
        self.accepting=True
        while self.accepting:
            if self.gate:
                try:self.gate.assert_mutation_allowed()
                except RuntimeError:
                    await asyncio.sleep(.25);continue
            await self.process_inbox(bot,**kwargs)
            async with self.inbox_sessions() as session:
                last=await session.scalar(select(func.max(TelegramInbox.update_id)).where(TelegramInbox.bot_id==str(bot.id)))
            updates=await bot.get_updates(offset=last+1 if last is not None else None,timeout=min(polling_timeout,15),allowed_updates=allowed_updates)
            if updates:await self.persist_updates(bot.id,updates)

    async def close_and_drain(self):
        self.accepting=False
        while self.inflight:await asyncio.sleep(.05)
