"""Product-owned durable ingestion, delivery and queued-admission lifecycle.

No browser, AgentScope App, Workspace I/O or process-local completion callback
is needed for correctness. Independent loops revisit database facts after restart.
"""
import asyncio
from contextlib import nullcontext
from workmate_product.coordination import FALLBACK_SECONDS, stage_wakeup, wait_for_wakeup
import math
from sqlalchemy import text
from .store import consume_batch,ProjectionConflict,lock_product_conversation
from ..application.delivery import close_delivery
from ..application.commands import AcceptedCommand


_QUERIES={
    'approval_expiry': '''SELECT c.id FROM conversations c JOIN tasks t ON t.id=c.active_product_task_id
        WHERE t.status='awaiting_permission' ''',
    'projection':'''SELECT c.id FROM conversations c JOIN runtime_sessions s ON s.conversation_id=c.id AND s.superseded_at IS NULL
        LEFT JOIN runtime_projection_cursors p ON p.conversation_id=c.id
        WHERE s.runtime_seq>coalesce(p.last_runtime_seq,0)''',
    'delivery':'''SELECT c.id FROM conversations c JOIN tasks t ON t.id=c.active_product_task_id
        JOIN task_projection_states p ON p.task_id=t.id
        WHERE t.status IN ('completed','failed','cancelled','interrupted')
        AND NOT(p.state ? 'delivery_closed')
        AND ((p.state ? 'terminal_boundary_id' AND EXISTS
            (SELECT 1 FROM runtime_delivery_captures d WHERE d.task_id=t.id))
            OR p.state ? 'terminal_control_event_id')''',
    'admission':'''SELECT c.id FROM conversations c LEFT JOIN runtime_sessions s ON s.conversation_id=c.id AND s.superseded_at IS NULL
        WHERE c.active_product_task_id IS NULL AND s.active_task_id IS NULL
        AND EXISTS(SELECT 1 FROM tasks t WHERE t.conversation_id=c.id AND t.status='created'
            AND NOT EXISTS(SELECT 1 FROM runtime_commands r WHERE r.task_id=t.id AND r.kind='StartTask'))''',
}


