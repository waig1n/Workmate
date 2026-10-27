"""Gateway-owned outbox delivery; Redis calls are outside database transactions."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import random
import time

from workmate_product.persistence.repositories import command_deliveries as repository
from .command_queue import CommandEnvelope, QueueRoute, RedisCommandQueue

logger = logging.getLogger(__name__)


class CommandPublisher:
    def __init__(self, sessions, queue, policy, *, release_maintenance=None, command_maintenance=None):
        self.sessions, self.queue, self.policy = sessions, queue, policy
        self._trim_cursor = 0
        if release_maintenance is None and isinstance(queue, RedisCommandQueue):
            from .worker_registry import RedisWorkerRegistry
            from .workspace_release_queue import RedisWorkspaceReleaseQueue
            from .workspace_release_maintenance import WorkspaceReleaseMaintenance
            release_maintenance = WorkspaceReleaseMaintenance(sessions,
                RedisWorkspaceReleaseQueue(queue.client, namespace=queue.namespace),
                RedisWorkerRegistry(queue.client, namespace=queue.namespace))
        self.release_maintenance = release_maintenance
        if command_maintenance is None and isinstance(queue, RedisCommandQueue):
            from .worker_registry import RedisWorkerRegistry
            from .command_queue_maintenance import CommandQueueMaintenance
            command_maintenance = CommandQueueMaintenance(sessions, queue,
                RedisWorkerRegistry(queue.client, namespace=queue.namespace), max_generations=policy.max_attempts)
        self.command_maintenance = command_maintenance

    async def publish_once(self):
        async with self.sessions.begin() as db:
            claims = await repository.lease_due(db, limit=self.policy.batch_size,
                                               lease_seconds=self.policy.lease_seconds)
        # All leased network operations start together under the bounded batch;
        # a sequential outage must not exhaust later claims before they start.
        async with asyncio.TaskGroup() as group:
            for claim in claims:
                group.create_task(self._publish_claim(claim))
        return len(claims)

    async def _publish_claim(self,claim):
        try:
            route = QueueRoute(claim.storage_node_id,claim.target_incarnation,claim.channel=='control')
            envelope = CommandEnvelope(command_id=claim.command_id,delivery_generation=claim.delivery_generation,
                storage_node_id=claim.storage_node_id,target_incarnation=claim.target_incarnation)
            async with asyncio.timeout(self.policy.lease_seconds/2):
                await self.queue.ensure_group(route)
                message_id = await self.queue.publish(route,envelope)
        except Exception as error:
            delay = random.uniform(.5,1)*min(self.policy.retry_max_seconds,2**min(claim.attempt_count,20))
            async with self.sessions.begin() as db:
                await repository.failed(db,claim,'invalid_delivery' if isinstance(error,ValueError) else 'queue_unavailable',
                        max_attempts=1 if isinstance(error,ValueError) else self.policy.max_attempts,
                    next_attempt_at=datetime.now(timezone.utc)+timedelta(seconds=delay))
            logger.warning('Command publication deferred exception_type=%s',type(error).__name__)
        else:
            async with self.sessions.begin() as db:
                await repository.published(db,claim,message_id)

    async def repair_once(self):
        from ..application.execution_quarantine import report_expired_executions
        await report_expired_executions(self.sessions, limit=self.policy.batch_size)
        if self.command_maintenance is not None:
            await self.command_maintenance.tick(count=self.policy.batch_size,
                retain_ms=self.policy.retention_seconds*1000)
        if self.release_maintenance is not None:
            await self.release_maintenance.tick(count=self.policy.batch_size,
                retain_ms=self.policy.retention_seconds*1000)
        # Retire acknowledged history even when every command is already settled.
        self._trim_cursor = await self.queue.trim_page(cursor=self._trim_cursor,
            count=self.policy.batch_size,retain_ms=self.policy.retention_seconds*1000)
        cutoff = datetime.now(timezone.utc)-timedelta(seconds=self.policy.redelivery_after_seconds)
        async with self.sessions.begin() as db:
            candidates = await repository.repair_candidates(db,older_than=cutoff,limit=self.policy.batch_size)
        for row in candidates:
            command_id,generation = row['command_id'],row['delivery_generation']
            if generation is None:
                async with self.sessions.begin() as db:
                    await repository.stage(db,command_id)
                continue
            if row['session_superseded_at'] is not None:
                async with self.sessions.begin() as db:
                    await repository.quarantine_superseded(db,command_id,generation)
                continue
            if row['command_status'] in {'completed','rejected','indeterminate'}:
                async with self.sessions.begin() as db:
                    await repository.settle(db,command_id,generation)
                continue
            if row['command_status'] == 'claimed':
                async with self.sessions.begin() as db:
                    await repository.quarantine_abandoned(db,command_id,generation)
            if row['command_status'] == 'accepted':
                route=QueueRoute(row['storage_node_id'],row['target_incarnation'],row['channel']=='control')
                exists = await self.queue.stream_has(route,row['stream_message_id'])
                if not exists or repository.repair_route_changed(row):
                    async with self.sessions.begin() as db:
                        await repository.reroute(db,command_id,expected_generation=generation,max_generations=self.policy.max_attempts)
            async with self.sessions.begin() as db:
                await repository.touch_repair(db,command_id,generation,
                    next_attempt_at=datetime.now(timezone.utc)+timedelta(seconds=self.policy.repair_interval_seconds))

    async def serve(self, shutdown):
        next_repair=0.0
        while not shutdown.is_set():
            cycle=asyncio.create_task(self.publish_once())
            stopping=asyncio.create_task(shutdown.wait())
            try:
                done,_=await asyncio.wait((cycle,stopping),return_when=asyncio.FIRST_COMPLETED)
                if stopping in done:
                    return
                await cycle
                if time.monotonic() >= next_repair:
                    cycle=asyncio.create_task(self.repair_once())
                    done,_=await asyncio.wait((cycle,stopping),return_when=asyncio.FIRST_COMPLETED)
                    if stopping in done:
                        return
                    await cycle
                    next_repair=time.monotonic()+self.policy.repair_interval_seconds
            except Exception as error:
                # Durable leases/intents permit retry. Never log connection URLs,
                # command bodies, Redis exception messages or model content.
                logger.warning('Command outbox cycle deferred exception_type=%s',type(error).__name__)
            finally:
                for task in (cycle,stopping):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(cycle,stopping,return_exceptions=True)
            try:
                await asyncio.wait_for(shutdown.wait(),self.policy.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
