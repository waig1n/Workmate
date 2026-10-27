"""Running interruption drains real Core and completes both commands atomically."""
import asyncio
import json

import pytest

from test_postgres_execution_store import database  # noqa: F401
from test_execution_controller import prepare_reply
from test_parked_stop import stop_intent as seed_stop_intent, quiescent
from workmate_runtime.execution.controller import ExecutionAborted
from workmate_runtime.execution.running import RunningExecutionController
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk
from test_core_engine import ConfirmTool


async def stop_intent(db):
    # Product acceptance publishes cancelling + command in the same transaction.
    async with db.transaction():
        await seed_stop_intent(db)


class WaitingTool(ConfirmTool):
    is_concurrency_safe = True

    def __init__(self, count=1, *, success_first=False):
        super().__init__()
        self.count = count
        self.success_first = success_first
        self.entered = asyncio.Event()
        self.exited = 0

    async def check_permissions(self, tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW,message='test')

    async def __call__(self, **kwargs):
        self.calls += 1
        index = self.calls
        try:
            if self.success_first and index == 1:
                yield ToolChunk(content=[TextBlock(text='already committed')],is_last=True)
                return
            if self.calls == self.count:
                self.entered.set()
            yield ToolChunk(content=[TextBlock(text='waiting')],is_last=False)
            await asyncio.Event().wait()
        finally:
            self.exited += 1


async def test_natural_completion_also_requires_resource_quiescence(database):
    pool, db = database
    prepare, _ = prepare_reply([[TextBlock(text='model finished')]])
    async def not_quiescent(*args):
        return False
    with pytest.raises(ExecutionAborted):
        await RunningExecutionController(pool,verify_cleanup=not_quiescent).start(
            'command','worker','node',prepare)
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'claimed'


async def test_parking_does_not_seal_resources_needed_for_resume(database):
    pool, db = database
    tool = ConfirmTool()
    prepare, _ = prepare_reply([[ToolCallBlock(id='a',name=tool.name,input='{}')]],tool=tool)
    async def never(*args):
        raise AssertionError('Parking must not seal the resource scope')
    result = await RunningExecutionController(pool,verify_cleanup=never).start('command','worker','node',prepare)
    assert result.status == 'awaiting_interaction'


async def test_poll_failure_during_terminal_resource_probe_cannot_commit(database):
    pool, db = database
    probing, failed = asyncio.Event(), asyncio.Event()
    class PollFailure(RunningExecutionController):
        async def _watch_stop(self, guard):
            await probing.wait()
            failed.set()
            raise RuntimeError('poll unavailable')
    async def verify(*args):
        probing.set()
        await failed.wait()
        return True
    prepare, _ = prepare_reply([[TextBlock(text='done')]])
    with pytest.raises(ExecutionAborted):
        await PollFailure(pool, verify_cleanup=verify).start('command','worker','node',prepare)
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0


async def test_running_model_stop_drains_and_completes_both_commands(database):
    pool, db = database
    entered = asyncio.Event()
    prepare, _ = prepare_reply([], entered=entered)
    run = asyncio.create_task(RunningExecutionController(pool, verify_cleanup=quiescent,
        poll_interval=.01).start('command', 'worker', 'node', prepare))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await stop_intent(db)
        result = await asyncio.wait_for(run, 5)
        assert result.status == 'interrupted'
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 2
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'
        assert await db.fetchval('SELECT active_product_task_id FROM conversations') == 'task'
        events = [json.loads(r['payload']) for r in await db.fetch('SELECT payload FROM runtime_events ORDER BY runtime_seq')]
        assert sum(e['type'] == 'REPLY_END' for e in events) == 1
        assert events[-1]['stop_cleanup']['resources_quiescent'] is True
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.parametrize('failure', ['unproven', 'commit', 'lease'])
async def test_running_stop_failure_retains_claims_and_has_no_terminal(database, failure):
    pool, db = database
    entered = asyncio.Event()
    async def verify(*args):
        if failure == 'lease':
            await db.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 second'")
        return failure != 'unproven'
    if failure == 'commit':
        await db.execute("ALTER TABLE runtime_commands ADD CHECK (id<>'stop' OR status<>'completed')")
    prepare, _ = prepare_reply([], entered=entered)
    run = asyncio.create_task(RunningExecutionController(pool, verify_cleanup=verify,
        poll_interval=.01).start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(entered.wait(),3)
        await stop_intent(db)
        with pytest.raises(ExecutionAborted):
            await asyncio.wait_for(run,5)
        assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 0
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run,return_exceptions=True)


