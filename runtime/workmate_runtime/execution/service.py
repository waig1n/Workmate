"""Durable Worker scheduling with explicit trusted application dependencies.

This module owns tasks and capacity, not Product admission or delivery release.
No App server or browser input can provide the assembly callbacks.
"""
import asyncio
from dataclasses import dataclass
import logging
import math
from typing import Callable
import uuid

from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from asyncpg import CannotConnectNowError, ConnectionDoesNotExistError, ConnectionFailureError

from workmate_runtime.execution.supervisor import ParkedSupervisor
from workmate_runtime.execution.terminal_stop import TerminalStopController
from workmate_runtime.execution.reconciliation import inspect_sessions
from workmate_runtime.transport.postgres import _lease_seconds

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerBindings:
    prepare_start: Callable
    prepare_resume: Callable
    prepare_stop: Callable
    authorize_resume: Callable
    verify_cleanup: Callable
    finalize_result: Callable | None = None
    report_failure: Callable | None = None
    suspend: Callable | None = None


class WorkerService:
    """One process incarnation. Database claims remain the execution authority.

    A failed session is quarantined locally; restarting scans persisted state,
    never retries a claimed operation. Terminal results retain delivery gates.
    Shutdown stops discovery, allows bounded drain, then cancels and joins owned
    coroutines. Cancellation alone never proves external resources quiescent.
    """

    def __init__(self, pool, storage_node_id, bindings, *, controller, capacity=4,
                 lease_seconds=30, drain_seconds=30, wakeup_bus=None, command_source=None):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        if not isinstance(storage_node_id, str) or not storage_node_id.strip():
            raise ValueError('storage_node_id is required')
        if (isinstance(drain_seconds, bool) or not isinstance(drain_seconds, (int, float))
                or not math.isfinite(drain_seconds) or drain_seconds < 0):
            raise ValueError('drain_seconds must be finite and nonnegative')
        self.command_source = command_source
        self.wakeup_bus = wakeup_bus
        self.pool, self.node_id, self.bindings = pool, storage_node_id, bindings
        self.controller = controller
        if command_source is not None and controller is not None:
            controller.command_source = command_source
        self.worker_id = 'worker-' + uuid.uuid4().hex
        self.lease_seconds = _lease_seconds(lease_seconds)
        self.drain_seconds = drain_seconds
        self._slots = asyncio.Semaphore(capacity)
        self._active = {}
        self.failures = {}
        self._unclaimed_commands = set()
        self._terminal_settlements = set()
        self.startup_observations = ()
        self._served = False
        self.ready = asyncio.Event()

    @property
    def available_capacity(self):
        return self._slots._value

    @property
    def active_sessions(self):
        return frozenset(self._active)

    async def _session(self, command_id, session_id, kind='StartTask', stop_phase='terminal', *, task_id=None, delivery_generation=None):
        reserved = kind in {'StartTask','ResumeInteraction'}  # Only Starts receive an inference slot.
        try:
            if kind=='StopTask' and stop_phase!='suspended':
                if stop_phase=='unclaimed':
                    settled=await self.controller.stop_unclaimed(command_id,self.worker_id,self.node_id)
                else:
                    settled=await TerminalStopController(self.pool).settle(command_id,self.worker_id,self.node_id)
                if not settled:
                    await self._remember_unclaimed(command_id,delivery_generation)
                return
            if kind == 'StopTask':
                from workmate_runtime.execution.stopping import ParkedStopController
                result = await ParkedStopController(self.pool,lease_seconds=self.lease_seconds,
                    seal_terminal=getattr(self.controller,'seal_terminal',None),
                    realtime_writer_factory=getattr(self.controller,'realtime_writer_factory',None)).stop(
                        command_id,self.worker_id,self.node_id,self.bindings.prepare_stop,
                        verify_cleanup=self.bindings.verify_cleanup)
            elif kind == 'ResumeInteraction':
                result = await self.controller.resume(command_id,self.worker_id,self.node_id,
                    self.bindings.prepare_resume,authorize_resume=self.bindings.authorize_resume)
            else:
                result = await self.controller.start(command_id,self.worker_id,self.node_id,
                    self.bindings.prepare_start)
            if reserved:
                self._slots.release()
                reserved = False
            if self.wakeup_bus is not None:
                self.wakeup_bus.signal_local("commands")
            if result is None:
                # Another process may have won or admission became invalid.
                # Do not continuously retry an unchanged rejected candidate.
                await self._remember_unclaimed(command_id,delivery_generation)
                return
            if result.status == 'awaiting_interaction':
                result = await ParkedSupervisor(self.pool,lease_seconds=self.lease_seconds,
                    execution_controller=self.controller,wakeup_bus=self.wakeup_bus,
                    suspend=self.bindings.suspend,command_source=self.command_source).serve(result.owner,self.node_id,
                    resume_slots=self._slots,prepare_resume=self.bindings.prepare_resume,
                    prepare_stop=self.bindings.prepare_stop,
                    authorize_resume=self.bindings.authorize_resume,
                    verify_cleanup=self.bindings.verify_cleanup)
            if result is not None and self.bindings.finalize_result is not None:
                await self.bindings.finalize_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Exception text/tracebacks can contain model input or credentials.
            # Log only durable identities and categories, never the exception.
            self.failures[session_id] = type(error).__name__
            cause = error.__cause__ if error.__cause__ is not None else error.__context__
            logger.error(
                'Worker session blocked session_id=%s task_id=%s command_id=%s kind=%s exception_type=%s cause_type=%s',
                session_id, task_id or '-', command_id, kind,
                type(error).__name__, type(cause).__name__ if cause is not None else '-',
            )
            if self.bindings.report_failure is not None:
                try:
                    await self.bindings.report_failure(
                        command_id=command_id, session_id=session_id, task_id=task_id,
                        worker_id=self.worker_id, storage_node_id=self.node_id,
                        error_type=type(error).__name__,
                        cause_type=type(cause).__name__ if cause is not None else None,
                    )
                except Exception as report_error:
                    # Reporting failure never unblocks the session or releases
                    # a durable gate, and its message may itself contain secrets.
                    logger.error(
                        'Worker failure report failed session_id=%s task_id=%s command_id=%s exception_type=%s',
                        session_id, task_id or '-', command_id, type(report_error).__name__,
                    )
        finally:
            self._terminal_settlements.discard(session_id)
            if reserved:
                self._slots.release()
            if self.wakeup_bus is not None:
                self.wakeup_bus.signal_local("commands")

    async def _remember_unclaimed(self, command_id, delivery_generation):
        if self.command_source is None:
            self._unclaimed_commands.add(command_id)
        else:
            try:
                await self.command_source.rejected(command_id,delivery_generation)
            except (ConnectionError, TimeoutError, OSError, RedisError, DBAPIError,
                    CannotConnectNowError, ConnectionDoesNotExistError, ConnectionFailureError) as error:
                # Source retains the isolation intent until durable retry succeeds.
                logger.warning("Command rejection disposition deferred exception_type=%s",type(error).__name__)

    async def serve(self, shutdown):
        if self.command_source is None:
            raise ValueError('Worker requires a command source')
        if self._served:
            raise RuntimeError('WorkerService is single-use per process incarnation')
        self._served = True
        try:
            self.startup_observations = await self._until_shutdown(self._inspect(), shutdown)
            if self.startup_observations is None:
                return
            for observation in self.startup_observations:
                if observation.category.startswith('blocked_'):
                    self.failures[observation.session_id] = observation.category
            self.ready.set()
            while not shutdown.is_set():
                for sid, task in tuple(self._active.items()):
                    if task.done():
                        await task
                        del self._active[sid]
                try:
                    await self._until_shutdown(self.command_source.settle(), shutdown)
                    if shutdown.is_set():
                        break
                    candidates = await self._until_shutdown(self.command_source.poll(
                        worker_id=self.worker_id, node_id=self.node_id,
                        capacity=self.available_capacity,
                        control_capacity=max(0, 100-len(self._terminal_settlements))), shutdown)
                    if candidates is None:
                        break
                    for command in candidates:
                        sid = command['session_id']
                        if command['id'] in self._unclaimed_commands:
                            continue
                        if sid in self._active or sid in self.failures:
                            if self.wakeup_bus is not None:
                                self.wakeup_bus.signal_local('commands')
                            continue
                        kind = command['kind']
                        if kind == 'StopTask':
                            if len(self._terminal_settlements) >= 100:
                                continue
                            self._terminal_settlements.add(sid)
                        else:
                            if not self.available_capacity:
                                continue
                            await self._slots.acquire()
                        self._active[sid] = asyncio.create_task(self._session(
                            command['id'], sid, kind, command.get('stop_phase', 'terminal'),
                            task_id=command['task_id'],delivery_generation=command['delivery_generation']))
                except (ConnectionError, TimeoutError, OSError, RedisError, DBAPIError,
                        CannotConnectNowError, ConnectionDoesNotExistError, ConnectionFailureError) as error:
                    # Transport failure never enables a second PG execution path.
                    # asyncpg connect failures may bypass SQLAlchemy's wrapper.
                    logger.warning('Worker command transport unavailable exception_type=%s', type(error).__name__)
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=.1)
                except TimeoutError:
                    pass
        finally:
            self.ready.clear()
            tasks = tuple(self._active.values())
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=self.drain_seconds)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self._active.clear()

    async def _inspect(self):
        async with self.pool.acquire() as db:
            return await inspect_sessions(db,worker_id=self.worker_id,storage_node_id=self.node_id)

    async def _until_shutdown(self, operation, shutdown):
        query = asyncio.create_task(operation)
        stopping = asyncio.create_task(shutdown.wait())
        try:
            done, _ = await asyncio.wait((query,stopping),timeout=self.lease_seconds/3,
                                        return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                return None
            if query not in done:
                raise TimeoutError('Worker discovery database deadline exceeded')
            return await query
        finally:
            for task in (query,stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(query,stopping,return_exceptions=True)
