"""Parked Stop uses Core interruption, never resumes business execution."""
import asyncio
import json

import pytest

from test_postgres_execution_store import database  # noqa: F401
from test_resume_controller import parked, submit, prepare_resume, allow
from test_core_engine import ConfirmTool
from workmate_runtime.execution.controller import ExecutionController, ExecutionAborted
from workmate_runtime.execution.stopping import ParkedStopController
from workmate_runtime.transport.stopping import StopStore


async def stop_intent(db):
    await db.execute("UPDATE tasks SET status='cancelling' WHERE id='task'")
    await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
        kind,expected_epoch,payload,content_digest,reply_id) VALUES
        ('stop','session','conversation','task','user','StopTask',0,'{}',repeat('d',64),'old-observation')""")
    await db.execute("UPDATE runtime_commands SET status='rejected' WHERE kind='ResumeInteraction' AND status='accepted'")


async def quiescent(owner, original, stopped):
    return True  # Fixtures own no subprocesses or external jobs.


async def test_concurrent_parked_stop_closes_calls_and_retains_gates(database):
    pool, db = database
    first = await parked(pool, db, partial=True)
    await stop_intent(db)
    tool = ConfirmTool()
    controller = ParkedStopController(pool)
    results = await asyncio.gather(*(controller.stop('stop','worker','node',prepare_resume(tool),
        verify_cleanup=quiescent) for _ in range(2)))
    result = next(r for r in results if r)
    assert sum(r is not None for r in results)==1
    assert result.status=='interrupted' and result.reply_id==first.reply_id
    assert result.pending_tool_call_ids==() and tool.calls==0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
    assert await db.fetchval('SELECT status FROM runtime_sessions')=='interrupted'
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    assert await db.fetchval('SELECT active_product_task_id FROM conversations')=='task'
    assert await controller.stop('stop','worker','node',prepare_resume(tool),verify_cleanup=quiescent) is None


async def test_submitted_cleanup_requires_external_quiescence(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db,first.reply_id)
    tool = ConfirmTool()
    tool.is_external_tool=True
    resumed = await ExecutionController(pool).resume('resume','worker','node',prepare_resume(tool),authorize_resume=allow)
    assert resumed.status=='awaiting_interaction'
    before = await db.fetchval('SELECT runtime_seq FROM runtime_sessions')
    await stop_intent(db)
    result = await ParkedStopController(pool).stop('stop','worker','node',prepare_resume(tool),verify_cleanup=quiescent)
    assert result.status=='interrupted' and tool.calls==0
    events = [json.loads(r['payload']) for r in await db.fetch('SELECT payload FROM runtime_events WHERE runtime_seq>$1 ORDER BY runtime_seq',before)]
    assert not any(e['type']=='TOOL_RESULT_START' for e in events)
    assert any(e['type']=='TOOL_RESULT_END' and e['state']=='interrupted' for e in events)


@pytest.mark.parametrize('mutation', ["UPDATE runtime_sessions SET owner_id='other'",
    "UPDATE runtime_sessions SET lease_until=now()-interval '1 second'",
    "UPDATE runtime_sessions SET storage_node_id='elsewhere'",
    "UPDATE runtime_sessions SET status='running'",
    "UPDATE runtime_task_executions SET snapshot_id=NULL",
    "UPDATE conversations SET active_product_task_id='other-task'",
    "UPDATE tasks SET status='awaiting_permission' WHERE id='task'"])
async def test_stop_requires_current_parked_owner(database, mutation):
    pool, db = database
    await parked(pool,db)
    await stop_intent(db)
    await db.execute(mutation)
    async def never(*args):
        raise AssertionError('must not prepare')
    assert await ParkedStopController(pool).stop('stop','worker','node',never,verify_cleanup=quiescent) is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='accepted'


async def test_agent_deletion_does_not_block_existing_stop_intent(database):
    pool, db = database
    await parked(pool,db)
    await stop_intent(db)
    await db.execute('UPDATE agents SET deleted_at=now()')
    # Accepted intent remains executable even if the actor is subsequently disabled.
    await db.execute("UPDATE users SET status='disabled'")
    result = await ParkedStopController(pool).stop('stop','worker','node',prepare_resume(ConfirmTool()),verify_cleanup=quiescent)
    assert result.status=='interrupted'


@pytest.mark.parametrize('failure', ['unproven','lease','commit'])
async def test_no_terminal_or_completed_command_without_proof_and_atomic_commit(database, failure):
    pool, db = database
    await parked(pool,db)
    await stop_intent(db)
    before = await db.fetchval('SELECT runtime_seq FROM runtime_sessions')
    if failure=='commit':
        await db.execute("ALTER TABLE runtime_commands ADD CHECK (id<>'stop' OR status<>'completed')")
    async def verify(*args):
        if failure=='lease':
            await db.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 second'")
        return failure!='unproven'
    with pytest.raises(ExecutionAborted):
        await ParkedStopController(pool).stop('stop','worker','node',prepare_resume(ConfirmTool()),verify_cleanup=verify)
    assert await db.fetchval('SELECT state_version FROM runtime_sessions')==1
    assert await db.fetchval('SELECT runtime_seq FROM runtime_sessions')==before
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='claimed'
    assert await db.fetchval('SELECT status FROM runtime_sessions')=='stopping'


async def test_claimed_resume_cannot_be_overwritten_by_parked_stop(database):
    from workmate_runtime.transport.controls import ResumeStore
    pool, db = database
    first = await parked(pool,db)
    await submit(db,first.reply_id)
    assert await ResumeStore(db).claim_resume('resume','worker','node',authorize_resume=allow)
    await stop_intent(db)
    assert await StopStore(db).claim_parked('stop','worker','node') is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='claimed'


@pytest.mark.parametrize('change', ['state','missing_end','wrong_tool'])
async def test_unsafe_cleanup_assembly_or_events_cannot_complete(database, change):
    from agentscope.middleware import MiddlewareBase
    from test_core_engine import make_engine
    from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware
    pool, db = database
    await parked(pool,db)
    await stop_intent(db)
    tool = ConfirmTool()
    class AlterCleanup(MiddlewareBase):
        async def on_reply(self, agent, input_kwargs, next_handler):
            async for event in next_handler(**input_kwargs):
                if change=='missing_end' and event.type=='REPLY_END':
                    continue
                if change=='wrong_tool' and event.type=='TOOL_RESULT_END':
                    event = event.model_copy(update={'tool_call_id':'unrelated'})
                yield event
    async def prepare(owner,snapshot,guard):
        engine = make_engine([],state=snapshot.to_state(),tool=tool,middlewares=[AlterCleanup(),
            ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='read_only')),guard])
        if change=='state':
            engine.agent.state.summary='changed'
        return engine
    with pytest.raises(ExecutionAborted):
        await ParkedStopController(pool).stop('stop','worker','node',prepare,verify_cleanup=quiescent)
    assert tool.calls==0
    assert await db.fetchval('SELECT state_version FROM runtime_sessions')==1
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='claimed'


async def test_cleanup_events_wait_for_full_drain_and_resource_proof(database):
    pool, db = database
    await parked(pool,db)
    await stop_intent(db)
    before = await db.fetchval('SELECT runtime_seq FROM runtime_sessions')
    entered,release = asyncio.Event(),asyncio.Event()
    async def verify(*args):
        entered.set()
        await release.wait()
        return True
    run = asyncio.create_task(ParkedStopController(pool).stop('stop','worker','node',prepare_resume(ConfirmTool()),verify_cleanup=verify))
    try:
        await asyncio.wait_for(entered.wait(),5)
        assert await db.fetchval('SELECT runtime_seq FROM runtime_sessions')==before
        assert await db.fetchval('SELECT status FROM runtime_sessions')=='stopping'
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='claimed'
    finally:
        release.set()
        result = await run
    assert result.state_version==2


@pytest.mark.parametrize('hook', ['on_model_call','on_compress_context','on_acting'])
async def test_stop_guard_never_grants_business_io(database, hook):
    pool, db = database
    await parked(pool,db)
    await stop_intent(db)
    invoked=[]
    async def forbidden(**kwargs):
        invoked.append('io')
    async def prepare(owner,snapshot,guard):
        if hook=='on_acting':
            async for _ in guard.on_acting(None,{},forbidden):
                pass
        else:
            await getattr(guard,hook)(None,{},forbidden)
        raise AssertionError('guard must abort')
    with pytest.raises(ExecutionAborted):
        await ParkedStopController(pool).stop('stop','worker','node',prepare,verify_cleanup=quiescent)
    assert invoked==[]


async def test_normal_running_writer_cannot_append_during_stop(database):
    from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership
    pool, db = database
    first = await parked(pool,db)
    await stop_intent(db)
    assert await StopStore(db).claim_parked('stop','worker','node')
    with pytest.raises(LostOwnership):
        await ExecutionStore(db).append_events(first.owner,first.reply_id,[{'type':'TEXT_BLOCK_DELTA','reply_id':first.reply_id,'delta':'must not publish'}])
