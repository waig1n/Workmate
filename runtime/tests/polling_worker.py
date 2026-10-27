"""Explicit test-only PG discovery harness for existing controller regression."""
import asyncio
from contextlib import nullcontext
from workmate_product.coordination import FALLBACK_SECONDS, wait_for_wakeup
from workmate_runtime.execution.service import WorkerBindings, WorkerService as StreamsWorkerService


class WorkerService(StreamsWorkerService):
    async def _discover(self):
        # Candidate hints only. Each handler rechecks identities and gates under
        # ordered locks. Late terminal Stop only settles existing durable proof;
        # it never sweeps claimed work into the fresh Start path.
        async with self.pool.acquire() as db:
            return await db.fetch("""SELECT c.id,c.session_id,c.task_id,c.kind FROM runtime_commands c
                JOIN runtime_sessions s ON s.id=c.session_id
                JOIN conversations v ON v.id=c.conversation_id
                JOIN tasks t ON t.id=c.task_id
                WHERE v.deleted_at IS NULL AND c.status='accepted' AND c.protocol_version=1
                    AND s.storage_node_id=$1 AND v.active_product_task_id=c.task_id
                    AND c.kind='StartTask' AND s.active_task_id IS NULL AND t.status='created'
                    AND NOT(c.session_id=ANY($2::varchar[]))
                    AND NOT(c.id=ANY($3::varchar[]))
                ORDER BY c.created_at,c.id LIMIT $4""", self.node_id,
                list(self._active.keys() | self.failures.keys()),
                list(self._unclaimed_commands),self.available_capacity)

    async def _discover_terminal_stops(self):
        # Cancellation settlement is control work, independent of saturated
        # inference capacity. Bound the total concurrent settlements separately.
        async with self.pool.acquire() as db:
            return await db.fetch("""SELECT c.id,c.session_id,c.task_id,
                    CASE WHEN s.active_task_id IS NULL THEN 'unclaimed' ELSE 'terminal' END AS stop_phase
                FROM runtime_commands c
                JOIN runtime_sessions s ON s.id=c.session_id
                JOIN conversations v ON v.id=c.conversation_id
                JOIN tasks t ON t.id=c.task_id
                WHERE c.kind='StopTask' AND c.status='accepted' AND c.protocol_version=1
                    AND s.storage_node_id=$1 AND v.active_product_task_id=c.task_id
                    AND ((s.active_task_id=c.task_id AND s.status IN ('completed','failed','interrupted')
                        AND t.status IN ('cancelling','cancelled'))
                    OR (s.active_task_id IS NULL AND t.status='cancelling'
                        AND s.status IN ('accepted','completed','failed','cancelled','interrupted')))
                    AND NOT(c.session_id=ANY($2::varchar[]))
                    AND NOT(c.id=ANY($3::varchar[]))
                ORDER BY c.created_at,c.id LIMIT $4""",self.node_id,
                list(self._active.keys() | self.failures.keys()),
                list(self._unclaimed_commands),max(0,100-len(self._terminal_settlements)))

    async def _discover_suspended(self, *, stopping):
        # Hints only; the control store validates the complete immutable boundary
        # and claims a new epoch. Stop stays independent of inference capacity.
        async with self.pool.acquire() as db:
            return await db.fetch("""SELECT c.id,c.session_id,c.task_id,c.kind
                FROM runtime_commands c JOIN runtime_sessions s ON s.id=c.session_id
                JOIN runtime_suspensions p ON p.snapshot_id=s.snapshot_id
                WHERE c.status='accepted' AND c.protocol_version=1
                    AND c.task_id=s.active_task_id AND s.storage_node_id=$1
                    AND s.owner_id IS NULL AND s.lease_until IS NULL AND p.status='released'
                    AND c.kind=$2
                    AND NOT(c.session_id=ANY($3::varchar[]))
                    AND NOT(c.id=ANY($4::varchar[]))
                    AND ($2='StopTask' OR NOT EXISTS (SELECT 1 FROM runtime_commands stop
                        WHERE stop.task_id=c.task_id AND stop.kind='StopTask' AND stop.status='accepted'))
                ORDER BY c.created_at,c.id LIMIT $5""", self.node_id,
                'StopTask' if stopping else 'ResumeInteraction',
                list(self._active.keys() | self.failures.keys()),list(self._unclaimed_commands),
                max(0,100-len(self._terminal_settlements)) if stopping else self.available_capacity)

    async def serve(self, shutdown):
        subscription = self.wakeup_bus.subscribe('commands') if self.wakeup_bus else nullcontext(None)
        with subscription as pulse:
            await self._serve(shutdown, pulse)

    async def _serve(self, shutdown, pulse):
        if self._served:
            raise RuntimeError('WorkerService is single-use per process incarnation')
        self._served = True
        try:
            self.startup_observations = await self._until_shutdown(self._inspect(),shutdown)
            if self.startup_observations is None:
                return
            for observation in self.startup_observations:
                if observation.category.startswith('blocked_'):
                    self.failures[observation.session_id] = observation.category
            self.ready.set()
            while not shutdown.is_set():
                version = pulse.version if pulse else 0
                for sid, task in tuple(self._active.items()):
                    if task.done():
                        await task
                        del self._active[sid]
                if len(self._terminal_settlements)<100:
                    controls = await self._until_shutdown(self._discover_terminal_stops(),shutdown)
                    if controls is None:
                        break
                    for command in controls:
                        if shutdown.is_set():
                            break
                        sid=command['session_id']
                        if sid in self._active:
                            continue
                        self._terminal_settlements.add(sid)
                        self._active[sid]=asyncio.create_task(self._session(command['id'],sid,'StopTask',command['stop_phase'],task_id=command['task_id']))
                if getattr(self.bindings, 'suspend', None) is not None:
                    for stopping in (True, False):
                        candidates = await self._until_shutdown(self._discover_suspended(stopping=stopping),shutdown)
                        if candidates is None:
                            break
                        for command in candidates:
                            sid = command['session_id']
                            if shutdown.is_set() or sid in self._active:
                                continue
                            if stopping:
                                self._terminal_settlements.add(sid)
                            else:
                                if not self.available_capacity:
                                    break
                                await self._slots.acquire()
                            self._active[sid] = asyncio.create_task(self._session(command['id'],sid,
                                command['kind'],'suspended',task_id=command['task_id']))
                if self.available_capacity:
                    candidates = await self._until_shutdown(self._discover(),shutdown)
                    if candidates is None:
                        break
                    for command in candidates:
                        if shutdown.is_set():
                            break
                        sid = command['session_id']
                        if sid in self._active or not self.available_capacity:
                            continue
                        await self._slots.acquire()
                        self._active[sid] = asyncio.create_task(self._session(command['id'],sid,command['kind'],task_id=command['task_id']))
                await wait_for_wakeup(pulse, version,
                    FALLBACK_SECONDS if pulse else .1, shutdown)
        finally:
            self.ready.clear()
            tasks = tuple(self._active.values())
            if tasks:
                _, pending = await asyncio.wait(tasks,timeout=self.drain_seconds)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
            self._active.clear()



import pytest


@pytest.fixture
def polling_composition(monkeypatch):
    """Explicit controller regression harness; production always uses Streams."""
    monkeypatch.setattr("workmate_runtime.execution.composition.WorkerService", WorkerService)
