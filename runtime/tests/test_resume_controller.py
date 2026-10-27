"""Same-owner durable permission resume using actual Core and PostgreSQL."""
import asyncio
import json

import pytest
from agentscope.message import TextBlock, ToolCallBlock

from test_core_engine import ConfirmTool, make_engine
from test_execution_controller import prepare_reply
from test_postgres_execution_store import database  # noqa: F401
from workmate_runtime.execution.controller import ExecutionController, ExecutionAborted
from workmate_runtime.transport.controls import ResumeStore
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware


async def allow(db, actor, agent, task):
    return True


async def parked(pool, db, *, partial=False):
    tool = ConfirmTool()
    tool.safety_ask = True
    tool.is_concurrency_safe = True
    calls = [ToolCallBlock(id='a',name=tool.name,input='{}')]
    if partial:
        calls.append(ToolCallBlock(id='b',name=tool.name,input='{}'))
    prepare, _ = prepare_reply([calls], tool=tool)
    result = await ExecutionController(pool).start('command','worker','node',prepare)
    await db.execute("UPDATE tasks SET status='awaiting_permission' WHERE id='task'")
    await db.execute('CREATE TABLE task_events(task_id text, event_type text, payload json)')
    requests = await db.fetch("SELECT payload FROM runtime_events WHERE payload->>'type'='REQUIRE_USER_CONFIRM' ORDER BY runtime_seq")
    originals = [call for row in requests for call in json.loads(row['payload'])['tool_calls']]
    await db.execute("INSERT INTO task_events VALUES ('task','hitl_request',$1::json)",
                     json.dumps({'decisionVersion':'request', 'replyId':result.reply_id,'toolCalls':originals}))
    await db.execute('CREATE TABLE task_projection_states(task_id text,session_id text,reply_id text,format_version int,last_runtime_seq bigint,state jsonb)')
    state = {'interactions':{'interaction':{'kind':'permission','request_version':'request',
        'reply_id':result.reply_id,'tool_calls':originals}},'pending_interactions':[]}
    await db.execute("INSERT INTO task_projection_states VALUES ('task','session',$1,1,0,$2::jsonb)",result.reply_id,json.dumps(state))
    return result


async def submit(db, reply, *, cid='resume', call='a', approved=True):
    row = await db.fetchrow('SELECT * FROM runtime_task_executions')
    saved = json.loads(await db.fetchval('SELECT state FROM task_projection_states'))
    snapshot = await db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',row['snapshot_id'])
    pending = CoreSnapshot.model_validate(json.loads(snapshot['payload'])).to_state().get_awaiting_tool_calls('test')
    saved = {**saved,'interaction_boundary_id':row['snapshot_id'],'pending_interactions':[{
        'interaction_id':'interaction','request_version':'request','kind':'permission','pending_tool_call_ids':[c.id for c in pending]}]}
    await db.execute('UPDATE task_projection_states SET state=$1::jsonb,last_runtime_seq=$2',json.dumps(saved),snapshot['runtime_seq'])
    payload = {'decision_version':cid, 'snapshot_id':row['snapshot_id'], 'state_version':row['state_version'],
               'decisions':[{'tool_call_id':call,'approved':approved}]}
    await db.execute('''INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
        kind,expected_epoch,payload,content_digest,reply_id,interaction_id,request_version)
        VALUES ($1,'session','conversation','task','user','ResumeInteraction',1,$2::jsonb,
        repeat('d',64),$3,'interaction','request')''', cid,json.dumps(payload),reply)
    await db.execute("INSERT INTO task_events VALUES ('task','hitl_decision',$1::json)", json.dumps({
        'decisionVersion':cid,'requestVersion':'request','actorUserId':'user','scope':'current_batch',
        'decisions':[{'toolCallId':call,'approved':approved}]}))
    return payload


def prepare_resume(tool, responses=None, *, denied=False):
    async def prepare(owner, snapshot, guard):
        return make_engine(responses or [], state=snapshot.to_state(), tool=tool,
            middlewares=[ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id=owner.task_id,
                approval_profile='standard',denied_tools=[tool.name] if denied else [])),guard])
    return prepare


