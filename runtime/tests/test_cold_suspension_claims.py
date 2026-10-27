"""Released permission snapshots can change owner exactly once without replay."""
import asyncio
import pytest
from test_suspension_store import suspended_database, begin, receipt
from test_postgres_execution_store import database
from test_resume_controller import submit, allow
from workmate_runtime.transport.controls import ResumeStore
from workmate_runtime.transport.suspension import SuspensionStore
from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership


async def released(db,result):
    claim = await begin(db,result)
    await SuspensionStore(db).release(claim,receipt(claim))
    await submit(db,result.reply_id)
    return claim


async def test_competing_cold_resume_claims_keep_immutable_source(suspended_database):
    pool, db, result = suspended_database
    source = await released(db,result)
    async def claim(worker):
        async with pool.acquire() as conn:
            return await ResumeStore(conn).claim_resume('resume',worker,'node',authorize_resume=allow)
    claims = await asyncio.gather(*(claim(f'new-{i}') for i in range(5)))
    winner = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    assert winner.owner.execution_epoch == source.owner.execution_epoch+1
    assert winner.boundary.snapshot_id == source.snapshot_id
    assert winner.reply_id == result.reply_id
    assert await db.fetchval('SELECT execution_epoch FROM runtime_snapshots WHERE id=$1',source.snapshot_id) == source.owner.execution_epoch
    assert await db.fetchval("SELECT claimed_epoch FROM runtime_commands WHERE id='resume'") == winner.owner.execution_epoch
    with pytest.raises(LostOwnership):
        await ExecutionStore(db).renew(source.owner,lease_seconds=30)
    await ResumeStore(db).begin_resume(winner,'node',authorize_resume=allow,event={
        'type':'REPLY_START','reply_id':result.reply_id,'session_id':source.owner.session_id})


async def test_cold_resume_denied_authority_rolls_back_owner_and_bridge(suspended_database):
    _,db,result=suspended_database
    await released(db,result)
    async def deny(*args):
        return False
    assert await ResumeStore(db).claim_resume('resume','new','node',authorize_resume=deny) is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None
    assert await db.fetchval('SELECT resume_epoch FROM runtime_suspensions') is None


async def test_cold_resume_wrong_storage_node_cannot_claim(suspended_database):
    _,db,result=suspended_database
    await released(db,result)
    assert await ResumeStore(db).claim_resume('resume','new','other',authorize_resume=allow) is None


async def test_cold_stop_interrupts_same_reply_without_executing_tools(suspended_database):
    from test_parked_stop import stop_intent, quiescent
    from test_resume_controller import prepare_resume
    from test_core_engine import ConfirmTool
    from workmate_runtime.execution.stopping import ParkedStopController
    pool,db,result=suspended_database
    source = await begin(db,result)
    await SuspensionStore(db).release(source,receipt(source))
    await stop_intent(db)  # Stop's original observed epoch may legitimately be stale.
    tool=ConfirmTool()
    outcome=await ParkedStopController(pool).stop('stop','replacement','node',prepare_resume(tool),verify_cleanup=quiescent)
    assert outcome and outcome.status == 'interrupted'
    assert outcome.reply_id == result.reply_id and outcome.owner.execution_epoch == result.owner.execution_epoch+1
    assert tool.calls == 0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'") == 'completed'


@pytest.mark.parametrize('field,value', [('snapshot_digest','0'*64),('source_worker_id','forged'),('status','blocked')])
async def test_cold_resume_rejects_corrupt_release_evidence(suspended_database,field,value):
    _,db,result=suspended_database
    await released(db,result)
    if field=='status':
        await db.execute("UPDATE runtime_suspensions SET status='blocked',cleanup_receipt=NULL,released_at=NULL")
    else:
        await db.execute(f'UPDATE runtime_suspensions SET {field}=$1',value)
    assert await ResumeStore(db).claim_resume('resume','new','node',authorize_resume=allow) is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None


async def test_full_access_cold_claim_requires_source_grant_and_begin_requires_new_sandbox(suspended_database):
    import json
    _,db,result=suspended_database
    source=await released(db,result)
    await db.execute('''        CREATE TABLE runtime_execution_specs(task_id text,payload jsonb);
        INSERT INTO runtime_execution_specs VALUES ('task','{"approval_profile":"standard"}');
        INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id,isolation,containment,instance_id,evidence) VALUES ('task','session','worker',1,'workspace','bubblewrap','pid_namespace','','{}');
        UPDATE runtime_commands SET payload=payload || '{"full_access":true}'::jsonb WHERE id='resume';''')
    assert await ResumeStore(db).claim_resume('resume','new','node',authorize_resume=allow) is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None
    await db.execute("INSERT INTO runtime_permission_upgrades(task_id,session_id,command_id,permission_version,state_version,worker_id,execution_epoch,actor_user_id,initial_profile) VALUES ('task','session','resume','resume',$1,'worker',1,'user','standard')",source.state_version)
    events=await db.fetch("SELECT payload FROM task_events WHERE event_type='hitl_decision'")
    event=json.loads(events[0]['payload']);event.update(scope='task',permissionMode='bypass')
    await db.execute("UPDATE task_events SET payload=$1::json WHERE event_type='hitl_decision'",json.dumps(event))
    claim=await ResumeStore(db).claim_resume('resume','new','node',authorize_resume=allow)
    assert claim is not None and claim.owner.execution_epoch==2
    start={'type':'REPLY_START','session_id':'session','reply_id':result.reply_id}
    with pytest.raises(LostOwnership):
        await ResumeStore(db).begin_resume(claim,'node',authorize_resume=allow,event=start)
    await db.execute("INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id,isolation,containment,instance_id,evidence) VALUES ('task','session','new',2,'workspace','bubblewrap','pid_namespace','','{}')")
    await ResumeStore(db).begin_resume(claim,'node',authorize_resume=allow,event=start)


