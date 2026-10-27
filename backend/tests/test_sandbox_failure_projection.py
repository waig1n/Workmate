"""Sandbox failure never invents a Reply or publishes Full Access activation."""
import asyncio
import json
import pytest
from test_durable_projection import projection_database
from test_unstarted_projection import consume
from workmate_product.application.delivery import close_delivery
from workmate_product.projections.store import ProjectionConflict


async def seed_failure(admin, reason='sandbox_verification_failed'):
    await admin.execute("""UPDATE runtime_sessions SET owner_id='worker',reply_id=NULL,status='failed',runtime_seq=1;
        UPDATE runtime_task_executions SET reply_id=NULL,status='failed';
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,
            status,expected_epoch,payload,content_digest,claimed_by,claimed_epoch,claimed_at)
        VALUES ('start','s','c','t','u','StartTask','completed',0,'{}',repeat('d',64),'worker',1,clock_timestamp());""")
    payload={'type':'EXECUTION_NOT_STARTED','session_id':'s','reply_id':None,'status':'failed',
        'reason':reason,'start_command_id':'start','worker_id':'worker',
        'preparation':'joined','resources_quiescent':True,'state_version':0,'input_snapshot_id':None}
    await admin.execute("""INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
        VALUES ('failure','s',1,1,'t',$1::jsonb)""",json.dumps(payload))


@pytest.mark.parametrize('reason',['sandbox_verification_failed','model_unavailable'])
async def test_failure_projects_once_then_closes_empty_delivery(projection_database,reason):
    sessions,admin=projection_database
    await seed_failure(admin,reason)
    await asyncio.gather(consume(sessions),consume(sessions))
    assert await admin.fetchval('SELECT status FROM tasks')=='failed'
    assert json.loads(await admin.fetchval('SELECT error::text FROM tasks'))['code']==reason
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_ERROR'")==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='task_failed'")==1
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==0
    assert await admin.fetchval('SELECT run_id FROM tasks') is None
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    async with sessions.begin() as db:
        assert await close_delivery(db,'c','t')==()
    async with sessions.begin() as db:
        assert await close_delivery(db,'c','t')==()
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None


@pytest.mark.parametrize('reason',['sandbox_verification_failed','model_unavailable'])
@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_events SET payload=payload||'{\"worker_id\":\"other\"}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"stop_command_id\":\"stop\"}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"resources_quiescent\":false}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"reason\":\"different\"}'::jsonb",
    "UPDATE runtime_commands SET status='claimed'",
    "UPDATE runtime_sessions SET status='preparing'",
    "UPDATE conversations SET active_product_task_id=NULL",
    "UPDATE tasks SET status='cancelling'",
])
async def test_failure_requires_exact_completed_preparation_proof(projection_database,mutation,reason):
    sessions,admin=projection_database
    await seed_failure(admin,reason)
    await admin.execute(mutation)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM product_events')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0