@pytest.mark.parametrize('success_first', [False, True])
async def test_parallel_stop_preserves_success_and_joins_all_tools(database, success_first):
    pool, db = database
    tool = WaitingTool(2,success_first=success_first)
    prepare,_ = prepare_reply([[ToolCallBlock(id=str(i),name=tool.name,input='{}') for i in range(2)]],tool=tool)
    async def verify(*args):
        assert tool.exited == 2
        return True
    run = asyncio.create_task(RunningExecutionController(pool,verify_cleanup=verify,poll_interval=.01)
        .start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(tool.entered.wait(),3)
        await stop_intent(db)
        result = await asyncio.wait_for(run,5)
        assert result.status == 'interrupted' and tool.calls == tool.exited == 2
        states = [json.loads(r['payload'])['state'] for r in await db.fetch(
            "SELECT payload FROM runtime_events WHERE payload->>'type'='TOOL_RESULT_END'")]
        assert states.count('interrupted') == (1 if success_first else 2)
        assert len(states) == 2
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run,return_exceptions=True)


async def test_stop_while_event_database_write_is_blocked_does_not_cancel_consumer(database):
    pool, db = database
    await db.execute('SELECT pg_advisory_lock(918372)')
    await db.execute("""CREATE FUNCTION hold_running_delta() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.payload->>'type'='TOOL_RESULT_TEXT_DELTA' THEN PERFORM pg_advisory_xact_lock(918372); END IF; RETURN NEW; END $$;
        CREATE TRIGGER hold_delta BEFORE INSERT ON runtime_events FOR EACH ROW EXECUTE FUNCTION hold_running_delta();""")
    tool = WaitingTool()
    prepare,_ = prepare_reply([[ToolCallBlock(id='one',name=tool.name,input='{}')]],tool=tool)
    run = asyncio.create_task(RunningExecutionController(pool,verify_cleanup=quiescent,poll_interval=.01)
        .start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(tool.entered.wait(),3)
        async with asyncio.timeout(5):
            while not await db.fetchval("SELECT count(*) FROM pg_stat_activity WHERE wait_event='advisory' AND query LIKE '%runtime_events%'"):
                await asyncio.sleep(.01)
        await stop_intent(db)
        assert not run.done()
        await db.execute('SELECT pg_advisory_unlock(918372)')
        result = await asyncio.wait_for(run,5)
        assert result.status == 'interrupted' and tool.exited == 1
    finally:
        await db.execute('SELECT pg_advisory_unlock(918372)')
        if not run.done():
            run.cancel()
        await asyncio.gather(run,return_exceptions=True)


async def test_resume_running_stop_completes_the_bound_resume_not_another_command(database):
    from test_resume_controller import parked,submit,prepare_resume,allow
    pool,db = database
    first = await parked(pool,db)
    await submit(db,first.reply_id)
    tool = WaitingTool()
    run = asyncio.create_task(RunningExecutionController(pool,verify_cleanup=quiescent,poll_interval=.01)
        .resume('resume','worker','node',prepare_resume(tool),authorize_resume=allow))
    try:
        await asyncio.wait_for(tool.entered.wait(),3)
        await stop_intent(db)
        result = await asyncio.wait_for(run,5)
        assert result.reply_id == first.reply_id and result.status == 'interrupted'
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 3
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run,return_exceptions=True)