@pytest.mark.parametrize('mutation', ["UPDATE runtime_commands SET status='accepted' WHERE id='resume'",
    "UPDATE runtime_commands SET claimed_by='forged' WHERE id='resume'",
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
    "UPDATE runtime_sessions SET status='completed'"])
async def test_recovery_bridge_requires_live_claim_evidence(suspended_database,mutation):
    from workmate_runtime.transport.suspension import read_recovery_bridge
    _,db,result=suspended_database
    await released(db,result)
    claim=await ResumeStore(db).claim_resume('resume','new','node',authorize_resume=allow)
    assert await read_recovery_bridge(db,claim.owner) is not None
    await db.execute(mutation)
    assert await read_recovery_bridge(db,claim.owner) is None


async def test_partial_approval_can_suspend_again_and_resume_on_third_owner(database):
    from test_resume_controller import parked, prepare_resume
    from test_core_engine import ConfirmTool
    from agentscope.message import TextBlock
    from workmate_runtime.execution.controller import ExecutionController
    pool,db=database
    first=await parked(pool,db,partial=True)
    await released(db,first)
    tool=ConfirmTool()
    partial=await ExecutionController(pool).resume('resume','second','node',prepare_resume(tool),authorize_resume=allow)
    assert partial and partial.status=='awaiting_interaction' and tool.calls==1
    assert partial.reply_id==first.reply_id and partial.owner.execution_epoch==2
    second_pause=await begin(db,partial)
    assert second_pause is not None
    await SuspensionStore(db).release(second_pause,receipt(second_pause))
    await submit(db,partial.reply_id,cid='resume2',call='b')
    await db.execute("UPDATE runtime_commands SET expected_epoch=2 WHERE id='resume2'")
    done=await ExecutionController(pool).resume('resume2','third','node',prepare_resume(tool,[[TextBlock(text='done')]]),authorize_resume=allow)
    assert done and done.status=='completed' and done.reply_id==first.reply_id
    assert done.owner.execution_epoch==3 and tool.calls==2
    assert await db.fetchval('SELECT count(*) FROM runtime_suspensions WHERE resume_epoch IS NOT NULL')==2


async def test_claimed_recovery_is_not_automatically_replayed_after_owner_loss(suspended_database):
    _,db,result=suspended_database
    await released(db,result)
    first=await ResumeStore(db).claim_resume('resume','second','node',authorize_resume=allow)
    assert first is not None
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    assert await ResumeStore(db).claim_resume('resume','third','node',authorize_resume=allow) is None
    assert await db.fetchval('SELECT execution_epoch FROM runtime_sessions')==2
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions')=='second'




@pytest.mark.parametrize('permission_source',['initial','previous_upgrade'])
async def test_existing_full_access_cold_begin_needs_fresh_sandbox_without_upgrade_flag(suspended_database,permission_source):
    _,db,result=suspended_database
    if permission_source=='initial':
        await db.execute("UPDATE tasks SET approval_profile='full_access' WHERE id='task'")
    else:
        await db.execute('''INSERT INTO runtime_permission_upgrades(task_id,session_id,command_id,
            permission_version,state_version,actor_user_id,worker_id,execution_epoch,initial_profile)
            VALUES ('task','session','command','prior',1,'user','worker',1,'standard')''')
    await released(db,result)
    claim=await ResumeStore(db).claim_resume('resume','new','node',authorize_resume=allow)
    assert claim and claim.payload.full_access is False
    event={'type':'REPLY_START','session_id':'session','reply_id':result.reply_id}
    with pytest.raises(LostOwnership):
        await ResumeStore(db).begin_resume(claim,'node',authorize_resume=allow,event=event)
    await db.execute('''INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
        VALUES ('task','session','new',2,'workspace')''')
    await ResumeStore(db).begin_resume(claim,'node',authorize_resume=allow,event=event)


async def test_cold_stop_rejects_future_observed_epoch(suspended_database):
    from test_parked_stop import stop_intent
    from workmate_runtime.transport.stopping import StopStore
    _,db,result=suspended_database
    source=await begin(db,result)
    await SuspensionStore(db).release(source,receipt(source))
    await stop_intent(db)
    await db.execute("UPDATE runtime_commands SET expected_epoch=99 WHERE id='stop'")
    assert await StopStore(db).claim_parked('stop','new','node') is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None
