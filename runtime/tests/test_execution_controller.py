"""Real Core + migrated PostgreSQL; deterministic models, no external services."""
import asyncio
import json

import pytest
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.state import AgentState

from test_core_engine import make_engine, ConfirmTool
from test_postgres_execution_store import database
from workmate_runtime.execution.controller import ExecutionController, ExecutionAborted, PreparedExecution
from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware


def prepare_reply(responses, *, tool=None, entered=None, before_model=None):
    calls = []
    async def prepare(owner, snapshot, guard):
        calls.append(owner)
        engine = make_engine(responses, state=snapshot.to_state() if snapshot else AgentState(session_id=owner.session_id),
            tool=tool, entered=entered, middlewares=[ProductPermissionMiddleware(
                ExecutionPermissionPolicy(task_id=owner.task_id, approval_profile='standard')), guard])
        if before_model:
            original = engine.agent.model._call_api
            async def checked(*args, **kwargs):
                await before_model()
                return await original(*args, **kwargs)
            engine.agent.model._call_api = checked
        return PreparedExecution(engine, UserMsg(name='user', content='hello'))
    return prepare, calls


async def test_controller_persists_start_before_model_and_drained_boundary(database):
    pool, db = database
    async def before_model():
        assert await db.fetchval('SELECT status FROM runtime_sessions') == 'running'
        assert await db.fetchval('SELECT reply_id FROM runtime_task_executions')
        assert json.loads(await db.fetchval('SELECT payload FROM runtime_events ORDER BY runtime_seq LIMIT 1'))['type'] == 'REPLY_START'
    prepare, calls = prepare_reply([[TextBlock(text='hello back')]], before_model=before_model)
    result = await ExecutionController(pool).start('command', 'worker', 'node', prepare)
    assert result.status == 'completed'
    assert len(calls) == 1
    assert await db.fetchval('SELECT state_version FROM runtime_sessions') == 1
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'
    snapshot = json.loads(await db.fetchval('SELECT payload FROM runtime_snapshots'))
    assert snapshot['engine_version'] == '2.0.7'
    assert 'hello back' in json.dumps(snapshot)
    assert await ExecutionController(pool).start('command', 'worker-2', 'node', prepare) is None
    assert len(calls) == 1


async def test_controller_parks_without_executing_unapproved_tool(database):
    pool, db = database
    tool = ConfirmTool()
    prepare, _ = prepare_reply([[ToolCallBlock(id='call',name=tool.name,input='{}')]], tool=tool)
    result = await ExecutionController(pool).start('command', 'worker', 'node', prepare)
    assert result.status == 'awaiting_interaction'
    assert tool.calls == 0
    assert result.pending_tool_call_ids == ('call',)
    from workmate_contracts import BoundaryEvidence, snapshot_digest
    payload=json.loads(await db.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='EXECUTION_BOUNDARY'"))
    evidence=BoundaryEvidence.model_validate(payload['boundary_evidence'])
    saved=json.loads(await db.fetchval('SELECT payload FROM runtime_snapshots'))
    assert evidence.snapshot_sha256==snapshot_digest(saved)
    assert [(call.id,call.kind,call.input) for call in evidence.pending_tools]==[('call','permission','{}')]
    assert await db.fetchval('SELECT status FROM runtime_task_executions') == 'awaiting_interaction'


async def test_start_commit_failure_prevents_model_execution(database):
    pool, db = database
    calls = []
    async def before_model():
        calls.append('model')
    await db.execute("ALTER TABLE runtime_events ADD CHECK (payload->>'type' <> 'REPLY_START')")
    prepare, _ = prepare_reply([[TextBlock(text='never')]], before_model=before_model)
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).start('command', 'worker', 'node', prepare)
    assert not calls
    assert await db.fetchval('SELECT status FROM runtime_sessions') == 'preparing'
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0


async def test_lease_loss_cancels_blocked_model_and_does_not_save_terminal(database):
    pool, db = database
    entered = asyncio.Event()
    prepare, calls = prepare_reply([], entered=entered)
    controller = ExecutionController(pool, lease_seconds=1)
    execution = asyncio.create_task(controller.start('command', 'worker', 'node', prepare))
    await asyncio.wait_for(entered.wait(), 5)
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    with pytest.raises(ExecutionAborted):
        await asyncio.wait_for(execution, 5)
    assert await db.fetchval('SELECT status FROM runtime_sessions') == 'running'
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await controller.start('command', 'replacement', 'node', prepare) is None
    assert len(calls) == 1


async def test_missing_execution_guard_is_rejected_before_model(database):
    pool, db = database
    async def prepare(owner, snapshot, guard):
        return PreparedExecution(make_engine([[TextBlock(text='never')]]), UserMsg(name='user',content='hi'))
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).start('command', 'worker', 'node', prepare)
    assert await db.fetchval('SELECT count(*) FROM runtime_events') == 0


