"""Restart observes proven no-Reply failure without replaying preparation."""
import pytest
from test_durable_projection import projection_database
from test_sandbox_failure_projection import seed_failure
from test_unstarted_projection import consume
from workmate_runtime.execution.reconciliation import inspect_sessions
from workmate_product.application.delivery import close_delivery


async def inspect(admin):
    return (await inspect_sessions(admin,worker_id='restarted-worker',storage_node_id='node'))[0]


async def test_failed_preparation_restart_is_delivery_pending_even_after_lease_expires(projection_database):
    sessions,admin=projection_database
    await seed_failure(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 hour'")
    before=await admin.fetchrow('SELECT * FROM runtime_sessions')
    assert (await inspect(admin)).category=='terminal_delivery_pending'
    assert dict(await admin.fetchrow('SELECT * FROM runtime_sessions'))==dict(before)
    await consume(sessions)
    assert (await inspect(admin)).category=='terminal_delivery_pending'
    async with sessions.begin() as db:
        await close_delivery(db,'c','t')
    assert (await inspect(admin)).category=='idle'


@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_events SET payload=payload||'{\"worker_id\":\"other\"}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"stop_command_id\":null}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"state_version\":false}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"resources_quiescent\":false}'::jsonb",
    "UPDATE runtime_events SET payload=payload||'{\"reason\":\"unknown\"}'::jsonb",
    "UPDATE runtime_commands SET claimed_epoch=2",
    "UPDATE runtime_commands SET actor_user_id='different'",
    "UPDATE tasks SET status='running'",
    "INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload) VALUES ('other','s',2,1,'t','{}')",
    "INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,status,expected_epoch,payload,content_digest) VALUES ('other','s','c','t','u','StopTask','rejected',1,'{}',repeat('d',64))",
])
async def test_restart_rejects_forged_failed_preparation_receipts(projection_database,mutation):
    _,admin=projection_database
    await seed_failure(admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '1 hour'")
    if "actor_user_id" in mutation:
        await admin.execute("INSERT INTO users(id,email,normalized_email,password_hash,display_name) VALUES ('different','different@example.invalid','different@example.invalid','fixture','Fixture')")
    await admin.execute(mutation)
    assert (await inspect(admin)).category=='blocked_inconsistent'
