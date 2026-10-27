"""No-Reply Stop uses durable commands, no synthetic execution metrics."""
import asyncio
import json

import pytest
from app.adapter.durable_projection import live_projector
from workmate_product.projections.store import consume_next, ProjectionConflict
from test_durable_projection import projection_database  # noqa: F401


async def seed(admin):
    await admin.execute("""UPDATE tasks SET status='cancelling';
        UPDATE runtime_sessions SET reply_id=NULL,status='cancelled',runtime_seq=1;
        UPDATE runtime_task_executions SET reply_id=NULL,status='cancelled';
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,
            status,expected_epoch,payload,content_digest,claimed_by,claimed_epoch,claimed_at)
        VALUES ('start','s','c','t','u','StartTask','rejected',0,'{}',repeat('d',64),NULL,NULL,NULL),
            ('stop','s','c','t','u','StopTask','completed',0,'{}',repeat('e',64),'worker',1,clock_timestamp());""")
    payload={'type':'EXECUTION_NOT_STARTED','session_id':'s','reply_id':None,'status':'cancelled',
        'stop_command_id':'stop','start_command_id':'start','preparation':'unclaimed',
        'resources_quiescent':True,'state_version':0,'input_snapshot_id':None}
    await admin.execute("""INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
        VALUES ('event','s',1,1,'t',$1::jsonb)""",json.dumps(payload))


async def consume(sessions):
    async with sessions.begin() as db:
        return await consume_next(db,'c',project=live_projector())


async def test_unstarted_stop_projects_once_without_reply_metrics_or_gate_release(projection_database):
    sessions,admin=projection_database
    await seed(admin)
    await asyncio.gather(consume(sessions),consume(sessions))
    task=await admin.fetchrow('SELECT * FROM tasks')
    assert task['status']=='cancelled' and task['run_id'] is None
    assert task['duration_ms'] is None and task['token_usage'] is None
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==0
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='task_cancelled'")==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type IN ('run_started','run_finished','run_metrics')")==0
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_CANCELLED'")==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    assert json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))['terminal_control_event_id']=='event'


@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_commands SET status='accepted' WHERE id='stop'",
    "UPDATE runtime_commands SET claimed_epoch=2 WHERE id='stop'",
    "UPDATE runtime_commands SET status='claimed',claimed_by='worker' WHERE id='start'",
    "UPDATE runtime_task_executions SET status='preparing'",
    "UPDATE runtime_events SET payload=jsonb_set(payload,'{resources_quiescent}','false')",
    "UPDATE runtime_events SET payload=jsonb_set(payload,'{type}','\"TEXT_BLOCK_DELTA\"')",
    "UPDATE tasks SET run_id='unexpected'",
])
async def test_missing_control_proof_rolls_back_cursor_and_product(projection_database,mutation):
    sessions,admin=projection_database
    await seed(admin)
    await admin.execute(mutation)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM product_events')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0
    assert await admin.fetchval('SELECT status FROM tasks')=='cancelling'


async def test_joined_preparation_command_history_is_valid_without_core_reply(projection_database):
    sessions,admin=projection_database
    await seed(admin)
    await admin.execute("""UPDATE runtime_commands SET status='completed',claimed_by='worker',
        claimed_epoch=1,claimed_at=clock_timestamp() WHERE id='start';
        UPDATE runtime_commands SET expected_epoch=1 WHERE id='stop';
        UPDATE runtime_events SET payload=jsonb_set(payload,'{preparation}','\"joined\"');""")
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='cancelled'


@pytest.mark.parametrize('actor', ['u', 'manager'])
async def test_account_disable_stop_preserves_reason_and_actual_administrator(projection_database, actor):
    from workmate_product.persistence.models import User
    sessions, admin = projection_database
    async with sessions.begin() as db:
        db.add(User(id='manager', email='manager@example.invalid', password_hash='x', display_name='Manager'))
    await seed(admin)
    await admin.execute("UPDATE runtime_commands SET actor_user_id=$1,payload=$2::jsonb WHERE id='stop'",
        actor, json.dumps({'reason': 'account_disabled'}))
    receipt = {'commandId': 'stop', 'actorUserId': actor, 'targetUserId': 'u', 'reason': 'account_disabled'}
    await admin.execute("""INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        VALUES ('t','c',1,'task_cancel_requested',$1::json)""", json.dumps(receipt))
    await consume(sessions)
    cancelled = json.loads(await admin.fetchval("SELECT payload FROM task_events WHERE event_type='task_cancelled'"))
    assert cancelled['reason'] == 'account_disabled'
    completed = json.loads(await admin.fetchval("""SELECT payload FROM product_events
        WHERE payload->>'name'='TASK_COMPLETED'"""))
    assert completed['value']['reason'] == 'account_disabled'
    assert await admin.fetchval("SELECT actor_user_id FROM runtime_commands WHERE id='stop'") == actor
    from workmate_product.application.delivery import close_delivery
    async with sessions.begin() as db:
        assert await close_delivery(db, 'c', 't') == ()
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None


@pytest.mark.parametrize('field', ['commandId', 'actorUserId', 'targetUserId', 'reason'])
async def test_account_disable_stop_requires_exact_admission_receipt(projection_database, field):
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_commands SET payload=$1::jsonb WHERE id='stop'",
                        json.dumps({'reason': 'account_disabled'}))
    receipt = {'commandId': 'stop', 'actorUserId': 'u', 'targetUserId': 'u', 'reason': 'account_disabled'}
    receipt[field] = 'wrong'
    await admin.execute("""INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        VALUES ('t','c',1,'task_cancel_requested',$1::json)""", json.dumps(receipt))
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors') == 0


@pytest.mark.parametrize('payload', [{'reason': 'unknown'}, {'reason': 'account_disabled'}, {'unexpected': True}])
async def test_unstarted_rejects_invalid_or_unproven_stop_reason(projection_database, payload):
    sessions, admin = projection_database
    await seed(admin)
    await admin.execute("UPDATE runtime_commands SET payload=$1::jsonb WHERE id='stop'", json.dumps(payload))
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'
