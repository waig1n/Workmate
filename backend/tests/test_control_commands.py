"""Control admission against real Product schema and durable parked projection."""
import asyncio
import json

import pytest
from sqlalchemy.exc import IntegrityError

from workmate_contracts import Command
from workmate_product.application.commands import CommandConflict, CommandRejected
from workmate_product.application.control_commands import accept_resume, accept_stop
from test_durable_projection import projection_database  # noqa: F401
from test_durable_interactions import CALL, SECOND, request, park, consume


async def allow(*args):
    return True


async def parked(sessions, admin):
    await request(sessions, admin, [CALL, SECOND])
    seq = await park(admin, [CALL, SECOND])
    await consume(sessions)
    for table in ('runtime_sessions', 'runtime_task_executions'):
        await admin.execute(f"UPDATE {table} SET snapshot_id=$1,state_version=1,status='awaiting_interaction'", str(seq))
    state = json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    pending = state['pending_interactions'][0]
    return Command(command_id='resume', kind='ResumeInteraction', conversation_id='c',
        runtime_session_id='s', product_task_id='t', reply_id='reply', expected_epoch=1,
        interaction_id=pending['interaction_id'], request_version=pending['request_version'],
        payload={'decision_version':'decision', 'snapshot_id':str(seq), 'state_version':1,
                 'decisions':[{'tool_call_id':'a', 'approved':True}]})


def stop():
    return Command(command_id='stop', kind='StopTask', conversation_id='c',
        runtime_session_id='s', product_task_id='t', reply_id='reply', expected_epoch=1)


async def admit(sessions, command, actor='u', authorize=allow):
    async with sessions.begin() as db:
        if command.kind == 'ResumeInteraction':
            return await accept_resume(db, command, actor_user_id=actor, authorize_resume=authorize)
        return await accept_stop(db, command, actor_user_id=actor)