@pytest.mark.parametrize('stop', [False, True])
async def test_authority_loss_never_becomes_safe_stop(database, stop):
    pool, db = database
    entered = asyncio.Event()
    prepare,_ = prepare_reply([],entered=entered)
    async def never(*args):
        raise AssertionError('lost owner cannot verify cleanup for completion')
    run = asyncio.create_task(RunningExecutionController(pool,verify_cleanup=never,
        lease_seconds=1,poll_interval=.01).start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(entered.wait(),3)
        await db.execute("UPDATE runtime_sessions SET owner_id='foreign'")
        if stop:
            await stop_intent(db)
        with pytest.raises(ExecutionAborted):
            await asyncio.wait_for(run,3)
        assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run,return_exceptions=True)


@pytest.mark.parametrize('park', [False,True])
async def test_running_controller_without_stop_preserves_normal_boundaries(database,park):
    pool,db = database
    tool = ConfirmTool()
    responses = [[ToolCallBlock(id='a',name=tool.name,input='{}')]] if park else [[TextBlock(text='done')]]
    prepare,_ = prepare_reply(responses,tool=tool)
    result = await RunningExecutionController(pool,verify_cleanup=quiescent).start('command','worker','node',prepare)
    assert result.status == ('awaiting_interaction' if park else 'completed')
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'completed'


async def test_stop_after_durable_natural_end_preserves_actual_reason(database):
    pool,db = database
    class EndRace(RunningExecutionController):
        async def _persist_or_buffer(self,guard,events,buffered):
            await super()._persist_or_buffer(guard,events,buffered)
            if events[-1]['type'] == 'REPLY_END':
                await stop_intent(db)
    prepare,_ = prepare_reply([[TextBlock(text='already finished')]])
    result = await EndRace(pool,verify_cleanup=quiescent).start('command','worker','node',prepare)
    assert result.status == 'completed'
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'") == 1
    assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 2


async def test_stop_at_final_boundary_is_serialized_with_commit(database, monkeypatch):
    from workmate_runtime.transport.stopping import StopStore
    pool, db = database
    locked = StopStore._locked
    contenders = []
    async def racing_lock(store, command_id):
        rows = await locked(store, command_id)
        if command_id == 'command':
            async def admit():
                async with db.transaction():
                    # Product locks this row before publishing Stop intent.
                    await db.fetchval("SELECT id FROM conversations WHERE id='conversation' FOR UPDATE")
                    await seed_stop_intent(db)
            contender = asyncio.create_task(admit())
            contenders.append(contender)
            done, _ = await asyncio.wait([contender], timeout=.05)
            assert not done, 'Stop admission must wait for the final boundary transaction'
        return rows
    monkeypatch.setattr(StopStore, '_locked', racing_lock)
    prepare,_ = prepare_reply([[TextBlock(text='done')]])
    result = await RunningExecutionController(pool,verify_cleanup=quiescent).start('command','worker','node',prepare)
    assert contenders, 'The final boundary must take Product admission locks'
    await asyncio.gather(*contenders)
    assert result.status == 'completed'


async def seed_running(pool, db):
    from workmate_runtime.transport.postgres import ExecutionStore
    async with pool.acquire() as conn:
        store = ExecutionStore(conn)
        owner = await store.claim_start('command','worker','node',lease_seconds=30)
        await store.begin_reply(owner,'reply',{'type':'REPLY_START','reply_id':'reply'})
    await stop_intent(db)
    return owner