async def test_heartbeat_renews_while_core_model_is_waiting(database):
    pool, db = database
    entered = asyncio.Event()
    prepare, _ = prepare_reply([], entered=entered)
    run = asyncio.create_task(ExecutionController(pool, lease_seconds=1).start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        first = await db.fetchval('SELECT lease_until FROM runtime_sessions')
        async with asyncio.timeout(5):
            while await db.fetchval('SELECT lease_until FROM runtime_sessions') <= first:
                await asyncio.sleep(0.02)
        assert not run.done()
    finally:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0


async def test_reply_end_is_not_terminal_until_middleware_has_drained(database):
    from agentscope.middleware import MiddlewareBase
    pool, db = database
    cleanup_entered, release = asyncio.Event(), asyncio.Event()
    class Cleanup(MiddlewareBase):
        async def on_reply(self, agent, input_kwargs, next_handler):
            async for event in next_handler(**input_kwargs):
                yield event
            cleanup_entered.set()
            await release.wait()
    async def prepare(owner, snapshot, guard):
        engine = make_engine([[TextBlock(text='done')]], state=AgentState(session_id=owner.session_id),
            middlewares=[Cleanup(), ProductPermissionMiddleware(ExecutionPermissionPolicy(
                task_id=owner.task_id, approval_profile='standard')), guard])
        return PreparedExecution(engine, UserMsg(name='user',content='hi'))
    run = asyncio.create_task(ExecutionController(pool).start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'") == 1
        assert await db.fetchval('SELECT status FROM runtime_sessions') == 'running'
        assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    finally:
        release.set()
        result = await run
    assert result.status == 'completed'


async def test_loss_during_event_database_wait_does_not_start_model_or_save_snapshot(database):
    pool, db = database
    calls = []
    await db.execute("""
        CREATE FUNCTION delay_model_event() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.payload->>'type'='MODEL_CALL_START' THEN PERFORM pg_sleep(2); END IF; RETURN NEW; END $$;
        CREATE TRIGGER delay_insert BEFORE INSERT ON runtime_events
        FOR EACH ROW EXECUTE FUNCTION delay_model_event();
    """)
    async def before_model():
        calls.append('model')
    prepare, _ = prepare_reply([[TextBlock(text='never')]], before_model=before_model)
    with pytest.raises(ExecutionAborted):
        await asyncio.wait_for(ExecutionController(pool,lease_seconds=1).start('command','worker','node',prepare), 8)
    assert calls == []
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await db.fetchval('SELECT runtime_seq FROM runtime_sessions') == 1
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'


async def test_compression_checks_lease_before_direct_model_path(database):
    from agentscope.middleware import MiddlewareBase
    pool, db = database
    compression_calls = []
    class RevokeBeforeCompression(MiddlewareBase):
        async def on_compress_context(self, agent, input_kwargs, next_handler):
            await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
            return await next_handler(**input_kwargs)
    async def prepare(owner,snapshot,guard):
        engine = make_engine([[TextBlock(text='never')]], state=AgentState(session_id=owner.session_id),
            middlewares=[RevokeBeforeCompression(), ProductPermissionMiddleware(ExecutionPermissionPolicy(
                task_id=owner.task_id,approval_profile='standard')), guard])
        engine.agent.context_config.trigger_ratio = 0.000001
        async def compression(*args, **kwargs):
            compression_calls.append('compression')
            raise AssertionError('Compression model must be fenced')
        engine.agent.model.generate_structured_output = compression
        return PreparedExecution(engine, UserMsg(name='user',content='force compression'))
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).start('command','worker','node',prepare)
    assert compression_calls == []
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0


@pytest.mark.parametrize('database_wait', [False, True])
async def test_lease_loss_cancels_all_parallel_tools(database, database_wait):
    from agentscope.permission import PermissionBehavior, PermissionDecision
    pool, db = database
    entered, stopped = asyncio.Event(), asyncio.Event()
    if database_wait:
        await db.execute("""
            CREATE FUNCTION hold_tool_delta() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.payload->>'type'='TOOL_RESULT_TEXT_DELTA' THEN PERFORM pg_sleep(2); END IF; RETURN NEW; END $$;
            CREATE TRIGGER hold_tool_delta_insert BEFORE INSERT ON runtime_events FOR EACH ROW EXECUTE FUNCTION hold_tool_delta();
        """)
    class BlockingTool(ConfirmTool):
        is_concurrency_safe = True
        async def check_permissions(self, tool_input, context):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='deterministic fixture allowed')
        async def __call__(self, **kwargs):
            self.calls += 1
            if self.calls == 2:
                entered.set()
            try:
                await entered.wait()
                from agentscope.tool import ToolChunk
                yield ToolChunk(content=[TextBlock(text='waiting')], is_last=False)
                await asyncio.Event().wait()
            finally:
                self.exited = getattr(self, 'exited', 0) + 1
                if self.exited == 2:
                    stopped.set()
    tool = BlockingTool()
    tool.is_concurrency_safe = True
    prepare, _ = prepare_reply([[
        ToolCallBlock(id='one',name=tool.name,input='{}'),
        ToolCallBlock(id='two',name=tool.name,input='{}'),
    ]], tool=tool)
    run = asyncio.create_task(ExecutionController(pool,lease_seconds=1).start('command','worker','node',prepare))
    try:
        waiter = asyncio.create_task(entered.wait())
        done, _ = await asyncio.wait((run, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        assert entered.is_set(), (await run if run.done() else 'not finished', tool.calls,
            [json.loads(row['payload']) for row in await db.fetch('SELECT payload FROM runtime_events')])
        if not database_wait:
            await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        with pytest.raises(ExecutionAborted):
            await asyncio.wait_for(run, 5)
        assert stopped.is_set()
        assert tool.calls == tool.exited == 2
        assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.parametrize('invalid', ['session','missing_guard','missing_policy','input','parked'])
async def test_preparation_failures_never_start_core(database, invalid):
    from test_core_engine import load_golden_snapshot
    pool, db = database
    async def prepare(owner, snapshot, guard):
        state = load_golden_snapshot('parked').to_state() if invalid == 'parked' else AgentState(session_id=owner.session_id)
        state.session_id = 'foreign' if invalid == 'session' else owner.session_id
        policy = ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='standard'))
        middleware = ([policy] if invalid == 'missing_guard' else [guard] if invalid == 'missing_policy' else [policy,guard])
        engine = make_engine([],state=state,middlewares=middleware)
        return PreparedExecution(engine, None if invalid == 'input' else UserMsg(name='user',content='hi'))
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).start('command','worker','node',prepare)
    assert await db.fetchval('SELECT count(*) FROM runtime_events') == 0


