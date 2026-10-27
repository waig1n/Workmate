"""Injected realtime delivery on real Core and isolated PostgreSQL execution fences."""
import asyncio
import json

import pytest
from agentscope.message import TextBlock, ThinkingBlock, ToolCallBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.model import ChatResponse

from test_postgres_execution_store import database
from test_execution_controller import prepare_reply as prepare_final_reply
from test_core_engine import ConfirmTool
from test_parked_stop import quiescent
from test_running_stop import WaitingTool, stop_intent
from workmate_runtime.execution.controller import ExecutionAborted
from workmate_runtime.execution.controller import ExecutionController
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.realtime_writer import RealtimeUnavailable
from workmate_contracts.realtime import MessageSnapshotReference
from workmate_product.realtime.message_accumulator import MessageAccumulator


async def test_display_lease_is_local_conservative_and_does_not_grant_tool_authority(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from workmate_runtime.execution.stopping import StopGuard
    import workmate_runtime.execution.controller as controller_module

    now = [10.0]
    monkeypatch.setattr(controller_module.time, 'monotonic', lambda: now[0])

    class DB:
        async def fetchval(self, *args):
            now[0] = 15.0  # DB/pool latency cannot extend the local lease.
            return 'session'

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield DB()

    controller = ExecutionController(Pool(), lease_seconds=30)
    owner = SimpleNamespace(session_id='session', task_id='task', worker_id='worker', execution_epoch=1)
    guard = StopGuard(controller, owner, 'command')
    guard.reply_id = 'reply'
    assert not await guard.display_authority()
    await controller._renew_display_lease(guard)
    assert await guard.display_authority()
    with pytest.raises(ExecutionAborted):
        await guard.check()
    now[0] = 40.0
    assert not await guard.display_authority()
    now[0] = 16.0
    guard.failure = RuntimeError('heartbeat failed')
    assert not await guard.display_authority()


async def test_initial_display_renewal_failure_never_enters_core(database):
    pool, db = database
    entered = False

    class FailedRenewal(RunningExecutionController):
        async def _renew_display_lease(self, guard):
            raise RuntimeError('renewal failed')

    async def prepare(*args):
        nonlocal entered
        entered = True
        raise AssertionError('Must not enter preparation')

    with pytest.raises(ExecutionAborted, match='Display lease renewal failed'):
        await FailedRenewal(pool, verify_cleanup=quiescent,
            realtime_writer_factory=lambda *args: None).start('command', 'worker', 'node', prepare)
    assert not entered
    assert await db.fetchval('SELECT count(*) FROM runtime_events') == 0

BODY_TYPES = {f'{kind}_BLOCK_{action}' for kind in ('TEXT', 'THINKING')
              for action in ('START', 'DELTA', 'END')}


def prepare_reply(*args, **kwargs):
    prepare, calls = prepare_final_reply(*args, **kwargs)
    async def prepare_streaming(owner, snapshot, guard):
        prepared = await prepare(owner, snapshot, guard)
        original = prepared.engine.agent.model._call_api
        async def streaming(*args, **kwargs):
            stream = await original(*args, **kwargs)
            async def chunks():
                async for chunk in stream:
                    # Core 2.0.7 stores the final response but only emits body
                    # events from nonfinal chunks; exercise that real path.
                    if chunk.is_last:
                        yield ChatResponse(content=chunk.content, is_last=False)
                    yield chunk
            return chunks()
        prepared.engine.agent.model._call_api = streaming
        return prepared
    return prepare_streaming, calls


async def persisted_events(db):
    return [json.loads(row['payload']) for row in await db.fetch(
        'SELECT payload FROM runtime_events ORDER BY runtime_seq')]


class RecordingWriter:
    """Only replace delivery I/O; use actual typed body state and snapshot refs."""
    def __init__(self, db, *, approval_kind=None, hold_body=False, fail_timer=None, fail_cut=False):
        self.db = db
        self.approval_kind = approval_kind
        self.hold_body = hold_body
        self.fail_timer = fail_timer
        self.fail_cut = fail_cut
        self.accepted = []
        self.cuts = []
        self.phases = []
        self.closed = False
        self.timer_stopped = asyncio.Event()
        self.body_entered = asyncio.Event()
        self.release_body = asyncio.Event()
        self.guard = None
        self.accumulator = None

    async def factory(self, guard, initial):
        self.guard = guard
        self.accumulator = MessageAccumulator('conversation', guard.owner.task_id,
            guard.reply_id, guard.owner.execution_epoch)
        return self

    async def accept(self, events):
        assert self.guard.write_lock.owner is not asyncio.current_task(), 'Redis delivery must not hold the execution write lock'
        for event in events:
            self.accepted.append(event)
            self.accumulator.apply(event)
            if event['type'] == 'TEXT_BLOCK_DELTA':
                self.body_entered.set()
                if self.hold_body:
                    await self.release_body.wait()

    async def checkpoint(self, phase, incomplete=False):
        assert self.guard.write_lock.owner is not asyncio.current_task(), 'Display cut must run outside the execution write lock'
        self.phases.append(phase)
        if phase == 'approval' and self.approval_kind and len(self.phases) == 1:
            assert await self.db.fetchval(
                "SELECT count(*) FROM runtime_events WHERE payload->>'type'=$1", self.approval_kind) == 0
        if self.fail_cut:
            raise RealtimeUnavailable('display snapshot could not commit')
        if not any(raw['type'] in BODY_TYPES for raw in self.accepted):
            return None
        snapshot = self.accumulator.capture(phase, None, 0, incomplete=incomplete, unanchored=True)
        self.cuts.append(snapshot)
        return snapshot

    async def run_flush_timer(self):
        try:
            if self.fail_timer:
                await self.fail_timer.wait()
                raise RealtimeUnavailable('timer snapshot failed')
            await asyncio.Event().wait()
        finally:
            self.timer_stopped.set()

    async def close(self):
        self.closed = True


class TrackingLock(asyncio.Lock):
    def __init__(self):
        super().__init__()
        self.owner = None

    async def acquire(self):
        result = await super().acquire()
        self.owner = asyncio.current_task()
        return result

    def release(self):
        self.owner = None
        super().release()


class ObservedController(RunningExecutionController):
    def _new_guard(self, owner, command_id, storage_node_id):
        guard = super()._new_guard(owner, command_id, storage_node_id)
        guard.write_lock = TrackingLock()
        return guard


def controller(pool, writer):
    return ObservedController(pool, verify_cleanup=quiescent, poll_interval=.01,
                                      realtime_writer_factory=writer.factory)


def reference(snapshot):
    return MessageSnapshotReference.from_snapshot(snapshot).model_dump(mode='json')


@pytest.mark.parametrize("reasoning", [False, True])
async def test_body_uses_writer_and_terminal_carries_exact_display_reference(database, reasoning):
    pool, db = database
    writer = RecordingWriter(db)
    blocks = [TextBlock(text='你好 streamed body')]
    if reasoning:
        blocks.insert(0, ThinkingBlock(thinking='plan first'))
    prepare, _ = prepare_reply([blocks])
    result = await controller(pool, writer).start('command', 'worker', 'node', prepare)
    assert result.status == 'completed'
    assert {'TEXT_BLOCK_START', 'TEXT_BLOCK_DELTA', 'TEXT_BLOCK_END'} <= {e['type'] for e in writer.accepted}
    assert ''.join(b.text for b in writer.cuts[-1].blocks if b.kind == 'text') == '你好 streamed body'
    if reasoning:
        assert {f'THINKING_BLOCK_{action}' for action in ('START', 'DELTA', 'END')} <= {e['type'] for e in writer.accepted}
        assert ''.join(b.text for b in writer.cuts[-1].blocks if b.kind == 'reasoning') == 'plan first'
    assert writer.cuts[-1].phase == 'final' and not writer.cuts[-1].incomplete
    events = await persisted_events(db)
    assert not BODY_TYPES.intersection(e['type'] for e in events)
    assert events[-1]['type'] == 'EXECUTION_BOUNDARY'
    assert events[-1]['display_snapshot'] == reference(writer.cuts[-1])
    assert any(e['type'] == 'REPLY_END' for e in events)
    assert writer.closed and writer.timer_stopped.is_set()


class ExternalTool(ConfirmTool):
    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='test external execution')


