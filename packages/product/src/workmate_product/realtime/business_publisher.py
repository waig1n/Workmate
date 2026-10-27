"""Replay-safe business notifications, separate from execution and text streaming.

Database leases commit before any Redis operation. The stable Product event ID
survives response loss and lease expiry; retry repeats a notification, never its
business operation. Ordinary outages retry indefinitely with bounded backoff;
invalid protocol is retained as blocked evidence for explicit repair.
"""
import asyncio
import logging
import random

from workmate_contracts.realtime import RealtimeEnvelope
from ..persistence.repositories import realtime_outbox as repository
from .classification import is_business_event

logger=logging.getLogger(__name__)


class BusinessPublisher:
    def __init__(self,sessions,log_manager,log,policy):
        self.sessions,self.log_manager,self.log,self.policy=sessions,log_manager,log,policy

    async def publish_once(self):
        async with self.sessions.begin() as db:
            claims=await repository.lease_due(db,limit=self.policy.batch_size,
                                            lease_seconds=self.policy.lease_seconds)
        async with asyncio.TaskGroup() as group:
            for claim in claims:
                group.create_task(self._publish_claim(claim))
        return len(claims)

    async def _publish_claim(self,claim):
        try:
            event=claim.event
            if not is_business_event(event.event_type,event.payload):
                raise ValueError('Ephemeral event is not a business notification')
            envelope=RealtimeEnvelope(event_id=event.product_event_id,
                conversation_id=event.conversation_id,task_id=event.product_task_id,
                reply_id=event.reply_id,source='business',business_seq=claim.business_seq,
                payload=event.payload)
        except ValueError as error:
            await self._defer(claim,error,invalid=True)
            return
        try:
            # Leave budget for the PG receipt; cancellation keeps the committed
            # claim leased so another publisher can safely retry after expiry.
            async with asyncio.timeout(self.policy.lease_seconds/2):
                ref=await self.log_manager.ensure(event.conversation_id)
                appended=await self.log.append(ref,envelope)
                if (appended.cursor.conversation_id!=ref.conversation_id
                        or appended.cursor.generation!=ref.generation):
                    raise ValueError('Publication receipt belongs to another log')
                current=await self.log_manager.is_current(ref)
        except Exception as error:
            # ValueError is also used for transient coordinator conditions such
            # as an unfinished generation handshake. Only the local envelope
            # validation above can classify an intent as invalid protocol.
            await self._defer(claim,error,invalid=False)
            return
        async with self.sessions.begin() as db:
            if current and await repository.published(db,claim,generation=ref.generation,
                    incarnation=ref.incarnation,stream_id=appended.cursor.stream_id):
                return
            # The repository additionally locks and checks the active PG log,
            # closing the race after the optimistic is_current network check.
            await repository.failed(db,claim,error_code='generation_changed',retry_seconds=0)

    async def _defer(self,claim,error,*,invalid):
        delay=random.uniform(.5,1)*min(self.policy.retry_max_seconds,2**min(claim.attempt_count,20))
        async with self.sessions.begin() as db:
            await repository.failed(db,claim,error_code='protocol_invalid' if invalid else 'log_unavailable',
                blocked=invalid,retry_seconds=delay)
        # Do not log credentials, exception text, payloads or tool content.
        logger.warning('Business publication deferred exception_type=%s',type(error).__name__)

    async def serve(self,shutdown):
        while not shutdown.is_set():
            cycle=asyncio.create_task(self.publish_once())
            stopping=asyncio.create_task(shutdown.wait())
            try:
                done,_=await asyncio.wait((cycle,stopping),return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    return
                # Each Conversation leases only its earliest unpublished head.
                # Drain the next head immediately instead of delaying every
                # tool delta and its following approval/terminal notification.
                if await cycle:
                    continue
            except Exception as error:
                logger.warning('Business outbox cycle deferred exception_type=%s',type(error).__name__)
            finally:
                for task in (cycle,stopping):
                    if not task.done(): task.cancel()
                await asyncio.gather(cycle,stopping,return_exceptions=True)
            try:
                await asyncio.wait_for(shutdown.wait(),self.policy.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