@pytest.mark.parametrize('mutation', [
    "UPDATE runtime_sessions SET owner_id='foreign'",
    "UPDATE runtime_sessions SET execution_epoch=execution_epoch+1",
    "UPDATE runtime_sessions SET lease_until=now()-interval '1 second'",
    "UPDATE runtime_sessions SET storage_node_id='foreign'",
    "UPDATE runtime_sessions SET status='awaiting_interaction'",
    "UPDATE runtime_task_executions SET state_version=state_version+1",
    "UPDATE runtime_commands SET claimed_by='foreign' WHERE id='command'",
    "UPDATE runtime_commands SET status='completed' WHERE id='command'",
    "UPDATE runtime_commands SET actor_user_id='foreign' WHERE id='stop'",
    "UPDATE tasks SET status='running' WHERE id='task'",
    "UPDATE conversations SET active_product_task_id='other-task'",
])
async def test_running_stop_store_rejects_inconsistent_or_foreign_claim(database,mutation):
    from workmate_runtime.transport.running import RunningStopStore
    pool,db = database
    owner = await seed_running(pool,db)
    if "actor_user_id='foreign'" in mutation:
        await db.execute("INSERT INTO users VALUES ('foreign','active')")
    await db.execute(mutation)
    async with pool.acquire() as conn:
        assert await RunningStopStore(conn).claim_pending(owner,'command','node') is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'") == 'accepted'


async def test_running_stop_claim_is_exclusive_and_does_not_grant_ordinary_writes(database):
    from workmate_runtime.transport.running import RunningStopStore
    from workmate_runtime.transport.postgres import ExecutionStore,LostOwnership
    pool,db = database
    owner = await seed_running(pool,db)
    async def claim():
        async with pool.acquire() as conn:
            return await RunningStopStore(conn).claim_pending(owner,'command','node')
    claims = await asyncio.gather(claim(),claim())
    assert sum(c is not None for c in claims) == 1
    async with pool.acquire() as conn:
        with pytest.raises(LostOwnership):
            await ExecutionStore(conn).append_events(owner,'reply',events=[{'type':'text'}])


@pytest.mark.parametrize('invalid', [0,-1,2,True,float('nan'),None])
def test_running_controller_rejects_invalid_poll_intervals(invalid):
    with pytest.raises(ValueError):
        RunningExecutionController(None,verify_cleanup=quiescent,poll_interval=invalid)


def test_running_controller_requires_trusted_cleanup_callback():
    with pytest.raises(TypeError):
        RunningExecutionController(None,verify_cleanup=None)


@pytest.mark.parametrize('hook', ['model','compression'])
@pytest.mark.parametrize('awaiting_stream_delivery', [False,True])
async def test_stop_discovered_inside_core_guard_does_not_double_cancel_cleanup(database,hook,awaiting_stream_delivery):
    from agentscope.middleware import MiddlewareBase
    from agentscope.state import AgentState
    from agentscope.message import UserMsg
    from test_core_engine import make_engine
    from workmate_runtime.permission_policy import ProductPermissionMiddleware,ExecutionPermissionPolicy
    from workmate_runtime.execution.controller import PreparedExecution
    pool,db = database
    checks = []
    class BeforeGuard(MiddlewareBase):
        async def on_model_call(self, agent, input_kwargs, next_handler):
            if hook == 'model':
                await stop_intent(db)
            return await next_handler(**input_kwargs)
        async def on_compress_context(self,agent,input_kwargs,next_handler):
            if hook == 'compression':
                await stop_intent(db)
            return await next_handler(**input_kwargs)
    class NoPolling(RunningExecutionController):
        async def _watch_stop(self,guard):
            await asyncio.Event().wait()
    async def prepare(owner,snapshot,guard):
        original = guard.claim_stop
        async def observe(conn, **kwargs):
            result = await original(conn, **kwargs)
            if result:
                checks.append(asyncio.current_task() is guard.next_core)
            return result
        guard.claim_stop = observe
        engine = make_engine([],state=AgentState(session_id=owner.session_id),middlewares=[BeforeGuard(),
            ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='standard')),guard])
        if hook == 'compression':
            engine.agent.context_config.trigger_ratio = .000001
        return PreparedExecution(engine,UserMsg(name='user',content='test'))
    controller = NoPolling(pool,verify_cleanup=quiescent)
    if awaiting_stream_delivery:
        from types import SimpleNamespace
        controller.command_source = SimpleNamespace(control_ids=lambda _: ())
    result = await controller.start('command','worker','node',prepare)
    assert result.status == 'interrupted' and True in checks
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'") == 1


