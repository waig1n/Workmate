"""Coordinate log identities in short PG transactions, with Redis I/O between them."""
import asyncio
import uuid

from workmate_contracts.realtime import RealtimeCursor
from ..persistence.repositories import event_logs as repository
from ..persistence.repositories.event_logs import LogUnavailable, LogState
from .redis_log import LogRef, LogLost


class EventLogManager:
    def __init__(self,sessions,log,*,lease_seconds=30):
        if type(lease_seconds) is not int or not 3<=lease_seconds<=300:
            raise ValueError('Log initialization lease must be from 3 to 300 seconds')
        self.sessions,self.log,self.lease_seconds=sessions,log,lease_seconds

    @staticmethod
    def _ref(state):
        return LogRef(*state.identity)

    async def _read(self,conversation_id):
        async with self.sessions.begin() as db:
            return await repository.read(db,conversation_id)

    async def _prepare(self,conversation_id,*,expected=None):
        if isinstance(expected,LogRef):
            expected=(expected.conversation_id,expected.generation,expected.incarnation)
        async with self.sessions.begin() as db:
            return await repository.prepare(db,conversation_id,lease_seconds=self.lease_seconds,expected=expected)

    async def _activate(self,state,ref):
        async with self.sessions.begin() as db:
            return await repository.activate(db,state,(ref.conversation_id,ref.generation,ref.incarnation))

    async def ensure(self,conversation_id):
        RealtimeCursor(conversation_id=conversation_id,generation=str(uuid.uuid4()),stream_id='0-0')
        for _ in range(4):
            current=await self._read(conversation_id)
            if current is not None and current.status=='active':
                try:
                    async with asyncio.timeout(self.lease_seconds/2):
                        await self.log.window(self._ref(current))
                except LogLost:
                    current=await self._prepare(conversation_id,expected=self._ref(current))
                else:
                    if await self.is_current(self._ref(current)):
                        return self._ref(current)
                    continue
            else:
                current=await self._prepare(conversation_id)
            if current.status!='initializing':
                continue
            try:
                async with asyncio.timeout(self.lease_seconds/2):
                    ref=await self.log.initialize(conversation_id,current.generation)
            except LogLost:
                await self._prepare(conversation_id,expected=current)
                continue
            if await self._activate(current,ref):
                return ref
        raise LogUnavailable('Concurrent log rotation requires a new handshake')

    async def is_current(self,ref):
        try:
            current=await self._read(ref.conversation_id)
        except LogUnavailable:
            return False
        return current is not None and current.status=='active' and self._ref(current)==ref