class ProductProjectionService:
    """One role lifecycle; DB locks and durable receipts serialize all replicas.

    project and admit_next are trusted services, never browser/model callbacks.
    admit_next(db, conversation_id) must recheck current authority, queue order and
    both gates, persist the immutable ExecutionSpec and unique Start command in
    this transaction, and return AcceptedCommand or None. It must never dispatch,
    commit, read workspace files or perform network I/O. It is invoked in its own
    transaction without Product row locks, preserving Agent-first admission order.
    An absent callback leaves queued Tasks available for later admission scans.
    """
    def __init__(self,sessions,*,project,admit_next=None,expire_approval=None,poll_interval=.2,batch_size=100,wakeup_bus=None):
        if (not callable(project) or (admit_next is not None and not callable(admit_next))
                or (expire_approval is not None and not callable(expire_approval))):
            raise TypeError('Trusted Product project/admission services are required')
        if (isinstance(poll_interval,bool) or not isinstance(poll_interval,(int,float))
                or not math.isfinite(poll_interval) or poll_interval<=0):
            raise ValueError('poll_interval must be positive and finite')
        if type(batch_size) is not int or not 1<=batch_size<=1000:
            raise ValueError('batch_size must be between 1 and 1000')
        self.wakeup_bus=wakeup_bus
        self.sessions,self.project,self.admit_next=sessions,project,admit_next
        self.expire_approval=expire_approval
        self.poll_interval,self.batch_size=poll_interval,batch_size
        self.failures={}
        self._after={phase:'' for phase in _QUERIES}
        self._tasks=set()
        self._retryable=set()
        self._served=False

    @property
    def active_loops(self):
        return sum(not task.done() for task in self._tasks)

    async def _candidates(self,phase):
        # Keyset rotation prevents a poisoned or slow first page from starving
        # other Conversations. Candidates are hints, not ownership claims.
        async with self.sessions() as db:
            query=text(_QUERIES[phase]+' AND c.deleted_at IS NULL AND c.id>:after ORDER BY c.id LIMIT :limit')
            rows=(await db.execute(query,
                {'after':self._after[phase],'limit':self.batch_size})).scalars().all()
            if not rows and self._after[phase]:
                rows=(await db.execute(query,{'after':'','limit':self.batch_size})).scalars().all()
        self._after[phase]=rows[-1] if rows else ''
        return rows

    async def _step(self,phase,cid):
        async with self.sessions.begin() as db:
            if phase=='approval_expiry':
                return await self.expire_approval(db,cid)
            if phase=='admission':
                # No row lock before the application obtains Agent -> Conversation
                # locks. Advisory ownership excludes duplicate background calls.
                if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(173542923,hashtext(:cid))'),{'cid':cid}):
                    return False
                eligible=await db.scalar(text('SELECT id FROM ('+_QUERIES[phase]+') q WHERE id=:cid'),{'cid':cid})
                if not eligible:
                    return False
                accepted=await self.admit_next(db,cid)
                if accepted is None:
                    return False
                if not isinstance(accepted,AcceptedCommand):
                    raise ProjectionConflict('Admission must return its persisted command receipt')
                persisted=await db.scalar(text('''SELECT r.id FROM runtime_commands r
                    JOIN conversations c ON c.id=r.conversation_id
                    JOIN runtime_task_executions e ON e.task_id=r.task_id
                    JOIN runtime_execution_specs spec ON spec.task_id=r.task_id
                    WHERE r.id=:id AND r.task_id=:tid AND r.conversation_id=:cid
                    AND r.kind='StartTask' AND r.status='accepted' AND c.active_product_task_id=r.task_id
                    AND e.session_id=r.session_id AND spec.session_id=r.session_id'''),
                    {'id':accepted.command_id,'tid':accepted.product_task_id,'cid':cid})
                if not persisted:
                    raise ProjectionConflict('Admission did not persist a bound Start and ExecutionSpec')
                return True
            if phase=='projection':
                return await consume_batch(db,cid,project=self.project,skip_locked=True) is not None
            current=await lock_product_conversation(db,cid,skip_locked=True)
            if not current:
                return False
            if current['active_product_task_id'] is None:
                return False
            await close_delivery(db,cid,current['active_product_task_id'])
            stage_wakeup(db, 'admission')
            return True

    async def tick(self,phase):
        self._retryable.discard(phase)
        if phase not in _QUERIES:
            raise ValueError('Unknown Product background phase')
        if phase=='admission' and self.admit_next is None:
            return 0
        if phase=='approval_expiry' and self.expire_approval is None:
            return 0
        completed=0
        deferred=failed=False
        for cid in await self._candidates(phase):
            try:
                progressed=bool(await self._step(phase,cid))
                completed+=progressed
                deferred=deferred or not progressed
                self.failures.pop((phase,cid),None)
            except Exception as error:
                # Transactions have already rolled back. Keep only safe error
                # types; providers and malformed source payloads may hold secrets.
                self.failures[(phase,cid)]=type(error).__name__
                failed=True
        if deferred and not failed:
            self._retryable.add(phase)
        return completed

    async def _loop(self,phase,shutdown):
        key = 'runtime' if phase == 'projection' else phase
        subscription = self.wakeup_bus.subscribe(key) if self.wakeup_bus else nullcontext(None)
        with subscription as pulse:
            retries=3
            while not shutdown.is_set():
                version = pulse.version if pulse else 0
                try:
                    progress=await self.tick(phase)
                    self.failures.pop((phase,'scan'),None)
                except Exception as error:
                    self.failures[(phase,'scan')]=type(error).__name__
                    self._retryable.discard(phase)
                    progress=0
                if progress:
                    retries=3
                    await asyncio.sleep(0)
                else:
                    interval = 60 if phase=='approval_expiry' else (
                        FALLBACK_SECONDS if pulse else self.poll_interval)
                    # A notification can arrive while an SSE page holds a read
                    # lock. SKIP LOCKED then leaves known work pending; retry a
                    # bounded burst without needing another event. Persistent
                    # failures or unavailable admission still use idle backoff.
                    if pulse and phase!='approval_expiry' and phase in self._retryable and retries:
                        interval=min(.1,self.poll_interval)
                        retries-=1
                    elif phase not in self._retryable:
                        retries=3
                    await wait_for_wakeup(pulse,version,interval,shutdown)
                    if pulse and pulse.version!=version:
                        retries=3

    async def serve(self,shutdown):
        if self._served:
            raise RuntimeError('Product lifecycle can only be served once')
        self._served=True
        self._tasks={asyncio.create_task(self._loop(phase,shutdown),name='product-'+phase)
                     for phase in _QUERIES if (phase!='admission' or self.admit_next is not None)
                     and (phase!='approval_expiry' or self.expire_approval is not None)}
        try:
            await shutdown.wait()
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks,return_exceptions=True)