async def test_poll_failure_cancels_and_joins_core_without_terminal(database):
    pool,db = database
    tool = WaitingTool()
    prepare,_ = prepare_reply([[ToolCallBlock(id='one',name=tool.name,input='{}')]],tool=tool)
    class FailedPoll(RunningExecutionController):
        async def _watch_stop(self,guard):
            await tool.entered.wait()
            raise ConnectionError('test poll lost database')
    with pytest.raises(ExecutionAborted):
        await FailedPoll(pool,verify_cleanup=quiescent).start('command','worker','node',prepare)
    assert tool.exited == 1
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0


async def test_stop_during_queue_backpressure_cancels_only_next_core_advance(database):
    pool,db = database
    paused,release = asyncio.Event(),asyncio.Event()
    class BurstingTool(WaitingTool):
        async def __call__(self, **kwargs):
            self.calls += 1
            self.emitted = 0
            try:
                for i in range(200):
                    self.emitted += 1
                    yield ToolChunk(content=[TextBlock(text=str(i))],is_last=False)
                await asyncio.Event().wait()
            finally:
                self.exited += 1
    class SlowConsumer(RunningExecutionController):
        async def _produce(self,guard,engine,inputs,queue):
            self.queue = queue
            return await super()._produce(guard,engine,inputs,queue)
        async def _persist_or_buffer(self,guard,events,buffered):
            if events[0]['type']=='TOOL_RESULT_TEXT_DELTA' and not paused.is_set():
                paused.set()
                await release.wait()
            return await super()._persist_or_buffer(guard,events,buffered)
    tool = BurstingTool()
    tool.is_concurrency_safe = False  # Core's concurrent batch has its own upstream queue.
    prepare,_ = prepare_reply([[ToolCallBlock(id='one',name=tool.name,input='{}')]],tool=tool)
    controller = SlowConsumer(pool,verify_cleanup=quiescent,poll_interval=.01)
    run = asyncio.create_task(controller.start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(paused.wait(),3)
        async with asyncio.timeout(3):
            while not controller.queue.full():
                await asyncio.sleep(0)
        assert tool.emitted < 200  # Core cannot outrun the bounded consumer queue.
        await stop_intent(db)
        async with asyncio.timeout(3):
            while await db.fetchval('SELECT status FROM runtime_sessions') != 'stopping':
                await asyncio.sleep(.01)
        assert not run.done()
        release.set()
        result = await asyncio.wait_for(run,5)
        assert result.status == 'interrupted' and tool.exited == 1
    finally:
        release.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run,return_exceptions=True)


async def test_approved_platform_binding_finishes_same_reply_without_context_loss(database):
    from contextvars import ContextVar
    from types import SimpleNamespace
    from pydantic import BaseModel
    from workmate_runtime.assembly.platform_tools import PlatformCallBinding
    from workmate_runtime.permission_policy import ProductPermissionMiddleware, ExecutionPermissionPolicy
    from test_resume_controller import parked, submit, allow
    from test_core_engine import make_engine

    class Arguments(BaseModel):
        pass

    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    tool = ConfirmTool()
    identity = ContextVar('approved_platform_call', default=None)

    async def prepare(owner, snapshot, guard):
        adapter = SimpleNamespace(schemas={tool.name: Arguments}, current=identity, guard=guard)
        return make_engine([[TextBlock(text='created successfully')]], state=snapshot.to_state(),
            tool=tool, middlewares=[PlatformCallBinding(adapter), ProductPermissionMiddleware(
                ExecutionPermissionPolicy(task_id=owner.task_id, approval_profile='standard')), guard])

    result = await RunningExecutionController(pool, verify_cleanup=quiescent).resume(
        'resume', 'worker', 'node', prepare, authorize_resume=allow)
    assert result.status == 'completed' and result.reply_id == first.reply_id
    assert tool.calls == 1 and identity.get() is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'completed'
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'") == 1


