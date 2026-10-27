"""Worker health hints and bounded internal resource-release notifications."""
import asyncio
import logging
import time

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from asyncpg import CannotConnectNowError, ConnectionDoesNotExistError, ConnectionFailureError
from workmate_product.coordination.command_queue import QueueRoute
from workmate_product.coordination.worker_registry import WorkerRecord
from workmate_product.persistence.repositories.command_deliveries import quarantine_message
from .workspace_residency import WorkspaceResidencyCoordinator


logger=logging.getLogger(__name__)


class WorkerWorkspaceLifecycle:
    def __init__(self,sessions,service,workspaces,registry,release_queue,*,capacity):
        self.sessions,self.service,self.workspaces=sessions,service,workspaces
        self.registry,self.queue,self.capacity=registry,release_queue,capacity
        self.route=QueueRoute(service.node_id,service.worker_id,True)
        self.ttl=int(service.lease_seconds)
        self._pending_after='-'
        self._repair_after=''
        self._drain_requests=set()
        self.coordinator=WorkspaceResidencyCoordinator(sessions,service.node_id,
            registry=registry,release_queue=release_queue)

    async def heartbeat(self,*,draining):
        await self.registry.heartbeat(WorkerRecord(incarnation=self.service.worker_id,
            node_id=self.service.node_id,total_capacity=self.capacity,
            available_capacity=self.service.available_capacity,draining=draining),ttl_seconds=self.ttl)
        for hold in self.workspaces.affinity_holds():
            record=WorkspaceResidencyCoordinator.affinity(hold)
            if draining:
                await self.registry.withdraw_affinity(record)
                identity=(record.session_id,record.hold_version)
                if identity not in self._drain_requests:
                    await self.coordinator.request_idle_release(hold)
                    self._drain_requests.add(identity)
            else:
                await self.registry.publish_affinity(record,ttl_seconds=self.ttl)

    async def _disposition(self,message):
        envelope=message.envelope
        error=message.protocol_error
        if envelope is not None:
            async with self.sessions.begin() as db:
                row=(await db.execute(text('SELECT * FROM workspace_release_requests WHERE id=:id'),
                    {'id':envelope.request_id})).mappings().first()
            if row is None or any(row[key]!=value for key,value in {
                    'session_id':envelope.session_id,'storage_node_id':envelope.storage_node_id,
                    'expected_worker_id':envelope.target_incarnation,
                    'workspace_generation':envelope.workspace_generation,
                    'expected_hold_version':envelope.expected_hold_version}.items()):
                error='release_identity_mismatch'
            elif row['status'] in {'released','stale','blocked'}:
                return True
            else:
                await self._release(row)
                async with self.sessions.begin() as db:
                    status=await db.scalar(text('SELECT status FROM workspace_release_requests WHERE id=:id'),
                        {'id':envelope.request_id})
                return status in {'released','stale','blocked'}
        async with self.sessions.begin() as db:
            await quarantine_message(db,self.queue.stream_key(self.route),message.message_id,
                error or 'invalid_envelope')
        return True

    async def _release(self,row):
        try:
            await self.workspaces.release_idle_session(row['session_id'],
                row['expected_hold_version'],row['workspace_generation'])
        except Exception as error:
            async with self.sessions.begin() as db:
                status=await db.scalar(text('SELECT status FROM workspace_release_requests WHERE id=:id'),
                    {'id':row['id']})
            if status!='blocked':
                raise
            self.service.failures[row['session_id']]=type(error).__name__
            logger.error('Workspace release blocked session_id=%s exception_type=%s',
                row['session_id'],type(error).__name__)

    async def release_tick(self):
        await self.queue.ensure_group(self.route)
        pending=await self.queue.pending(self.route,count=32,start=self._pending_after)
        self._pending_after='('+pending[-1].message_id if pending else '-'
        messages=await self.queue.claim_pending(self.route,self.service.worker_id,
            [item.message_id for item in pending],min_idle_ms=0) if pending else []
        if len(messages)<32:
            messages+=await self.queue.read(self.route,self.service.worker_id,count=32-len(messages))
        for message in messages:
            if await self._disposition(message):
                await self.queue.ack(self.route,[message.message_id])

    async def repair_releases(self):
        # Only durable resource requests, never Product execution candidates.
        # Keyset traversal keeps busy old requests from starving other sessions.
        async with self.sessions.begin() as db:
            rows=(await db.execute(text("""SELECT * FROM workspace_release_requests
                WHERE expected_worker_id=:worker AND storage_node_id=:node
                AND status='pending' AND id>:after ORDER BY id LIMIT 32"""),
                {'worker':self.service.worker_id,'node':self.service.node_id,
                 'after':self._repair_after})).mappings().all()
        self._repair_after=rows[-1]['id'] if rows else ''
        for row in rows:
            await self._release(row)

    async def _heartbeats(self,stop,draining):
        while not stop.is_set():
            try:
                await self.heartbeat(draining=draining.is_set())
            except (ConnectionError,TimeoutError,OSError,RedisError,DBAPIError,
                    CannotConnectNowError,ConnectionDoesNotExistError,ConnectionFailureError) as error:
                logger.warning('Worker hints deferred exception_type=%s',type(error).__name__)
            waiting=[asyncio.create_task(stop.wait())]
            if not draining.is_set():
                waiting.append(asyncio.create_task(draining.wait()))
            try:
                await asyncio.wait(waiting,timeout=self.ttl/3,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiting:
                    task.cancel()
                await asyncio.gather(*waiting,return_exceptions=True)

    async def _releases(self,stop):
        next_repair=0
        while not stop.is_set():
            try:
                now=time.monotonic()
                await self.release_tick()
                if now>=next_repair:
                    await self.repair_releases()
                    next_repair=now+self.ttl/3
            except (ConnectionError,TimeoutError,OSError,RedisError,DBAPIError,
                    CannotConnectNowError,ConnectionDoesNotExistError,ConnectionFailureError) as error:
                logger.warning('Workspace lifecycle transport deferred exception_type=%s',type(error).__name__)
            try:
                await asyncio.wait_for(stop.wait(),.2)
            except TimeoutError:
                pass

    async def serve(self,stop,draining):
        heartbeats=asyncio.create_task(self._heartbeats(stop,draining))
        releases=asyncio.create_task(self._releases(stop))
        stopped=asyncio.create_task(stop.wait())
        try:
            done,_=await asyncio.wait((heartbeats,releases,stopped),return_when=asyncio.FIRST_COMPLETED)
            for background in (heartbeats,releases):
                if background in done:
                    await background
                    if not stop.is_set():
                        raise RuntimeError('Workspace lifecycle loop exited unexpectedly')
        finally:
            for task in (heartbeats,releases,stopped):
                task.cancel()
            await asyncio.gather(heartbeats,releases,stopped,return_exceptions=True)
            try:
                await self.registry.withdraw_worker(WorkerRecord(incarnation=self.service.worker_id,
                    node_id=self.service.node_id,total_capacity=self.capacity,available_capacity=0,draining=True))
            except (ConnectionError,TimeoutError,OSError,RedisError) as error:
                logger.warning('Worker hint withdrawal deferred exception_type=%s',type(error).__name__)