@pytest.mark.parametrize('external', [False, True])
async def test_approval_display_cut_precedes_confirmation_event(database, external):
    pool, db = database
    tool = ExternalTool() if external else ConfirmTool()
    tool.is_external_tool = external
    kind = 'REQUIRE_EXTERNAL_EXECUTION' if external else 'REQUIRE_USER_CONFIRM'
    writer = RecordingWriter(db, approval_kind=kind)
    prepare, _ = prepare_reply([[TextBlock(text='review this'),
        ToolCallBlock(id='call', name=tool.name, input='{}')]], tool=tool)
    result = await controller(pool, writer).start('command', 'worker', 'node', prepare)
    assert result.status == 'awaiting_interaction' and tool.calls == 0
    assert writer.phases and set(writer.phases) == {'approval'}
    events = await persisted_events(db)
    request = next(event for event in events if event['type'] == kind)
    assert request['display_snapshot'] == reference(writer.cuts[0])
    assert events[-1]['display_snapshot'] == reference(writer.cuts[-1])
    assert writer.cuts[0].phase == 'approval'
    assert not BODY_TYPES.intersection(e['type'] for e in events)
    assert writer.closed and writer.timer_stopped.is_set()


async def test_uncommitted_approval_cut_cannot_publish_confirmation(database):
    pool, db = database
    tool = ConfirmTool()
    writer = RecordingWriter(db, fail_cut=True)
    prepare, _ = prepare_reply([[TextBlock(text='review this'),
        ToolCallBlock(id='call', name=tool.name, input='{}')]], tool=tool)
    with pytest.raises(ExecutionAborted):
        await controller(pool, writer).start('command', 'worker', 'node', prepare)
    events = await persisted_events(db)
    assert not {'REQUIRE_USER_CONFIRM', 'EXECUTION_BOUNDARY'}.intersection(e['type'] for e in events)
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert tool.calls == 0 and writer.closed


