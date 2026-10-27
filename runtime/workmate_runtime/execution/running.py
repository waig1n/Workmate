"""Running Stop cancels only Core advancement, never the database consumer."""
import asyncio
import json
from contextlib import aclosing, nullcontext
from agentscope.tool import ToolResponse
from workmate_contracts.realtime import MessageSnapshotReference
from workmate_product.coordination import FALLBACK_SECONDS, wait_for_wakeup

from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.execution.controller import ExecutionController, ExecutionGuard, ExecutionAborted, ExecutionResult
from workmate_runtime.execution.stopping import _unfinished
from workmate_runtime.execution.realtime_writer import RealtimeTransportExhausted
from workmate_runtime.execution.tool_deltas import coalesce_tool_deltas
from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership
from workmate_runtime.transport.running import RunningStopStore
from workmate_runtime.transport.stopping import StopStore


STREAM_BATCH_EVENTS = 16
STREAM_BATCH_BYTES = 128 * 1024
STREAM_DELTAS = frozenset({'TEXT_BLOCK_DELTA', 'TOOL_CALL_DELTA', 'TOOL_RESULT_TEXT_DELTA'})
DISPLAY_BODY = frozenset(f'{kind}_BLOCK_{action}' for kind in ('TEXT', 'THINKING')
                        for action in ('START', 'DELTA', 'END'))
INTERACTION_REQUESTS = frozenset({'REQUIRE_USER_CONFIRM', 'REQUIRE_EXTERNAL_EXECUTION'})


def _stream_raw(guard, event):
    raw = event.model_dump(mode='json')
    if (event.reply_id != guard.reply_id
            or raw.get('session_id', guard.owner.session_id) != guard.owner.session_id):
        raise ExecutionAborted('Core event belongs to another Session/Reply')
    return raw


def _ready_batch(guard, first, queue):
    """Take only ready deltas; return an unconsumed lookahead without requeueing it."""
    raw = _stream_raw(guard, first)
    batch = [raw]
    size = len(json.dumps(raw, allow_nan=False).encode())
    if first.type not in STREAM_DELTAS:
        return batch, None
    while len(batch) < STREAM_BATCH_EVENTS and size < STREAM_BATCH_BYTES:
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if event.type not in STREAM_DELTAS:
            return batch, event
        raw = _stream_raw(guard, event)
        length = len(json.dumps(raw, allow_nan=False).encode())
        if size + length > STREAM_BATCH_BYTES:
            return batch, event
        batch.append(raw)
        size += length
    return batch, None


class RunningGuard(ExecutionGuard):
    def __init__(self, controller, owner, command_id, node_id):
        super().__init__(controller,owner,command_id)
        self.node_id = node_id
        self.stop_claim = None
        self.next_core = None
        self.cancel_injected = False
        self.realtime_failed = False

    async def on_acting(self, agent, input_kwargs, next_handler):
        task = asyncio.current_task()
        self.active_tools.add(task)
        try:
            await self.check()
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for chunk in stream:
                    if isinstance(chunk, ToolResponse):
                        # Core 2.0.7 breaks sequential execution on an interrupted
                        # result without resuming this yield. Drain the innermost
                        # tool iterator before exposing its terminal response;
                        # async-generator garbage collection is not cleanup proof.
                        try:
                            await anext(stream)
                        except StopAsyncIteration:
                            pass
                        else:
                            raise ExecutionAborted('Tool emitted data after its final response')
                        self.active_tools.discard(task)
                        yield chunk
                        return
                    yield chunk
        finally:
            self.active_tools.discard(task)

    def cancel_core(self):
        if not self.cancel_injected and self.next_core is not None and not self.next_core.done():
            self.cancel_injected = True
            # check() already raises CancelledError into Core. Cancelling that
            # same task as well leaves a pending cancellation that can discard
            # its first cleanup event after Core swallowed the explicit error.
            if self.next_core is not asyncio.current_task():
                self.next_core.cancel()

    async def claim_stop(self, db, *, require_delivery=True):
        if self.stop_claim is None and self.reply_id is not None:
            source = getattr(self.controller, "command_source", None)
            command_ids = (list(source.control_ids(self.owner.session_id))
                if source is not None and require_delivery else None)
            self.stop_claim = await RunningStopStore(db).claim_pending(
                self.owner,self.command_id,self.node_id,command_ids=command_ids)
            if self.stop_claim is not None:
                self.cancel_core()
        return self.stop_claim

    async def check(self):
        if self.failure is not None:
            raise ExecutionAborted('Execution authority was lost') from self.failure
        if self.realtime_failed:
            raise asyncio.CancelledError()
        if self.reply_id is None:
            raise ExecutionAborted('Reply must be durable before execution')
        try:
            async with self.write_lock:
                async with self.controller.pool.acquire() as db:
                    if await self.claim_stop(db):
                        raise asyncio.CancelledError()
                    try:
                        await ExecutionStore(db).check_running(self.owner,self.reply_id)
                    except LostOwnership:
                        # Product may have committed Stop between our two reads.
                        # Only a locked, fully validated Stop permits cleanup.
                        if await self.claim_stop(db,require_delivery=False):
                            raise asyncio.CancelledError()
                        raise
        except Exception as error:
            self.failure = error
            raise


