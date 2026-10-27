"""Explicit visible failure retains Runtime evidence and blocked admission."""
import asyncio
import json
import pytest
from sqlalchemy import select
from test_durable_projection import projection_database,emit,consume
from workmate_product.persistence.models import Conversation, Task, User
from workmate_product.application.execution_quarantine import quarantine_execution,QuarantineConflict


async def seed(admin):
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker'; UPDATE tasks SET status='running',run_id='reply'")


async def quarantine(sessions,**changes):
    args=dict(conversation_id='c',task_id='t',session_id='s',worker_id='worker',execution_epoch=1,
        storage_node_id='node',error_type='ValueError',cause_type=None)
    args.update(changes)
    async with sessions.begin() as db:
        return await quarantine_execution(db,**args)


async def test_quarantine_is_idempotent_and_preserves_runtime_gates(projection_database):
    sessions,admin=projection_database
    await seed(admin)
    before=dict(await admin.fetchrow('SELECT * FROM runtime_sessions'))
    assert sorted(await asyncio.gather(quarantine(sessions),quarantine(sessions)))==[False,True]
    assert await admin.fetchval('SELECT status FROM tasks')=='failed'
    error=json.loads(await admin.fetchval('SELECT error FROM tasks'))
    assert error['code']=='runtime_execution_quarantined'
    assert error['context']['resumable'] is False
    assert dict(await admin.fetchrow('SELECT * FROM runtime_sessions'))==before
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_ERROR'")==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='execution_quarantined'")==1
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots')==0


@pytest.mark.parametrize('changes',[{'worker_id':'other'},{'execution_epoch':2},{'storage_node_id':'other'},
    {'session_id':'other'},{'conversation_id':'other'}])
async def test_quarantine_requires_exact_execution_identity(projection_database,changes):
    sessions,admin=projection_database
    await seed(admin)
    with pytest.raises(QuarantineConflict): await quarantine(sessions,**changes)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT count(*) FROM product_events')==0


async def test_quarantine_rejects_already_terminal(projection_database):
    sessions,admin=projection_database
    await seed(admin)
    await admin.execute("UPDATE tasks SET status='completed'")
    with pytest.raises(QuarantineConflict): await quarantine(sessions)


async def test_late_projection_does_not_revive_quarantined_task(projection_database):
    from app.adapter.durable_projection import ProjectionDeferred
    sessions,admin=projection_database
    await seed(admin)
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await quarantine(sessions)
    with pytest.raises(ProjectionDeferred): await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='failed'
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0


async def test_conversation_becomes_read_only_and_admission_rejects(projection_database):
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from app.api.routers.conversations import _conversation_out
    from app.services.execution_admission import TaskAdmission
    from workmate_product.application.execution_quarantine import READ_ONLY_REASON
    sessions,admin=projection_database
    await seed(admin)
    await quarantine(sessions)
    async with sessions() as db:
        conv=await db.get(Conversation,'c')
        user=await db.get(User,'u')
        factory=AsyncMock()
        factory.can_use.return_value=False
        view=await _conversation_out(conv,user,db,None,factory)
        assert view.read_only is True and view.read_only_reason==READ_ONLY_REASON
        service=TaskAdmission(sessions,storage_node_id='node')
        with pytest.raises(HTTPException) as error:
            await service.create_task(db,user,conv,'must not queue')
        assert error.value.status_code==409
    assert await admin.fetchval('SELECT count(*) FROM tasks')==1


async def test_prior_history_survives_quarantine_and_tail_is_frozen(projection_database):
    from app.adapter.durable_projection import ProjectionDeferred
    from workmate_product.persistence.models import TaskMessage
    sessions,admin=projection_database
    await seed(admin)
    async with sessions.begin() as db:
        db.add(TaskMessage(id='history',task_id='t',conversation_id='c',role='assistant',content={'text':'already projected'}))
    await quarantine(sessions)
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    with pytest.raises(ProjectionDeferred): await consume(sessions)
    async with sessions() as db:
        assert (await db.get(TaskMessage,'history')).content=={'text':'already projected'}
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'