async def test_stop_racing_body_delivery_never_buffers_body_back_into_pg(database):
    pool, db = database
    tool = WaitingTool()
    writer = RecordingWriter(db, hold_body=True)
    prepare, _ = prepare_reply([[TextBlock(text='retained before stop'),
        ToolCallBlock(id='call', name=tool.name, input='{}')]], tool=tool)
    run = asyncio.create_task(controller(pool, writer).start('command', 'worker', 'node', prepare))
    try:
        await asyncio.wait_for(writer.body_entered.wait(), 3)
        await stop_intent(db)
        async with asyncio.timeout(3):
            while writer.guard.stop_claim is None:
                await asyncio.sleep(.01)
        writer.release_body.set()
        result = await asyncio.wait_for(run, 5)
        assert result.status == 'interrupted'
        events = await persisted_events(db)
        assert not BODY_TYPES.intersection(e['type'] for e in events)
        assert {'TOOL_CALL_START', 'TOOL_CALL_DELTA', 'TOOL_RESULT_TEXT_DELTA', 'TOOL_RESULT_END'} <= {e['type'] for e in events}
        assert writer.cuts[-1].phase == 'interrupted'
        assert 'retained before stop' in ''.join(block.text for block in writer.cuts[-1].blocks)
        assert events[-1]['display_snapshot'] == reference(writer.cuts[-1])
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 2
        assert writer.closed and writer.timer_stopped.is_set()
    finally:
        writer.release_body.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