async def test_ready_tool_deltas_are_batched_without_changing_durable_order(database):
    pool, db = database
    class Burst(WaitingTool):
        is_concurrency_safe = False

        async def __call__(self, **kwargs):
            self.calls += 1
            for i in range(40):
                yield ToolChunk(content=[TextBlock(text=f'{i},')], is_last=i == 39)

    class RecordingController(RunningExecutionController):
        async def _persist_or_buffer(self, guard, events, buffered):
            batches.append(events)
            await super()._persist_or_buffer(guard, events, buffered)

    batches = []
    tool = Burst()
    prepare, _ = prepare_reply([[ToolCallBlock(id='call', name=tool.name, input='{}')],
                               [TextBlock(text='done')]], tool=tool)
    result = await RecordingController(pool, verify_cleanup=quiescent).start('command', 'worker', 'node', prepare)
    assert result.status == 'completed'
    assert tool.calls == 1
    assert any(len(batch) > 1 for batch in batches)
    for batch in batches:
        assert len(batch) <= 16
        if len(batch) > 1:
            assert all(e['type'] in {'TEXT_BLOCK_DELTA','TOOL_CALL_DELTA','TOOL_RESULT_TEXT_DELTA'} for e in batch)
    stored = [json.loads(row['payload']) for row in await db.fetch('SELECT payload FROM runtime_events ORDER BY runtime_seq')]
    assert stored[0]['type'] == 'REPLY_START'
    assert stored[-1]['type'] == 'EXECUTION_BOUNDARY'
    # Ready tool deltas are losslessly compacted before persistence. Compare
    # ordered content and semantic metadata across the same event boundaries,
    # rather than requiring the obsolete one-record-per-Core-chunk layout.
    def content_trace(events):
        trace = []
        for event in events:
            if event['type'] in {'TOOL_CALL_DELTA', 'TOOL_RESULT_TEXT_DELTA'}:
                identity = {key: value for key, value in event.items()
                            if key not in {'id', 'created_at', 'delta'}}
                trace.extend((identity, character) for character in event['delta'])
            else:
                trace.append(event)
        return trace
    emitted = [event for batch in batches for event in batch]
    assert content_trace(stored[1:-1]) == content_trace(emitted)
    originals = {event['id']: event for event in emitted}
    for event in stored[1:-1]:
        assert event['created_at'] == originals[event['id']]['created_at']
        if event['type'] in {'TOOL_CALL_DELTA', 'TOOL_RESULT_TEXT_DELTA'}:
            assert event['delta'].startswith(originals[event['id']]['delta'])
    assert ''.join(e['delta'] for e in stored if e['type']=='TOOL_RESULT_TEXT_DELTA') == ''.join(f'{i},' for i in range(40))


async def test_sequential_tool_stop_drains_acting_scope_before_boundary(database):
    """Bash-like sequential tools stop at INTERRUPTED without resuming acting."""
    pool, db = database
    class SequentialTool(WaitingTool):
        is_concurrency_safe = False
    tool = SequentialTool()
    prepare, _ = prepare_reply([[ToolCallBlock(id='sequential', name=tool.name, input='{}')]], tool=tool)
    async def verify(*args):
        assert tool.exited == 1
        return True
    run = asyncio.create_task(RunningExecutionController(pool, verify_cleanup=verify, poll_interval=.01)
        .start('command', 'worker', 'node', prepare))
    try:
        await asyncio.wait_for(tool.entered.wait(), 3)
        await stop_intent(db)
        result = await asyncio.wait_for(run, 5)
        assert result.status == 'interrupted'
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 2
        assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'") == 1
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
