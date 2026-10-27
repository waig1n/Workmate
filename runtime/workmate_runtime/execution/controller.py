"""Start and permission Resume controller with durable execution authority.

The trusted preparation callback must reload authorization, policy, attachments
and workspace baseline before constructing Core. Platform mutations still need
transactional fencing; the pre-call lease check cannot revoke an in-flight effect.
"""
import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

from agentscope.message import Msg
from agentscope.event import ConfirmResult, UserConfirmResultEvent, ReplyStartEvent
from workmate_contracts import snapshot_digest
from workmate_runtime.transport.controls import ResumeStore
from agentscope.middleware import MiddlewareBase

from workmate_runtime.engines.agentscope import CoreEngine, CoreSnapshot
from workmate_runtime.permission_policy import ProductPermissionMiddleware
from workmate_runtime.transport.postgres import ExecutionOwner, ExecutionStore, _lease_seconds


class ExecutionAborted(RuntimeError):
    """No safe boundary was committed; retain active ownership for reconciliation."""


class SandboxVerificationFailed(ExecutionAborted):
    """Known pre-Reply rejection; only proven cleanup permits a failed receipt."""


@dataclass(frozen=True)
class PreparedExecution:
    engine: CoreEngine
    inputs: Msg | list[Msg]


@dataclass(frozen=True)
class ExecutionResult:
    owner: ExecutionOwner
    reply_id: str | None
    status: str
    state_version: int
    runtime_seq: int
    pending_tool_call_ids: tuple[str, ...]


class ExecutionGuard(MiddlewareBase):
    """Innermost Core hooks: bind before reasoning, fence before model/tool I/O."""

    def __init__(self, controller, owner, command_id):
        self.controller = controller
        self.owner = owner
        self.command_id = command_id
        self.reply_id = None
        self.failure: BaseException | None = None
        self.write_lock = asyncio.Lock()
        self.active_tools: set[asyncio.Task] = set()
        self._display_lease_until = 0.0

    async def display_authority(self):
        """Cheap output preflight; snapshot transactions still fence every write.

        A live cleanup owner may save display state without gaining permission
        to call a model/tool. The deadline starts before the confirmed renewal,
        so pool/DB latency cannot extend the database lease locally.
        """
        return (self.failure is None and self.reply_id is not None
                and time.monotonic() < self._display_lease_until)

    async def call(self, method, *args, **kwargs):
        if self.failure is not None:
            raise ExecutionAborted('Execution authority was lost') from self.failure
        try:
            async with self.write_lock:
                if self.failure is not None:
                    raise ExecutionAborted('Execution authority was lost') from self.failure
                async with self.controller.pool.acquire() as db:
                    return await getattr(ExecutionStore(db), method)(self.owner, *args, **kwargs)
        except Exception as error:
            self.failure = error
            raise

    async def check(self):
        if self.reply_id is None:
            raise ExecutionAborted('Reply must be durable before execution')
        await self.call('check_running', self.reply_id)

    async def on_reply(self, agent, input_kwargs, next_handler):
        async with aclosing(next_handler(**input_kwargs)) as stream:
            async for event in stream:
                if getattr(event, 'type', None) == 'REPLY_START':
                    if self.reply_id is not None or event.session_id != self.owner.session_id:
                        raise ExecutionAborted('Unexpected Reply start')
                    await self.call('begin_reply', event.reply_id, event=event.model_dump(mode='json'))
                    self.reply_id = event.reply_id
                yield event

    async def on_model_call(self, agent, input_kwargs, next_handler):
        await self.check()
        return await next_handler(**input_kwargs)

    async def on_compress_context(self, agent, input_kwargs, next_handler):
        # Core compression calls model.generate_structured_output directly,
        # bypassing on_model_call (and may also write via its offloader).
        await self.check()
        return await next_handler(**input_kwargs)

    async def on_acting(self, agent, input_kwargs, next_handler):
        task = asyncio.current_task()
        self.active_tools.add(task)
        try:
            await self.check()
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            self.active_tools.discard(task)


Prepare = Callable[[ExecutionOwner, CoreSnapshot | None, ExecutionGuard], Awaitable[PreparedExecution]]