async def test_timer_failure_interrupts_idle_core_without_fabricating_terminal(database):
    pool, db = database
    entered = asyncio.Event()
    writer = RecordingWriter(db, fail_timer=entered)
    prepare, _ = prepare_reply([], entered=entered)
    with pytest.raises(ExecutionAborted):
        await asyncio.wait_for(controller(pool, writer).start('command', 'worker', 'node', prepare), 5)
    assert not writer.cuts
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 0
    assert not any(e['type'] == 'EXECUTION_BOUNDARY' for e in await persisted_events(db))
    assert writer.closed and writer.timer_stopped.is_set()


@pytest.mark.parametrize("approval", [False, True])
async def test_no_body_does_not_fabricate_snapshot_reference(database, approval):
    pool, db = database
    tool = ConfirmTool()
    writer = RecordingWriter(db, approval_kind='REQUIRE_USER_CONFIRM' if approval else None)
    blocks = [ToolCallBlock(id='call', name=tool.name, input='{}')] if approval else []
    prepare, _ = prepare_reply([blocks], tool=tool if approval else None)
    result = await controller(pool, writer).start('command', 'worker', 'node', prepare)
    assert result.status == ('awaiting_interaction' if approval else 'completed')
    assert not writer.cuts
    assert all('display_snapshot' not in event for event in await persisted_events(db))
    assert writer.closed and writer.timer_stopped.is_set()

async def test_transport_exhaustion_drains_real_core_and_commits_failed_boundary(database):
    from workmate_runtime.execution.realtime_writer import RealtimeTransportExhausted
    pool, db = database
    entered = asyncio.Event()
    class ExhaustedWriter(RecordingWriter):
        async def run_flush_timer(self):
            try:
                await entered.wait()
                raise RealtimeTransportExhausted('transport exhausted')
            finally:
                self.timer_stopped.set()
        async def emergency_checkpoint(self):
            return None
    writer = ExhaustedWriter(db)
    prepare, _ = prepare_reply([], entered=entered)
    result = await asyncio.wait_for(controller(pool, writer).start('command','worker','node',prepare),5)
    assert result.status == 'failed'
    events = await persisted_events(db)
    ended = next(e for e in events if e['type']=='REPLY_END')
    boundary = events[-1]
    assert ended['finished_reason']=='interrupted'
    assert boundary['status']=='failed'
    assert boundary['execution_failure']=={'code':'realtime_unavailable','finished_reason':'interrupted'}
    assert boundary['display_empty'] is True
    assert boundary['resource_cleanup']=={'core_drained':True,'resources_quiescent':True}
    assert 'stop_cleanup' not in boundary
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'completed'