async def test_next_task_rebuilds_core_from_prior_committed_context(database):
    pool, db = database
    inherited = []
    async def prepare(owner, snapshot, guard):
        inherited.append(snapshot)
        engine = make_engine([[TextBlock(text='answer for '+owner.task_id)]],
            state=snapshot.to_state() if snapshot else AgentState(session_id=owner.session_id),
            middlewares=[ProductPermissionMiddleware(ExecutionPermissionPolicy(
                task_id=owner.task_id,approval_profile='standard')),guard])
        return PreparedExecution(engine,UserMsg(name='user',content='question for '+owner.task_id))
    first = await ExecutionController(pool).start('command','worker','node',prepare)
    # Product delivery closure is simulated; the controller cannot release this gate.
    await db.execute("UPDATE runtime_sessions SET active_task_id=NULL")
    await db.execute("UPDATE conversations SET active_product_task_id='task-b'")
    await db.execute("INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task-b','session')")
    await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
        VALUES ('command-b','session','conversation','task-b','user','StartTask',1,'{}',repeat('d',64))""")
    second = await ExecutionController(pool).start('command-b','worker-b','node',prepare)
    assert first.reply_id != second.reply_id
    assert inherited[0] is None
    assert 'answer for task' in json.dumps(inherited[1].state)
    latest = json.loads(await db.fetchval('SELECT payload FROM runtime_snapshots ORDER BY state_version DESC LIMIT 1'))
    assert 'question for task-b' in json.dumps(latest) and 'answer for task-b' in json.dumps(latest)
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 2


async def test_competing_controllers_execute_model_once(database):
    pool, db = database
    prepare, calls = prepare_reply([[TextBlock(text='once')]])
    results = await asyncio.gather(*(ExecutionController(pool).start('command',f'worker-{i}','node',prepare) for i in range(3)))
    assert sum(result is not None for result in results) == 1
    assert len(calls) == 1
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 1


async def test_blocked_start_transaction_blocks_model_until_commit(database):
    pool, db = database
    model_calls = []
    await db.execute('SELECT pg_advisory_lock(986321)')
    await db.execute("""
        CREATE FUNCTION hold_start() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.payload->>'type'='REPLY_START' THEN PERFORM pg_advisory_xact_lock(986321); END IF; RETURN NEW; END $$;
        CREATE TRIGGER hold_start_insert BEFORE INSERT ON runtime_events FOR EACH ROW EXECUTE FUNCTION hold_start();
    """)
    async def before_model():
        model_calls.append('model')
    prepare, _ = prepare_reply([[TextBlock(text='after commit')]], before_model=before_model)
    run = asyncio.create_task(ExecutionController(pool).start('command','worker','node',prepare))
    try:
        async with asyncio.timeout(5):
            while not await db.fetchval("SELECT count(*) FROM pg_stat_activity WHERE wait_event='advisory' AND query LIKE '%runtime_events%'"):
                await asyncio.sleep(0.01)
        assert model_calls == []
        assert await db.fetchval('SELECT status FROM runtime_sessions') == 'preparing'
    finally:
        await db.execute('SELECT pg_advisory_unlock(986321)')
        await run
    assert model_calls == ['model']


def test_controller_import_is_independent_of_app_and_product_http():
    import os
    import subprocess
    import sys
    probe = """
import importlib.abc
import sys
class RejectHTTP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'fastapi' or fullname == 'app' or fullname.startswith(('app.', 'agentscope.app')):
            raise AssertionError('HTTP application dependency: ' + fullname)
sys.meta_path.insert(0, RejectHTTP())
from workmate_runtime.execution.controller import ExecutionController
"""
    result = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True,
                            timeout=15, env={**os.environ, 'COVERAGE_PROCESS_START': ''})
    assert result.returncode == 0, result.stderr
