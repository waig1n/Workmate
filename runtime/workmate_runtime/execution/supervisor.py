"""Parked owner control supervision with optional durable permission suspension."""
import asyncio
import logging
from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from contextlib import suppress, nullcontext
from workmate_product.coordination import wait_for_wakeup

from workmate_runtime.execution.controller import ExecutionController
from workmate_runtime.execution.stopping import ParkedStopController
from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership, _lease_seconds


logger = logging.getLogger(__name__)


class ReconciliationRequired(RuntimeError):
    """Persisted control state cannot safely be driven as a healthy parked owner."""


# Process-local exclusion, including multiple supervisor instances. Worker IDs
# must be unique per process incarnation; this is not a distributed lock.
_registered: set[tuple[str, str]] = set()


class ParkedSupervisor:
    def __init__(self, pool, *, lease_seconds=30, execution_controller=None, wakeup_bus=None, suspend=None, command_source=None):
        self.command_source = command_source
        self.suspend = suspend
        self.wakeup_bus = wakeup_bus
        self.pool = pool
        self.lease_seconds = _lease_seconds(lease_seconds)
        self.interval = min(1.0,self.lease_seconds/3)
        self.execution_controller = execution_controller

    async def _inspect(self, owner, node_id):
        # A single MVCC statement gives a consistent read. Actual dispatch must
        # still pass the controllers' transactional claim and begin guards.
        async with self.pool.acquire() as db:
            row = await db.fetchrow('''SELECT s.status,s.state_version,s.snapshot_id,s.runtime_seq,
                e.status AS task_execution_status,b.status AS boundary_status,
                b.runtime_seq AS boundary_seq,
                (s.owner_id=$3 AND s.execution_epoch=$4 AND s.storage_node_id=$5
                    AND s.lease_until>clock_timestamp() AND s.active_task_id=$2
                    AND v.active_product_task_id=$2 AND t.conversation_id=v.id
                    AND t.status IN ('created','running','awaiting_permission','cancelling')
                    AND e.session_id=s.id AND e.execution_epoch=s.execution_epoch
                    AND e.reply_id=s.reply_id AND e.state_version=s.state_version
                    AND e.snapshot_id=s.snapshot_id AND b.id=s.snapshot_id
                    AND b.session_id=s.id AND b.task_id=$2 AND b.reply_id=s.reply_id
                    AND b.execution_epoch=s.execution_epoch AND b.state_version=s.state_version) AS valid
                FROM runtime_sessions s JOIN conversations v ON v.id=s.conversation_id
                JOIN tasks t ON t.id=$2 JOIN runtime_task_executions e ON e.task_id=t.id
                LEFT JOIN runtime_snapshots b ON b.id=s.snapshot_id WHERE s.superseded_at IS NULL AND s.id=$1''',
                owner.session_id,owner.task_id,owner.worker_id,owner.execution_epoch,node_id)
            if not row or not row['valid']:
                raise LostOwnership('Cannot supervise an expired, foreign or inconsistent owner')
            if (row['status']!='awaiting_interaction' or row['task_execution_status']!='awaiting_interaction'
                    or row['boundary_status']!='awaiting_interaction' or row['boundary_seq']!=row['runtime_seq']):
                raise ReconciliationRequired('Supervisor requires a complete parked execution boundary')
            unresolved = await db.fetchval("""SELECT id FROM runtime_commands WHERE task_id=$1
                AND kind IN ('ResumeInteraction','StopTask') AND status IN ('claimed','indeterminate') LIMIT 1""",owner.task_id)
            if unresolved:
                raise ReconciliationRequired(f'Control command {unresolved} needs reconciliation')
            return await db.fetchrow("""SELECT id,kind FROM runtime_commands WHERE task_id=$1
                AND session_id=$2 AND kind IN ('ResumeInteraction','StopTask') AND status='accepted'
                AND ($3::varchar[] IS NULL OR id=ANY($3::varchar[]))
                ORDER BY CASE WHEN kind='StopTask' THEN 0 ELSE 1 END,created_at,id LIMIT 1""",owner.task_id,owner.session_id,
                list(self.command_source.control_ids(owner.session_id)) if self.command_source is not None else None)

    async def _heartbeat(self, owner):
        while True:
            async with asyncio.timeout(self.lease_seconds/3):
                async with self.pool.acquire() as db:
                    try:
                        await ExecutionStore(db).renew(owner,lease_seconds=self.lease_seconds)
                    except LostOwnership:
                        # release commits before its caller receives the result.
                        # A concurrent renewal must not cancel that successful
                        # cleanup coroutine or quarantine a safely released Task.
                        if await db.fetchval("""SELECT 1 FROM runtime_suspensions
                            WHERE session_id=$1 AND task_id=$2 AND source_worker_id=$3
                                AND source_epoch=$4 AND status='released'""",
                                owner.session_id,owner.task_id,owner.worker_id,owner.execution_epoch):
                            return
                        raise
            await asyncio.sleep(self.interval)

    async def serve(self, owner, node_id, *, resume_slots, prepare_resume, prepare_stop,
                    authorize_resume, verify_cleanup):
        """Attach to a safe parked owner and drive accepted controls until terminal.

        The capacity semaphore is supplied by the Worker, shared with execution
        scheduling. Parked waiting and Stop never consume it. Trusted preparation,
        authorization and resource verification are the existing controller
        dependencies. No production role startup is installed here.

        Cancellation or any failure joins child tasks, retains DB gates/claims
        and never revives an expired lease. Running Stop remains unsupported:
        a Resume interrupted by cancellation intent goes to reconciliation.
        """
        key = (owner.worker_id,owner.session_id)
        if key in _registered:
            raise ReconciliationRequired('Session is already supervised in this process')
        _registered.add(key)
        heartbeat = control = None
        try:
            async with asyncio.timeout(self.lease_seconds/3):
                await self._inspect(owner,node_id)
            heartbeat = asyncio.create_task(self._heartbeat(owner))
            control = asyncio.create_task(self._controls(owner,node_id,resume_slots,prepare_resume,
                                                        prepare_stop,authorize_resume,verify_cleanup))
            done,_ = await asyncio.wait((heartbeat,control),return_when=asyncio.FIRST_COMPLETED)
            if control in done:
                return await control
            if heartbeat in done:
                await heartbeat
            return await control
        finally:
            children = [task for task in (heartbeat,control) if task is not None]
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children,return_exceptions=True)
            _registered.discard(key)

    async def _controls(self, owner, node_id, slots, prepare_resume, prepare_stop, authorize, verify):
        subscription = self.wakeup_bus.subscribe('commands') if self.wakeup_bus else nullcontext(None)
        with subscription as pulse:
            return await self._drive_controls(owner, node_id, slots, prepare_resume,
                prepare_stop, authorize, verify, pulse)

    async def _drive_controls(self, owner, node_id, slots, prepare_resume, prepare_stop, authorize, verify, pulse):
        resume = self.execution_controller or ExecutionController(self.pool,lease_seconds=self.lease_seconds)
        stop = ParkedStopController(self.pool,lease_seconds=self.lease_seconds,
            seal_terminal=getattr(self.execution_controller,'seal_terminal',None),
            realtime_writer_factory=getattr(self.execution_controller,'realtime_writer_factory',None))
        capacity = None
        try:
            while True:
                version = pulse.version if pulse else 0
                async with asyncio.timeout(self.lease_seconds/3):
                    candidate = await self._inspect(owner,node_id)
                generation = (self.command_source.control_generation(candidate["id"])
                    if candidate is not None and self.command_source is not None else None)
                if candidate is not None and self.command_source is not None and generation is None:
                    candidate = None  # Delivered hint changed while the DB read awaited.
                if candidate is None:
                    await self._release_capacity(capacity,slots)
                    capacity = None
                    if self.suspend is not None and await self.suspend(owner, node_id):
                        return None  # Durable released boundary, not a terminal Task.
                elif candidate['kind']=='StopTask':
                    await self._release_capacity(capacity,slots)
                    capacity = None
                    result = await stop.stop(candidate['id'],owner.worker_id,node_id,prepare_stop,verify_cleanup=verify)
                    if result is None:
                        await self._reject(candidate['id'],generation)
                        raise ReconciliationRequired('Stop could not be claimed; inspect durable state')
                    return result
                else:
                    if capacity is None:
                        capacity = asyncio.create_task(slots.acquire())
                        if pulse is not None:
                            capacity.add_done_callback(lambda _: pulse.signal())
                    if capacity.done():
                        capacity.result()
                        try:
                            result = await resume.resume(candidate['id'],owner.worker_id,node_id,prepare_resume,
                                                         authorize_resume=authorize)
                        finally:
                            slots.release()
                            capacity = None
                        if result is None:
                            await self._reject(candidate['id'],generation)
                            # A competing Stop may have rejected this candidate.
                            # Permit that convergence, never retry the same command.
                            async with asyncio.timeout(self.lease_seconds/3):
                                next_command = await self._inspect(owner,node_id)
                            if next_command is None or next_command['kind']!='StopTask':
                                raise ReconciliationRequired('Resume was not claimed; inspect durable state')
                        elif result.status!='awaiting_interaction':
                            return result
                await wait_for_wakeup(pulse, version, self.interval)
        finally:
            await self._release_capacity(capacity,slots)

    async def _reject(self, command_id, generation):
        if self.command_source is None:
            return
        try:
            await self.command_source.rejected(command_id,generation)
        except (ConnectionError,TimeoutError,OSError,RedisError,DBAPIError) as error:
            # Source stores a generation-specific retry intent before doing I/O.
            logger.warning('Parked control disposition deferred exception_type=%s',type(error).__name__)

    @staticmethod
    async def _release_capacity(capacity, slots):
        if capacity is None:
            return
        if not capacity.done():
            capacity.cancel()
        with suppress(asyncio.CancelledError):
            if await capacity:
                slots.release()