async def test_failed_accept_retains_tool_result_and_drains_inflight_model(database):
    from workmate_runtime.execution.realtime_writer import RealtimeTransportExhausted
    pool, db = database
    class ResultFailure(RecordingWriter):
        async def accept(self, events):
            await super().accept(events)
            if any(event['type']=='TOOL_RESULT_END' for event in events):
                raise RealtimeTransportExhausted('buffer exhausted')
        async def emergency_checkpoint(self):
            return self.accumulator.capture('interrupted',None,0,incomplete=True,unanchored=True)
    writer=ResultFailure(db)
    tool=WaitingTool(success_first=True)
    original_prepare,_=prepare_reply([[TextBlock(text='before tool'),ToolCallBlock(id='call',name=tool.name,input='{}')]],tool=tool)
    async def prepare(*args):
        prepared=await original_prepare(*args)
        original=prepared.engine.agent.model._call_api
        calls=0
        async def api(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls>1:
                await asyncio.Event().wait()
            return await original(*args,**kwargs)
        prepared.engine.agent.model._call_api=api
        return prepared
    result=await asyncio.wait_for(controller(pool,writer).start('command','worker','node',prepare),5)
    assert result.status=='failed'
    events=await persisted_events(db)
    results=[e for e in events if e['type']=='TOOL_RESULT_END']
    assert len(results)==1 and results[0]['state']=='success'
    assert any(e['type']=='MODEL_CALL_END' for e in events)
    assert tool.calls==1 and tool.exited==1
    assert events[-1]['execution_failure']['code']=='realtime_unavailable'

@pytest.mark.parametrize('early_stop',[True,False])
async def test_transport_failure_keeps_stop_intent_and_failed_seal_consistent(database,early_stop):
    from workmate_runtime.execution.realtime_writer import RealtimeTransportExhausted
    pool,db=database
    class FailedResult(RecordingWriter):
        async def accept(self,events):
            await super().accept(events)
            if any(e['type']=='TOOL_RESULT_END' for e in events):
                raise RealtimeTransportExhausted('buffer exhausted')
        async def emergency_checkpoint(self):
            return self.accumulator.capture('interrupted',None,0,incomplete=True,unanchored=True)
    writer=FailedResult(db)
    tool=WaitingTool(success_first=not early_stop)
    original,_=prepare_reply([[TextBlock(text='prefix'),ToolCallBlock(id='call',name=tool.name,input='{}')]],tool=tool)
    async def prepare(*args):
        prepared=await original(*args)
        api=prepared.engine.agent.model._call_api
        count=0
        async def blocked(*args,**kwargs):
            nonlocal count
            count+=1
            if count>1:
                await asyncio.Event().wait()
            return await api(*args,**kwargs)
        prepared.engine.agent.model._call_api=blocked
        return prepared
    seals=[]
    async def cleanup(*args):
        if not early_stop:
            await stop_intent(db)
        return await quiescent(*args)
    async def seal(owner,reply,status,snapshot):
        seals.append(status)
        return 'a'*64
    instance=ObservedController(pool,verify_cleanup=cleanup,seal_terminal=seal,
                                realtime_writer_factory=writer.factory,poll_interval=.01)
    run=asyncio.create_task(instance.start('command','worker','node',prepare))
    try:
        if early_stop:
            await asyncio.wait_for(tool.entered.wait(),3)
            await stop_intent(db)
        result=await asyncio.wait_for(run,5)
        assert result.status=='failed' and seals==['failed']
        events=await persisted_events(db)
        boundary=events[-1]
        assert boundary['status']=='failed' and boundary['stop_cleanup']
        assert boundary['execution_failure']['finished_reason']=='interrupted'
        assert sum(e['type']=='REPLY_END' for e in events)==1
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'")==2
        assert tool.calls==1 and tool.exited==1
    finally:
        if not run.done(): run.cancel()
        await asyncio.gather(run,return_exceptions=True)

@pytest.mark.parametrize('invalid',[
    {'execution_failure':None},
    {'execution_failure':{'code':'other','finished_reason':'interrupted'}},
    {'execution_failure':{'code':'realtime_unavailable','finished_reason':'error'}},
    {'resource_cleanup':{'core_drained':1,'resources_quiescent':True}},
    {'resource_cleanup':{'core_drained':True,'resources_quiescent':1.0}},
    {'display_empty':1},
    {'display_snapshot':None},
    {'display_snapshot':{},'display_empty':False},
])
def test_running_stop_refuses_malformed_realtime_failure_evidence(invalid):
    from workmate_runtime.transport.running import _matches_terminal_status
    boundary={'execution_failure':{'code':'realtime_unavailable','finished_reason':'interrupted'},
        'resource_cleanup':{'core_drained':True,'resources_quiescent':True},'display_empty':True}
    assert _matches_terminal_status({'finished_reason':'interrupted'},'failed',boundary)
    assert not _matches_terminal_status({'finished_reason':'interrupted'},'failed',{**boundary,**invalid})