async def test_concurrent_resume_keeps_reply_and_completes_command_at_boundary(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    tool = ConfirmTool()
    prepare = prepare_resume(tool, [[TextBlock(text='done')]])
    controller = ExecutionController(pool)
    results = await asyncio.gather(*(controller.resume('resume','worker','node',prepare,
        authorize_resume=allow) for _ in range(2)))
    result = next(r for r in results if r)
    assert sum(r is not None for r in results) == 1
    assert result.reply_id == first.reply_id and result.owner == first.owner
    assert result.status == 'completed' and tool.calls == 1
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'completed'
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'completed'
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'
    assert await db.fetchval('SELECT active_product_task_id FROM conversations') == 'task'
    assert await controller.resume('resume','worker','node',prepare,authorize_resume=allow) is None
    assert tool.calls == 1


async def test_two_partial_decisions_use_new_snapshot_without_replaying_first_tool(database):
    pool, db = database
    first = await parked(pool, db, partial=True)
    await submit(db, first.reply_id)
    tool = ConfirmTool()
    tool.safety_ask = True
    tool.is_concurrency_safe = True
    controller = ExecutionController(pool)
    middle = await controller.resume('resume','worker','node',prepare_resume(tool),authorize_resume=allow)
    assert middle.pending_tool_call_ids == ('b',) and middle.state_version == 2
    assert tool.calls == 1
    await submit(db, first.reply_id, cid='second',call='b')
    result = await controller.resume('second','worker','node',prepare_resume(tool,[[TextBlock(text='done')]]),
                                     authorize_resume=allow)
    assert result.status == 'completed' and result.reply_id == first.reply_id and tool.calls == 2


@pytest.mark.parametrize('mutation', ["UPDATE tasks SET status='cancelling'", "UPDATE users SET status='disabled'",
    "UPDATE agents SET deleted_at=now()", "UPDATE runtime_sessions SET owner_id='other'",
    "UPDATE runtime_sessions SET lease_until=now()-interval '1 second'",
    "UPDATE runtime_commands SET expected_epoch=0 WHERE id='resume'",
    "UPDATE runtime_commands SET payload=jsonb_set(payload,'{snapshot_id}','\"old\"') WHERE id='resume'",
    "DELETE FROM task_events WHERE event_type='hitl_decision'"])
async def test_ineligible_resume_never_calls_core(database, mutation):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    await db.execute(mutation)
    async def never(*args):
        raise AssertionError('Preparation must not run')
    assert await ExecutionController(pool).resume('resume','worker','node',never,authorize_resume=allow) is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'accepted'


async def test_current_deny_overrides_approved_snapshot(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    tool = ConfirmTool()
    result = await ExecutionController(pool).resume('resume','worker','node',
        prepare_resume(tool,[[TextBlock(text='denied')]],denied=True),authorize_resume=allow)
    assert result.status == 'completed' and tool.calls == 0


async def test_stop_after_claim_before_execution_does_not_run_tool(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    tool = ConfirmTool()
    async def prepare(owner, snapshot, guard):
        await db.execute("UPDATE tasks SET status='cancelling' WHERE id='task'")
        return await prepare_resume(tool)(owner,snapshot,guard)
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).resume('resume','worker','node',prepare,authorize_resume=allow)
    assert tool.calls == 0
    assert await db.fetchval('SELECT state_version FROM runtime_sessions') == 1
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'claimed'


async def test_command_completion_failure_rolls_back_snapshot_and_never_reexecutes(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    await db.execute("ALTER TABLE runtime_commands ADD CHECK (id <> 'resume' OR status <> 'completed')")
    tool = ConfirmTool()
    controller = ExecutionController(pool)
    prepare = prepare_resume(tool,[[TextBlock(text='done')]])
    with pytest.raises(ExecutionAborted):
        await controller.resume('resume','worker','node',prepare,authorize_resume=allow)
    assert tool.calls == 1
    assert await db.fetchval('SELECT state_version FROM runtime_sessions') == 1
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 1
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'claimed'
    assert await controller.resume('resume','worker','node',prepare,authorize_resume=allow) is None
    assert tool.calls == 1


@pytest.mark.parametrize('at', ['claim','begin'])
async def test_authorization_is_rechecked_before_claim_and_after_assembly(database, at):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    checks = []
    async def authorize(*args):
        checks.append(args[1:])
        return at=='begin' and len(checks)==1
    tool = ConfirmTool()
    operation = ExecutionController(pool).resume('resume','worker','node',prepare_resume(tool),authorize_resume=authorize)
    if at=='claim':
        assert await operation is None
    else:
        with pytest.raises(ExecutionAborted):
            await operation
    assert tool.calls == 0
    assert checks == [('user','agent','task')]*(1 if at=='claim' else 2)
    assert await db.fetchval('SELECT status FROM runtime_sessions') == 'awaiting_interaction'


@pytest.mark.parametrize('mutation', [
    "INSERT INTO task_events VALUES ('task','hitl_abandoned','{\"decisionVersion\":\"request\"}')",
    "UPDATE task_events SET payload='{\"decisionVersion\":\"resume\",\"decisions\":[{\"toolCallId\":\"a\",\"approved\":false}]}' WHERE event_type='hitl_decision'",
    "UPDATE runtime_commands SET reply_id='wrong' WHERE id='resume'",
    "UPDATE runtime_sessions SET storage_node_id='elsewhere'",
    "UPDATE runtime_commands SET interaction_id='wrong' WHERE id='resume'",
    "UPDATE task_events SET payload=jsonb_set(payload::jsonb,'{toolCalls}','[]')::json WHERE event_type='hitl_request'",
])
async def test_untrusted_decision_or_binding_cannot_be_claimed(database, mutation):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    await db.execute(mutation)
    assert await ResumeStore(db).claim_resume('resume','worker','node',authorize_resume=allow) is None


async def test_denial_runs_no_business_tool(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db,first.reply_id,approved=False)
    tool = ConfirmTool()
    result = await ExecutionController(pool).resume('resume','worker','node',prepare_resume(tool,[[TextBlock(text='denied')]]),
                                                   authorize_resume=allow)
    assert result.status == 'completed' and tool.calls == 0


async def test_preparation_cannot_replace_original_state(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db,first.reply_id)
    tool = ConfirmTool()
    async def prepare(owner,snapshot,guard):
        engine = await prepare_resume(tool)(owner,snapshot,guard)
        engine.agent.state.summary = 'replaced history'
        return engine
    with pytest.raises(ExecutionAborted,match='without releasing'):
        await ExecutionController(pool).resume('resume','worker','node',prepare,authorize_resume=allow)
    assert tool.calls == 0
    assert await db.fetchval('SELECT state_version FROM runtime_sessions') == 1


async def test_resume_start_event_failure_rolls_back_running_before_tool(database):
    pool, db = database
    first = await parked(pool, db)
    await submit(db,first.reply_id)
    await db.execute("""CREATE FUNCTION reject_resume_start() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.payload->>'type'='REPLY_START' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$;
        CREATE TRIGGER reject_start BEFORE INSERT ON runtime_events FOR EACH ROW EXECUTE FUNCTION reject_resume_start();""")
    tool = ConfirmTool()
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).resume('resume','worker','node',prepare_resume(tool),authorize_resume=allow)
    assert tool.calls == 0
    assert await db.fetchval('SELECT status FROM runtime_sessions') == 'awaiting_interaction'
    assert await db.fetchval('SELECT state_version FROM runtime_sessions') == 1


async def test_stop_after_begin_before_acting_fences_new_tool(database):
    from agentscope.middleware import MiddlewareBase
    pool, db = database
    first = await parked(pool,db)
    await submit(db,first.reply_id)
    tool = ConfirmTool()
    class StopBeforeActing(MiddlewareBase):
        async def on_acting(self, agent, input_kwargs, next_handler):
            await db.execute("UPDATE tasks SET status='cancelling' WHERE id='task'")
            async for event in next_handler(**input_kwargs):
                yield event
    async def prepare(owner,snapshot,guard):
        return make_engine([],state=snapshot.to_state(),tool=tool,middlewares=[StopBeforeActing(),
            ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='standard')),guard])
    with pytest.raises(ExecutionAborted):
        await ExecutionController(pool).resume('resume','worker','node',prepare,authorize_resume=allow)
    assert tool.calls == 0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'claimed'
    assert await db.fetchval('SELECT state_version FROM runtime_sessions') == 1