class RunningExecutionController(ExecutionController):
    def __init__(self, pool, *, verify_cleanup, lease_seconds=30, poll_interval=.1, seal_terminal=None, wakeup_bus=None,
                 realtime_writer_factory=None, tool_content_factory=None):
        super().__init__(pool,lease_seconds=lease_seconds)
        if not callable(verify_cleanup):
            raise TypeError('Task-scoped cleanup verification is required')
        if isinstance(poll_interval,bool) or not isinstance(poll_interval,(int,float)) or not 0 < poll_interval <= 1:
            raise ValueError('poll_interval must be greater than zero and at most one second')
        if seal_terminal is not None and not callable(seal_terminal):
            raise TypeError("Terminal delivery sealing requires a trusted callback")
        if realtime_writer_factory is not None and not callable(realtime_writer_factory):
            raise TypeError('Realtime writer requires a trusted factory')
        self.tool_content_factory = tool_content_factory
        self.realtime_writer_factory = realtime_writer_factory
        self.wakeup_bus = wakeup_bus
        self.seal_terminal = seal_terminal
        self.verify_cleanup = verify_cleanup
        self.poll_interval = poll_interval

    def _new_guard(self, owner, command_id, storage_node_id):
        return RunningGuard(self,owner,command_id,storage_node_id)

    async def _watch_stop(self, guard):
        subscription = self.wakeup_bus.subscribe('commands') if self.wakeup_bus else nullcontext(None)
        with subscription as pulse:
            while guard.stop_claim is None:
                version = pulse.version if pulse else 0
                async with guard.write_lock:
                    async with self.pool.acquire() as db:
                        await guard.claim_stop(db)
                if guard.stop_claim is None:
                    await wait_for_wakeup(pulse, version,
                        FALLBACK_SECONDS if pulse else self.poll_interval)

    async def _produce(self, guard, engine, inputs, queue):
        async with aclosing(engine.reply_stream(inputs)) as stream:
            while True:
                # Advance and close the entire stream in one Task/Context:
                # acting middleware keeps ContextVar tokens across yields.
                # Expose this producer as cancellable only while inside Core,
                # never while waiting on the queue or the durable consumer.
                guard.next_core = asyncio.current_task()
                if guard.stop_claim is not None or guard.realtime_failed:
                    asyncio.get_running_loop().call_soon(guard.cancel_core)
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    return
                finally:
                    guard.next_core = None
                await queue.put(event)

    async def _persist_or_buffer(self, guard, events, buffered):
        events = coalesce_tool_deltas(events)
        async with guard.write_lock:
            if guard.failure is not None:
                raise ExecutionAborted('Authority lost during stream consumption') from guard.failure
            if guard.stop_claim is not None:
                buffered.extend(events)
                return
            async with self.pool.acquire() as db:
                # append_events already fences writes against cancelling Tasks.
                # Only resolve a new Stop when that boundary rejects the batch;
                # the independent watcher handles periods with no events.
                try:
                    await ExecutionStore(db).append_events(guard.owner,guard.reply_id,events=events)
                except LostOwnership:
                    if not await guard.claim_stop(db,require_delivery=False):
                        raise
                    buffered.extend(events)

    async def _drain(self, guard, engine, inputs, initial):
        queue = asyncio.Queue(maxsize=64)
        producer = asyncio.create_task(self._produce(guard,engine,inputs,queue))
        watcher = asyncio.create_task(self._watch_stop(guard))
        buffered = []
        end_reason = None
        receiver = None
        pending_event = None
        writer = timer = None
        tool_content = self.tool_content_factory() if self.tool_content_factory else None

        def check_timer():
            if timer is not None and timer.done():
                timer.result()
                raise ExecutionAborted('Realtime timer exited before its display boundary')

        async def checkpoint(phase):
            check_timer()
            cut = await writer.checkpoint(phase)
            check_timer()
            return (MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json')
                    if cut is not None else None)

        try:
            while True:
                batch = None
                try:
                    if not guard.realtime_failed:
                        check_timer()
                    if pending_event is not None:
                        event, pending_event = pending_event, None
                        if watcher.done():
                            await watcher
                    else:
                        if producer.done() and queue.empty():
                            await producer
                            break
                        receiver = asyncio.create_task(queue.get())
                        watched = [receiver,producer]
                        if timer is not None and not guard.realtime_failed:
                            watched.append(timer)
                        if not watcher.done() or watcher.exception() is not None:
                            watched.append(watcher)
                        done,_ = await asyncio.wait(watched,return_when=asyncio.FIRST_COMPLETED)
                        if not guard.realtime_failed:
                            check_timer()
                        if watcher in done:
                            await watcher
                        if receiver not in done:
                            receiver.cancel()
                            await asyncio.gather(receiver,return_exceptions=True)
                            receiver = None
                            if producer in done:
                                await producer
                            continue
                        event = receiver.result()
                        receiver = None
                    batch, pending_event = _ready_batch(guard, event, queue)
                    if writer is None and self.realtime_writer_factory is not None:
                        writer = await self.realtime_writer_factory(guard, initial)
                        timer = asyncio.create_task(writer.run_flush_timer())
                    if event.type == 'REPLY_END':
                        end_reason = event.finished_reason
                    if writer is not None and not guard.realtime_failed:
                        await writer.accept(batch)
                        check_timer()
                        for raw in batch:
                            if raw['type'] in INTERACTION_REQUESTS:
                                ref = await checkpoint('approval')
                                if ref is not None:
                                    raw['display_snapshot'] = ref
                        # Tool args/results still feed audit and RAG projections.
                        # Filtering here also protects the running Stop buffer.
                    if writer is not None:
                        batch = [raw for raw in batch if raw['type'] not in DISPLAY_BODY
                            and (not guard.realtime_failed or raw['type'] not in INTERACTION_REQUESTS)]
                    if tool_content is not None:
                        batch = await tool_content.transform(batch)
                    if event.type != 'REPLY_START' and batch:
                        await self._persist_or_buffer(guard,batch,buffered)
                    if event.type == 'REPLY_END':
                        end_reason = event.finished_reason
                except RealtimeTransportExhausted:
                    if guard.realtime_failed:
                        raise
                    # Do not poison execution authority: only model/tool entry is
                    # disabled while the same producer drains real Core cleanup.
                    guard.realtime_failed = True
                    if timer is not None:
                        timer.cancel()
                        await asyncio.gather(timer, return_exceptions=True)
                    guard.cancel_core()
                    if batch is not None:
                        durable = [raw for raw in batch if raw['type'] not in DISPLAY_BODY
                            and raw['type'] not in INTERACTION_REQUESTS and raw['type'] != 'REPLY_START']
                        if tool_content is not None:
                            durable = await tool_content.transform(durable)
                        if durable:
                            await self._persist_or_buffer(guard, durable, buffered)
                    elif receiver is not None:
                        if not receiver.done():
                            receiver.cancel()
                            await asyncio.gather(receiver, return_exceptions=True)
                        if not receiver.cancelled():
                            pending_event = receiver.result()
                        receiver = None
            if tool_content is not None:
                tail = await tool_content.flush()
                if tail:
                    await self._persist_or_buffer(guard,tail,buffered)
            if guard.failure is not None or guard.active_tools:
                raise ExecutionAborted('Core did not drain under live authority') from guard.failure
            if watcher.done():
                await watcher  # A concurrent polling failure must not become success.
            stopped = engine.snapshot()
            pending = tuple(c.id for c in stopped.to_state().get_awaiting_tool_calls(engine.agent.name))
            status = 'awaiting_interaction' if pending else {
                'completed':'completed','error':'failed','interrupted':'interrupted',
                'exceed_max_iters':'interrupted'}.get(end_reason)
            if guard.realtime_failed:
                if pending or end_reason is None or _unfinished(stopped.to_state(), engine.agent.name):
                    raise ExecutionAborted('Realtime failure did not reach a quiescent Core boundary')
                status = 'failed'
            if status is None:
                raise ExecutionAborted('Core did not provide a safe boundary')
            if status != 'awaiting_interaction':
                original = CoreSnapshot.model_validate(initial.snapshot) if initial.snapshot is not None else None
                if await self.verify_cleanup(guard.owner,original,stopped) is not True:
                    raise ExecutionAborted('Task resource cleanup has not been proven')
                if guard.failure is not None:
                    raise ExecutionAborted('Ownership lost during cleanup verification') from guard.failure
            evidence = stopped.boundary_evidence(engine.agent.name)
            boundary = {'type':'EXECUTION_BOUNDARY','reply_id':guard.reply_id,'status':status,
                'pending_tool_call_ids':list(pending),'boundary_evidence':evidence.model_dump(mode='json')}
            if guard.realtime_failed:
                boundary['execution_failure'] = {'code':'realtime_unavailable','finished_reason':end_reason}
            if writer is not None:
                if guard.realtime_failed:
                    cut = await writer.emergency_checkpoint()
                    ref = MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json') if cut is not None else None
                else:
                    try:
                        ref = await checkpoint('approval' if pending else
                            'final' if status == 'completed' else 'interrupted')
                    except RealtimeTransportExhausted:
                        if pending or _unfinished(stopped.to_state(), engine.agent.name):
                            raise ExecutionAborted('Realtime failure cannot retire parked tools')
                        guard.realtime_failed = True
                        timer.cancel()
                        await asyncio.gather(timer, return_exceptions=True)
                        status = boundary['status'] = 'failed'
                        boundary['execution_failure'] = {'code':'realtime_unavailable','finished_reason':end_reason}
                        cut = await writer.emergency_checkpoint()
                        ref = MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json') if cut is not None else None
                if ref is not None:
                    boundary['display_snapshot'] = ref
                else:
                    boundary['display_empty'] = True
                # After this cut, prevent a background save from racing the
                # immutable boundary. Check failures before cancelling timer.
                if not guard.realtime_failed:
                    check_timer()
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
                await writer.close()
            if status != 'awaiting_interaction':
                boundary['resource_cleanup'] = {'core_drained':True,'resources_quiescent':True}
                if self.seal_terminal is not None:
                    boundary['delivery_seal_sha256'] = await self.seal_terminal(
                        guard.owner,guard.reply_id,status,stopped.model_dump(mode='json'))
            async with guard.write_lock:
                if watcher.done():
                    await watcher
                async with self.pool.acquire() as db:
                    async with db.transaction():
                        # Match Product admission ordering and hold the same
                        # locks from the final Stop read through boundary save.
                        if await StopStore(db)._locked(guard.command_id) is None:
                            raise ExecutionAborted('Execution command binding disappeared')
                        await guard.claim_stop(db,require_delivery=False)
                        claim = guard.stop_claim
                        if claim is None:
                            version,seq = await self._save_normal(db,guard,initial,status,stopped,boundary)
                        else:
                            if pending or _unfinished(stopped.to_state(),engine.agent.name):
                                raise ExecutionAborted('Stop raced with parked work; reconciliation is required')
                            boundary['stop_cleanup'] = {'command_id':claim.command_id,
                                'execution_command_id':guard.command_id,'original_snapshot_id':claim.snapshot_id,
                                'core_drained':True,'resources_quiescent':True,'finished_reason':end_reason}
                            version,seq = await RunningStopStore(db).finish(claim,status=status,
                                snapshot=stopped.model_dump(mode='json'),events=buffered,boundary=boundary)
            return ExecutionResult(guard.owner,guard.reply_id,status,version,seq,pending)
        except Exception as error:
            # Preserve partial display evidence on producer/tool errors only
            # while the normal append fence still grants this execution access.
            if tool_content is not None and tool_content.buffers:
                try:
                    partial = await tool_content.flush()
                    if partial:
                        await self._persist_or_buffer(guard,partial,buffered)
                except Exception as flush_error:
                    error.add_note('Partial tool display flush failed: ' + type(flush_error).__name__)
            raise
        finally:
            if tool_content is not None:
                tool_content.close()
            children = [t for t in (timer,watcher,receiver,producer,guard.next_core) if t is not None]
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children,return_exceptions=True)
            if writer is not None:
                await writer.close()

    async def _save_normal(self, db, guard, initial, status, stopped, boundary):
        return await ExecutionStore(db).save_boundary(guard.owner,
                expected_state_version=initial.state_version,status=status,reply_id=guard.reply_id,
                snapshot=stopped.model_dump(mode='json'),events=[boundary],completed_command_id=guard.command_id)