@pytest.mark.parametrize('sql',[
    'UPDATE conversations SET active_product_task_id=NULL',
    'UPDATE runtime_sessions SET active_task_id=NULL',
    "UPDATE runtime_sessions SET status='completed'; UPDATE runtime_task_executions SET status='completed'",
])
async def test_quarantine_requires_both_gates_and_uncertain_active_runtime(projection_database,sql):
    sessions,admin=projection_database
    await seed(admin)
    await admin.execute(sql)
    with pytest.raises(QuarantineConflict): await quarantine(sessions)
    assert await admin.fetchval('SELECT count(*) FROM product_events')==0


async def test_actual_display_snapshot_is_retained_after_quarantine(projection_database):
    from app.adapter.durable_projection import ProjectionDeferred
    from test_durable_lifecycle import saved_display
    sessions,admin=projection_database
    await seed(admin)
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await saved_display(sessions,admin,body='已保存的部分回答',phase='partial')
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker'")
    before=await admin.fetchval('SELECT content FROM task_messages')
    await quarantine(sessions)
    await emit(admin,2,{'type':'TOOL_CALL_START','tool_call_id':'late','tool_call_name':'must remain unprojected'})
    with pytest.raises(ProjectionDeferred): await consume(sessions)
    assert await admin.fetchval('SELECT content FROM task_messages')==before
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='TEXT_MESSAGE_CONTENT'")==0
    assert await admin.fetchval('SELECT status FROM tasks')=='failed'


async def test_expired_owner_reports_failure_after_restart_without_replaying_or_releasing(projection_database):
    from workmate_product.application.execution_quarantine import report_expired_executions
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 minute'")
    before = dict(await admin.fetchrow('SELECT * FROM runtime_sessions'))
    commands = [dict(row) for row in await admin.fetch('SELECT * FROM runtime_commands')]
    assert await report_expired_executions(sessions, limit=10) == 1
    assert await report_expired_executions(sessions, limit=10) == 0
    assert await admin.fetchval('SELECT status FROM tasks') == 'failed'
    assert dict(await admin.fetchrow('SELECT * FROM runtime_sessions')) == before
    assert [dict(row) for row in await admin.fetch('SELECT * FROM runtime_commands')] == commands
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    error=json.loads(await admin.fetchval('SELECT error FROM tasks'))
    assert error['context']['error_type'] == 'ExecutionOwnerExpired'
    assert error['context']['resources_quiescent'] is None


@pytest.mark.parametrize('condition', ['live', 'runtime_completed', 'released'])
async def test_expiry_reporting_does_not_replace_live_terminal_or_released_boundaries(projection_database, condition):
    from workmate_product.application.execution_quarantine import report_expired_executions
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 minute'")
    if condition == 'live':
        await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '1 hour'")
    elif condition == 'runtime_completed':
        await admin.execute("UPDATE runtime_sessions SET status='completed'; UPDATE runtime_task_executions SET status='completed'")
    else:
        await admin.execute("UPDATE runtime_sessions SET owner_id=NULL,lease_until=NULL,status='awaiting_interaction'; UPDATE runtime_task_executions SET status='awaiting_interaction'; UPDATE tasks SET status='awaiting_permission'")
    assert await report_expired_executions(sessions, limit=10) == 0
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='execution_quarantined'") == 0


async def test_expiry_is_rechecked_under_execution_locks(projection_database):
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '1 hour'")
    assert await quarantine(sessions, require_expired=True) is False
    assert await admin.fetchval('SELECT status FROM tasks') == 'running'


@pytest.mark.parametrize('has_snapshot', [False, True])
async def test_quarantined_cold_view_exposes_failed_task_partial_body_and_no_refresh_wait(projection_database, has_snapshot):
    from app.services.conversation_business_state import ConversationBusinessState
    from test_durable_lifecycle import saved_display
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '1 hour'")
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    if has_snapshot:
        await saved_display(sessions,admin,body='保留下来的回答',phase='partial')
        await admin.execute("UPDATE runtime_sessions SET owner_id='worker'")
    # Failure reporting while lease is still live also cannot wait for a future
    # model checkpoint: that controller already stopped and owns no continuation.
    await quarantine(sessions)
    view = await ConversationBusinessState(sessions,'u').read_view('c')
    assert view.business_state['tasks'][0]['status'] == 'failed'
    assert view.active.refresh_possible is False
    assert view.business_state['pending_confirm'] is None
    if has_snapshot:
        assert view.snapshots[0].incomplete is True
        assert view.snapshots[0].phase == 'partial'
        assert view.snapshots[0].blocks[0].text == '保留下来的回答'
    else:
        assert view.snapshots == ()