async def test_concurrent_resume_is_once_and_retains_runtime_pending(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    results = await asyncio.gather(*(admit(sessions, cmd) for _ in range(2)))
    assert sorted(r.duplicate for r in results) == [False, True]
    duplicate = await admit(sessions, cmd.model_copy(update={'command_id':'retry'}))
    assert duplicate.command_id == 'resume' and duplicate.duplicate
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'") == 1
    assert await admin.fetchval('SELECT status FROM tasks') == 'awaiting_permission'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
    state = json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['pending_interactions'][0]['pending_tool_call_ids'] == ['a', 'b']
    event = json.loads(await admin.fetchval("SELECT payload FROM product_events WHERE payload->>'name'='HITL_DECISION_RECORDED'"))
    assert event['value']['decisionVersion'] == cmd.request_version
    assert event['value']['decidedToolCallIds'] == ['a']


@pytest.mark.parametrize('change', ['snapshot', 'epoch', 'reply', 'request', 'interaction', 'unknown_call', 'full_access', 'parameters', 'duplicate_call', 'bool_coercion'])
async def test_resume_rejects_stale_or_untrusted_content(projection_database, change):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    updates = {'snapshot':{'payload':{**cmd.payload,'snapshot_id':'old'}}, 'epoch':{'expected_epoch':0},
        'reply':{'reply_id':'other'}, 'request':{'request_version':'old'}, 'interaction':{'interaction_id':'old'},
        'unknown_call':{'payload':{**cmd.payload,'decisions':[{'tool_call_id':'unknown','approved':True}]}},
        'full_access':{'payload':{**cmd.payload,'full_access':True}},
        'parameters':{'payload':{**cmd.payload,'decisions':[{'tool_call_id':'a','approved':True,'input':'{}'}]}},
        'duplicate_call':{'payload':{**cmd.payload,'decisions':cmd.payload['decisions']*2}},
        'bool_coercion':{'payload':{**cmd.payload,'decisions':[{'tool_call_id':'a','approved':'true'}]}}}
    with pytest.raises(ValueError):
        await admit(sessions, cmd.model_copy(update=updates[change]))
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0


async def test_version_conflict_busy_resume_and_retry_after_stop(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    await admit(sessions, cmd)
    for command_id in ('resume', 'different'):
        with pytest.raises(CommandConflict):
            await admit(sessions, cmd.model_copy(update={'command_id':command_id,
                'payload':{**cmd.payload,'decisions':[{'tool_call_id':'a','approved':False}]}}))
    with pytest.raises(CommandRejected):
        await admit(sessions, cmd.model_copy(update={'command_id':'second',
            'payload':{**cmd.payload,'decision_version':'second','decisions':[{'tool_call_id':'b','approved':True}]}}))
    await admit(sessions, stop())
    retry = await admit(sessions, cmd)
    assert retry.duplicate and retry.status == 'rejected'


@pytest.mark.parametrize('kind', ['resume', 'stop'])
async def test_replay_failure_rolls_back_all_effects_even_if_caller_commits(projection_database, kind):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    before = await admin.fetchval('SELECT product_seq FROM conversations')
    await admin.execute("ALTER TABLE product_events ADD CONSTRAINT reject_business CHECK (business_operation_key IS NULL)")
    async with sessions.begin() as db:
        with pytest.raises(IntegrityError):
            if kind == 'resume':
                await accept_resume(db, cmd, actor_user_id='u', authorize_resume=allow)
            else:
                await accept_stop(db, stop(), actor_user_id='u')
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0
    assert await admin.fetchval('SELECT product_seq FROM conversations') == before
    assert await admin.fetchval('SELECT status FROM tasks') == 'awaiting_permission'
    assert await admin.fetchval('SELECT last_decision_version FROM tasks') is None


async def test_stop_wins_without_agent_use_and_does_not_claim_completion(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    await admin.execute('UPDATE agents SET deleted_at=now()')
    results = await asyncio.gather(*(admit(sessions, stop()) for _ in range(2)))
    assert sorted(r.duplicate for r in results) == [False, True]
    with pytest.raises(CommandRejected):
        await admit(sessions, cmd)
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'
    assert await admin.fetchval('SELECT status FROM runtime_task_executions') == 'awaiting_interaction'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'


@pytest.mark.parametrize('mutation', ["UPDATE users SET status='disabled'", "UPDATE conversations SET active_product_task_id=NULL", "UPDATE runtime_sessions SET active_task_id=NULL", "UPDATE runtime_task_executions SET snapshot_id=NULL,state_version=2", "UPDATE task_projection_states SET last_runtime_seq=1", "UPDATE runtime_projection_cursors SET last_runtime_seq=1"])
async def test_current_authority_and_projection_required(projection_database, mutation):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    await admin.execute(mutation)
    with pytest.raises(CommandRejected):
        await admit(sessions, cmd)


@pytest.mark.parametrize('tombstone', ['hitl_abandoned', 'hitl_resume_exhausted'])
async def test_closed_request_cannot_be_decided(projection_database, tombstone):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    await admin.execute('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        SELECT 't','c',max(seq)+1,$1,$2::json FROM task_events''', tombstone,
        json.dumps({'decisionVersion':cmd.request_version}))
    with pytest.raises(CommandRejected):
        await admit(sessions, cmd)


async def test_partial_decisions_require_new_boundary_and_retain_original_version(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    await admit(sessions, cmd)
    await admin.execute("UPDATE runtime_commands SET status='completed'")
    with pytest.raises(CommandRejected):
        await admit(sessions, cmd.model_copy(update={'command_id':'too-early', 'payload':{**cmd.payload,
            'decision_version':'too-early', 'decisions':[{'tool_call_id':'b','approved':False}]}}))
    # Simulate the future Worker's next complete, immutable parked boundary.
    seq = await park(admin, [SECOND])
    await consume(sessions)
    for table in ('runtime_sessions', 'runtime_task_executions'):
        await admin.execute(f"UPDATE {table} SET snapshot_id=$1,state_version=2", str(seq))
    second = cmd.model_copy(update={'command_id':'second', 'payload':{**cmd.payload,
        'decision_version':'second', 'snapshot_id':str(seq), 'state_version':2,
        'decisions':[{'tool_call_id':'b','approved':False}]}})
    with pytest.raises(CommandRejected):
        await admit(sessions, second.model_copy(update={'payload':{**second.payload,
            'decisions':cmd.payload['decisions']+second.payload['decisions']}}))
    await admit(sessions, second)
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'") == 2
    state = json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['pending_interactions'][0]['awaiting_resume'] is True
    assert state['pending_interactions'][0]['pending_tool_call_ids'] == ['b']


async def test_revoked_authority_blocks_resume_but_not_owner_stop(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    async def deny(*args):
        return False
    with pytest.raises(CommandRejected):
        await admit(sessions, cmd, authorize=deny)
    for command in (cmd, stop()):
        with pytest.raises(CommandRejected):
            await admit(sessions, command, actor='someone_else')
    await admit(sessions, stop())


async def test_stop_resume_race_preserves_claimed_history(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    results = await asyncio.gather(admit(sessions, cmd), admit(sessions, stop()), return_exceptions=True)
    assert not isinstance(results[1], Exception)
    assert not isinstance(results[0], Exception) or isinstance(results[0], CommandRejected)
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='ResumeInteraction' AND status='accepted'") == 0
    assert (await admit(sessions, stop().model_copy(update={'command_id':'stop-again'}))).duplicate


async def test_stop_does_not_rewrite_claimed_resume(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    await admit(sessions, cmd)
    await admin.execute("UPDATE runtime_commands SET status='claimed',claimed_by='worker',claimed_epoch=1")
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=now()-interval '1 minute'")
    await admit(sessions, stop())
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'claimed'
    assert await admin.fetchval('SELECT owner_id FROM runtime_sessions') == 'worker'
    assert await admin.fetchval('SELECT execution_epoch FROM runtime_sessions') == 1


async def test_admission_guard_and_payload_contracts(projection_database):
    sessions, admin = projection_database
    cmd = await parked(sessions, admin)
    async with sessions() as db:
        with pytest.raises(CommandRejected):
            await accept_stop(db, stop(), actor_user_id='u')
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await accept_resume(db, stop(), actor_user_id='u', authorize_resume=allow)
        with pytest.raises(CommandRejected):
            await accept_stop(db, cmd, actor_user_id='u')
    with pytest.raises(ValueError):
        await admit(sessions, stop().model_copy(update={'payload':{'reason':'anything'}}))
    await admin.execute("UPDATE tasks SET status='completed'")
    with pytest.raises(CommandRejected):
        await admit(sessions, stop())


async def test_missing_conversation_is_rejected(projection_database):
    sessions, _ = projection_database
    with pytest.raises(CommandRejected):
        await admit(sessions, stop().model_copy(update={'conversation_id':'missing'}))


async def test_stop_resolves_current_epoch_and_reply_despite_lagging_product(projection_database):
    sessions, admin = projection_database
    await parked(sessions, admin)
    await admin.execute("UPDATE tasks SET run_id=NULL,status='running'")
    for table in ('runtime_sessions', 'runtime_task_executions'):
        await admin.execute(f"UPDATE {table} SET execution_epoch=2")
    await admit(sessions, stop().model_copy(update={'reply_id':None,'expected_epoch':0}))
    row = await admin.fetchrow('SELECT expected_epoch,reply_id FROM runtime_commands')
    assert row['expected_epoch'] == 2 and row['reply_id'] == 'reply'
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'


async def test_stop_before_start_claim_rejects_unclaimed_command(projection_database):
    from workmate_runtime.transport.postgres import ExecutionStore
    sessions, admin = projection_database
    await admin.execute("UPDATE runtime_sessions SET active_task_id=NULL,execution_epoch=0,status='accepted',reply_id=NULL")
    await admin.execute("UPDATE runtime_task_executions SET execution_epoch=0,status='accepted',reply_id=NULL")
    await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('0',64))""")
    await admit(sessions, stop())
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='start'") == 'rejected'
    assert await ExecutionStore(admin).claim_start('start','worker','node',lease_seconds=30) is None
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None
