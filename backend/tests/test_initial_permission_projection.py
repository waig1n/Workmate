"""Initial Full Access activation requires the actual Task sandbox proof."""
import asyncio
import json
import pytest
from test_durable_projection import projection_database,emit,consume
from workmate_product.application.execution_context import ExecutionSpec,SelectedModel,_digest
from workmate_product.projections.store import ProjectionConflict


async def seed_permission(admin,*,proof=True):
    await admin.execute("""UPDATE tasks SET approval_profile='full_access';
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,
            status,expected_epoch,payload,content_digest,claimed_by,claimed_epoch,claimed_at)
        VALUES ('start','s','c','t','u','StartTask','claimed',0,'{}',repeat('d',64),'worker',1,clock_timestamp());""")
    spec=ExecutionSpec(task_id='t',session_id='s',conversation_id='c',actor_user_id='u',agent_id='a',
        approval_profile='full_access',tool_policy={},platform_tool_profile='none',capability_ids=(),
        knowledge_base_ids=(),selected_model=SelectedModel(profile_name='test',model_type='openai',
            model='test',api_key_env='TEST_KEY'))
    payload=spec.model_dump(mode='json')
    await admin.execute("INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest) VALUES ('t','s',1,$1::jsonb,$2)",json.dumps(payload),_digest(payload))
    if proof:
        await admin.execute("""INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
            VALUES ('t','s','worker',1,'workspace')""")
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})


async def test_initial_permission_activation_is_once_and_not_repeated_on_resume(projection_database):
    sessions,admin=projection_database
    await seed_permission(admin)
    await asyncio.gather(consume(sessions),consume(sessions))
    await emit(admin,2,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='permission_mode_changed'")==1
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'")==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_resumed'")==1


@pytest.mark.parametrize('mutation',[
    None,
    "UPDATE runtime_commands SET claimed_by='different'",
    "UPDATE runtime_commands SET claimed_epoch=2",
    "UPDATE runtime_commands SET expected_epoch=1",
])
async def test_initial_activation_without_matching_proof_rolls_back(projection_database,mutation):
    sessions,admin=projection_database
    await seed_permission(admin,proof=mutation is not None)
    if mutation: await admin.execute(mutation)
    with pytest.raises(ProjectionConflict): await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM task_events')==0
    assert await admin.fetchval('SELECT count(*) FROM security_audit_events')==0
    assert await admin.fetchval('SELECT run_id FROM tasks') is None


async def test_delayed_projection_retains_historical_proof_after_owner_uncertainty(projection_database):
    sessions,admin=projection_database
    await seed_permission(admin)
    await admin.execute("UPDATE runtime_commands SET status='indeterminate'")
    await consume(sessions)
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'")==1