class ExecutionController:
    """Claim once, run outside transactions, stream durably, persist after drain.

    Preparation is a required trusted dependency,
    not a callback accepted from the browser or model. It must attach the guard
    last and attach current ProductPermissionMiddleware; no side-effecting or
    prefetching outer middleware is supported by this first controller.
    """

    def __init__(self, pool, *, lease_seconds: int = 30):
        self.pool = pool
        self.lease_seconds = _lease_seconds(lease_seconds)

    def _new_guard(self, owner, command_id, storage_node_id):
        return ExecutionGuard(self, owner, command_id)

    async def start(self, command_id: str, worker_id: str, storage_node_id: str,
                    prepare: Prepare) -> ExecutionResult | None:
        async with self.pool.acquire() as db:
            owner = await ExecutionStore(db).claim_start(command_id, worker_id, storage_node_id,
                                                         lease_seconds=self.lease_seconds)
        if owner is None:
            return None
        guard = self._new_guard(owner, command_id, storage_node_id)
        return await self._supervise(guard, self._execute(guard, prepare))

    async def resume(self, command_id, worker_id, storage_node_id, prepare, *, authorize_resume):
        """Resume a permission decision under a live owner or a proven cold claim.

        prepare(owner, exact_snapshot, guard) must construct a NEW CoreEngine
        with current policy and tools. It cannot supply replacement confirmation
        inputs. Cold ownership requires an exact durable release bridge; expiry alone grants nothing.
        """
        async with self.pool.acquire() as db:
            claim = await ResumeStore(db).claim_resume(command_id,worker_id,storage_node_id,
                                                      authorize_resume=authorize_resume,lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        guard = self._new_guard(claim.owner,command_id,storage_node_id)
        guard.reply_id = claim.reply_id
        return await self._supervise(guard,self._resume(guard,claim,storage_node_id,prepare,authorize_resume))

    async def _supervise(self, guard, coroutine):
        if getattr(self, 'realtime_writer_factory', None) is not None:
            try:
                async with asyncio.timeout(self.lease_seconds / 3):
                    await self._renew_display_lease(guard)
            except asyncio.CancelledError as error:
                guard.failure = error
                coroutine.close()
                raise
            except Exception as error:
                guard.failure = error
                coroutine.close()
                raise ExecutionAborted('Display lease renewal failed before execution') from error
        execution = asyncio.create_task(coroutine)
        heartbeat = asyncio.create_task(self._heartbeat(guard))
        try:
            done, _ = await asyncio.wait((execution, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat  # Raises; never treat a failed renewal as a normal stop.
            return await execution
        except asyncio.CancelledError as error:
            guard.failure = error
            raise
        except Exception as error:
            guard.failure = error
            raise ExecutionAborted('Execution stopped without releasing the Task gate') from error
        finally:
            heartbeat.cancel()
            if not execution.done():
                execution.cancel()
            # Core may consume cancellation and emit cleanup events. A lost guard
            # discards them; an old owner cannot save a fabricated terminal state.
            await asyncio.gather(heartbeat, execution, return_exceptions=True)
            # Cancellation during a DB await does not enter Core's own cancel
            # handler. Explicitly cancel/join any parallel acting tasks still
            # alive after the stream closes; aclose alone is not cleanup proof.
            remaining = tuple(guard.active_tools)
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)

    async def _heartbeat(self, guard):
        interval = self.lease_seconds / 3
        try:
            while True:
                await asyncio.sleep(interval)
                async with asyncio.timeout(interval):
                    await self._renew_display_lease(guard)
        except Exception as error:
            guard.failure = error
            raise

    async def _renew_display_lease(self, guard):
        started = time.monotonic()
        async with self.pool.acquire() as db:
            await ExecutionStore(db).renew(guard.owner, lease_seconds=self.lease_seconds)
        guard._display_lease_until = started + self.lease_seconds

    def _validate_engine(self, engine, guard):
        agent = engine.agent
        if agent.state.session_id != guard.owner.session_id:
            raise ExecutionAborted('Prepared state belongs to another Session')
        # These are version-pinned Core 2.0.7 hook lists, validated in real Core tests.
        for hooks in (agent._reply_middlewares, agent._model_call_middlewares,
                      agent._acting_middlewares, agent._compress_context_middlewares):
            if not hooks or hooks[-1] is not guard:
                raise ExecutionAborted('The execution guard must be the innermost Core middleware')
        policies = [mw for mw in agent._check_permission_middlewares if isinstance(mw, ProductPermissionMiddleware)]
        if not policies or any(mw._policy is None or mw._policy.task_id != guard.owner.task_id for mw in policies):
            raise ExecutionAborted('Current Task permission policy is required')

    def _validate_prepared(self, prepared, guard):
        self._validate_engine(prepared.engine,guard)
        agent = prepared.engine.agent
        if agent.state.has_awaiting_tool_calls(agent.name):
            raise ExecutionAborted('Start cannot resume a parked Reply')
        if not isinstance(prepared.inputs, Msg) and not (
                isinstance(prepared.inputs, list) and prepared.inputs and all(isinstance(msg, Msg) for msg in prepared.inputs)):
            raise ExecutionAborted('Start requires product-owned messages')

    async def _execute(self, guard, prepare):
        initial = await guard.call('load_boundary')
        if initial.status != 'preparing' or initial.reply_id is not None:
            raise ExecutionAborted('Only a newly claimed execution may start Core')
        snapshot = CoreSnapshot.model_validate(initial.snapshot) if initial.snapshot is not None else None
        prepared = await prepare(guard.owner, snapshot, guard)
        self._validate_prepared(prepared, guard)
        return await self._drain(guard,prepared.engine,prepared.inputs,initial)

    async def _resume(self, guard, claim, node_id, prepare, authorize):
        snapshot = CoreSnapshot.model_validate(claim.boundary.snapshot)
        state = snapshot.to_state()
        if state.session_id != guard.owner.session_id or state.reply_id != claim.reply_id:
            raise ExecutionAborted('Resume snapshot belongs to another Session/Reply')
        engine = await prepare(guard.owner,snapshot,guard)
        self._validate_engine(engine,guard)
        if snapshot_digest(engine.snapshot().model_dump(mode='json')) != snapshot_digest(claim.boundary.snapshot):
            raise ExecutionAborted('Preparation replaced the original complete resume state')
        calls = {call.id:call for call in state.get_awaiting_tool_calls(engine.agent.name) if call.state == 'asking'}
        if any(d.tool_call_id not in calls for d in claim.payload.decisions):
            raise ExecutionAborted('Decision does not refer to original pending permission calls')
        if any((calls[c['id']].name,calls[c['id']].input) != (c['name'],c['input']) for c in claim.original_calls):
            raise ExecutionAborted('Snapshot pending parameters differ from the original request')
        inputs = UserConfirmResultEvent(reply_id=claim.reply_id,confirm_results=[
            ConfirmResult(confirmed=d.approved,tool_call=calls[d.tool_call_id]) for d in claim.payload.decisions])
        async with self.pool.acquire() as db:
            await ResumeStore(db).begin_resume(claim,node_id,authorize_resume=authorize,
                event=ReplyStartEvent(session_id=guard.owner.session_id,reply_id=claim.reply_id,
                                      name=engine.agent.name).model_dump(mode='json'))
        await guard.check()
        return await self._drain(guard,engine,inputs,claim.boundary)

    async def _drain(self, guard, engine, inputs, initial):
        end_reason = None
        async with aclosing(engine.reply_stream(inputs)) as stream:
            async for event in stream:
                if guard.failure is not None:
                    continue  # Only drain local cancellation; never write under lost authority.
                if (getattr(event, 'session_id', guard.owner.session_id) != guard.owner.session_id
                        or event.reply_id != guard.reply_id):
                    raise ExecutionAborted('Core event belongs to an unexpected Reply')
                if event.type != 'REPLY_START':
                    await guard.call('append_events', guard.reply_id, events=[event.model_dump(mode='json')])
                if event.type == 'REPLY_END':
                    end_reason = event.finished_reason
        if guard.failure is not None:
            raise ExecutionAborted('Authority was lost while draining Core') from guard.failure
        if guard.active_tools:
            raise ExecutionAborted('Background tools remain active after the Reply stream')
        snapshot = engine.snapshot()
        state = snapshot.to_state()
        pending_calls = state.get_awaiting_tool_calls(engine.agent.name)
        pending = tuple(call.id for call in pending_calls)
        snapshot_payload = snapshot.model_dump(mode='json')
        evidence = snapshot.boundary_evidence(engine.agent.name)
        if pending:
            status = 'awaiting_interaction'
        else:
            status = {'completed': 'completed', 'error': 'failed', 'interrupted': 'interrupted',
                      'exceed_max_iters': 'interrupted'}.get(end_reason)
            if status is None:
                raise ExecutionAborted('Drained Core has neither pending interaction nor terminal evidence')
        version, seq = await guard.call('save_boundary', expected_state_version=initial.state_version,
            status=status, reply_id=guard.reply_id, snapshot=snapshot_payload,
            completed_command_id=guard.command_id,
            events=[{'type': 'EXECUTION_BOUNDARY', 'reply_id': guard.reply_id, 'status': status,
                     'pending_tool_call_ids': list(pending),
                     'boundary_evidence': evidence.model_dump(mode='json')}])
        return ExecutionResult(guard.owner, guard.reply_id, status, version, seq, pending)