async def test_quarantined_final_display_remains_valid_for_cold_reset(projection_database):
    from app.services.conversation_business_state import ConversationBusinessState
    from workmate_contracts.realtime import MessageSnapshot
    from workmate_product.application.execution_quarantine import report_expired_executions
    from test_durable_lifecycle import saved_display
    sessions, admin = projection_database
    await seed(admin)
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await saved_display(sessions,admin,body='完整保存的回答',phase='final')
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=clock_timestamp()-interval '1 minute'")
    before=await admin.fetchval('SELECT content FROM task_messages')
    assert await report_expired_executions(sessions)==1
    view=await ConversationBusinessState(sessions,'u').read_view('c')
    # Validate the serialized payload exactly as a new reader does; model_copy
    # itself deliberately does not run Pydantic validators.
    cut=MessageSnapshot.model_validate(view.snapshots[0].model_dump(mode='json'))
    assert cut.phase=='final' and cut.incomplete is False
    assert cut.blocks[0].text=='完整保存的回答'
    assert view.business_state['tasks'][0]['status']=='failed'
    assert view.active.refresh_possible is False
    assert await admin.fetchval('SELECT content FROM task_messages')==before


async def test_expiry_scan_skips_locked_candidate_and_preserves_execution_fences(projection_database):
    from sqlalchemy import text
    from workmate_product.application.execution_quarantine import report_expired_executions
    sessions, admin = projection_database
    await seed(admin)
    async with sessions.begin() as db:
        db.add(Conversation(id='c2', user_id='u', agent_id='a'))
        await db.flush()
        db.add(Task(id='t2', conversation_id='c2', title='Second', status='running', run_id='reply2'))
    await admin.execute("""
        UPDATE conversations SET active_product_task_id='t2' WHERE id='c2';
        UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '2 minutes';
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id,active_task_id,
            execution_epoch,status,reply_id,owner_id,lease_until)
        VALUES ('s2','c2','node','t2',2,'running','reply2','worker2',clock_timestamp()-interval '1 minute');
        INSERT INTO runtime_task_executions(task_id,session_id,execution_epoch,status,reply_id)
        VALUES ('t2','s2',2,'running','reply2');
    """)
    before = [dict(row) for row in await admin.fetch('SELECT * FROM runtime_sessions ORDER BY id')]
    async with sessions.begin() as blocker:
        await blocker.execute(text("SELECT id FROM runtime_sessions WHERE id='s' FOR UPDATE"))
        # This deadline detects the old unbounded lock wait without leaving a
        # hanging test; it is deliberately wider than the one-second lock budget.
        assert await asyncio.wait_for(report_expired_executions(sessions), timeout=5) == 1
        assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'running'
        assert await admin.fetchval("SELECT status FROM tasks WHERE id='t2'") == 'failed'
    assert await report_expired_executions(sessions) == 1
    assert await report_expired_executions(sessions) == 0
    assert [dict(row) for row in await admin.fetch('SELECT * FROM runtime_sessions ORDER BY id')] == before
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='execution_quarantined'") == 2
    async with sessions.begin() as db:
        assert await db.scalar(text('SHOW lock_timeout')) == '0'


@pytest.mark.parametrize('sql,sqlstate', [('SELECT 1/0', '22012'),
    ('SELECT * FROM nonexistent_quarantine_fixture_table', '42P01')])
async def test_expiry_scan_does_not_hide_other_database_errors(projection_database, monkeypatch, sql, sqlstate):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from workmate_product.application import execution_quarantine as service
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 minute'")

    async def broken(db, **kwargs):
        await db.execute(text(sql))

    monkeypatch.setattr(service, 'quarantine_execution', broken)
    with pytest.raises(DBAPIError) as raised:
        await service.report_expired_executions(sessions)
    assert raised.value.orig.sqlstate == sqlstate
    assert await admin.fetchval('SELECT status FROM tasks') == 'running'
    async with sessions.begin() as db:
        assert await db.scalar(text('SELECT 42')) == 42
