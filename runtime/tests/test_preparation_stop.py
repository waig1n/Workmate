"""Unstarted Stop is real control evidence, never a fabricated Core Reply."""
import asyncio
import json

import pytest

from test_postgres_execution_store import database, claim  # noqa: F401
from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership
from workmate_runtime.transport.stopping_preparation import PreparationStopStore


async def stop(admin):
    await admin.execute("""UPDATE tasks SET status='cancelling' WHERE id='task';
        UPDATE runtime_commands SET status='rejected' WHERE kind='StartTask' AND status='accepted';
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
        SELECT 'stop','session','conversation','task','user','StopTask',execution_epoch,'{}',repeat('e',64)
        FROM runtime_sessions;""")


@pytest.mark.asyncio
async def test_unclaimed_stop_competes_once_without_reply_or_snapshot(database):
    pool,admin=database
    await stop(admin)
    async def settle(worker):
        async with pool.acquire() as db:
            store=PreparationStopStore(db)
            owned=await store.claim('stop',worker,'node',lease_seconds=30)
            if owned:
                await store.finish(owned,'node',preparation_joined=True,resources_quiescent=True)
            return owned
    winners=[c for c in await asyncio.gather(*(settle(f'w{i}') for i in range(6))) if c]
    assert len(winners)==1
    assert winners[0].unclaimed
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots')==0
    event=json.loads(await admin.fetchval('SELECT payload FROM runtime_events'))
    assert event['type']=='EXECUTION_NOT_STARTED' and event['reply_id'] is None
    assert event['stop_command_id']=='stop'
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='command'")=='rejected'
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='task'
    assert await admin.fetchval('SELECT status FROM runtime_task_executions')=='cancelled'


@pytest.mark.asyncio
async def test_preparing_requires_original_owner_and_cleanup_proof(database):
    pool,admin=database
    owner=await claim(pool)
    await stop(admin)
    async with pool.acquire() as db:
        store=PreparationStopStore(db)
        assert await store.claim('stop','stranger','node',lease_seconds=30) is None
        owned=await store.claim('stop',owner.worker_id,'node',lease_seconds=30)
        assert owned.owner==owner and not owned.unclaimed
        with pytest.raises(LostOwnership):
            await ExecutionStore(db).begin_reply(owner,'reply',event={'type':'REPLY_START'})
        with pytest.raises(ValueError):
            await store.finish(owned,'node',preparation_joined=True,resources_quiescent=False)
        assert await admin.fetchval('SELECT count(*) FROM runtime_events')==0
        await store.finish(owned,'node',preparation_joined=True,resources_quiescent=True)
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='command'")=='completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_sessions SET storage_node_id='other'",
    "UPDATE conversations SET active_product_task_id='other-task'",
    "UPDATE runtime_commands SET actor_user_id='other' WHERE id='stop'",
    "UPDATE runtime_commands SET claimed_by='unknown' WHERE id='command'",
])
async def test_unclaimed_inconsistent_identity_refuses_without_mutation(database,mutation):
    pool,admin=database
    await stop(admin)
    if 'actor_user' in mutation:
        await admin.execute("INSERT INTO users VALUES ('other','active')")
    await admin.execute(mutation)
    async with pool.acquire() as db:
        assert await PreparationStopStore(db).claim('stop','worker','node',lease_seconds=30) is None
    assert await admin.fetchval('SELECT count(*) FROM runtime_events')==0


@pytest.mark.asyncio
async def test_expired_preparation_and_lost_lease_cannot_settle(database):
    pool,admin=database
    owner=await claim(pool)
    await stop(admin)
    async with pool.acquire() as db:
        store=PreparationStopStore(db)
        owned=await store.claim('stop',owner.worker_id,'node',lease_seconds=30)
        await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        with pytest.raises(LostOwnership):
            await store.finish(owned,'node',preparation_joined=True,resources_quiescent=True)
    assert await admin.fetchval('SELECT count(*) FROM runtime_events')==0


@pytest.mark.asyncio
async def test_controller_cancels_and_joins_preparation_before_evidence(database):
    from workmate_runtime.execution.stopping_preparation import PreparingExecutionController
    pool,admin=database
    entered=asyncio.Event()
    joined=asyncio.Event()
    checked=[]
    async def prepare(owner,snapshot,guard):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()
    async def verify(owner):
        checked.append(joined.is_set())
        return joined.is_set()
    controller=PreparingExecutionController(pool,verify_cleanup=lambda *args: None,
        verify_preparation_cleanup=verify,poll_interval=.01)
    running=asyncio.create_task(controller.start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(entered.wait(),2)
        await stop(admin)
        result=await asyncio.wait_for(running,3)
        assert result.status=='cancelled' and result.reply_id is None
        assert checked==[True]
        assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots')==0
    finally:
        running.cancel()
        await asyncio.gather(running,return_exceptions=True)


@pytest.mark.asyncio
async def test_controller_unknown_cleanup_keeps_stopping_gate(database):
    from workmate_runtime.execution.stopping_preparation import PreparingExecutionController
    from workmate_runtime.execution.controller import ExecutionAborted
    pool,admin=database
    entered=asyncio.Event()
    async def prepare(*args):
        entered.set()
        await asyncio.Event().wait()
    async def verify(owner):
        return False
    controller=PreparingExecutionController(pool,verify_cleanup=lambda *args: None,
        verify_preparation_cleanup=verify,poll_interval=.01)
    running=asyncio.create_task(controller.start('command','worker','node',prepare))
    try:
        await asyncio.wait_for(entered.wait(),2)
        await stop(admin)
        with pytest.raises(ExecutionAborted):
            await asyncio.wait_for(running,3)
        assert await admin.fetchval('SELECT status FROM runtime_sessions')=='stopping'
        assert await admin.fetchval('SELECT count(*) FROM runtime_events')==0
    finally:
        running.cancel()
        await asyncio.gather(running,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('preparing',[False,True])
async def test_cancel_preserves_previous_task_snapshot_verbatim(database,preparing):
    pool,admin=database
    prior={'history':['previous answer'],'reply_id':'previous-reply'}
    await admin.execute("""INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,
        runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('old','session','other-task',3,1,2,'completed','previous-reply',$1::jsonb)""",json.dumps(prior))
    await admin.execute("""UPDATE runtime_sessions SET state_version=3,snapshot_id='old',snapshot=$1::jsonb,
        execution_epoch=2,status='completed',reply_id='previous-reply';
        """,json.dumps(prior))
    await admin.execute("UPDATE runtime_commands SET expected_epoch=2 WHERE id='command'")
    if preparing:
        await claim(pool)
    await stop(admin)
    async with pool.acquire() as db:
        store=PreparationStopStore(db)
        owned=await store.claim('stop','worker','node',lease_seconds=30)
        await store.finish(owned,'node',preparation_joined=True,resources_quiescent=True)
    row=await admin.fetchrow('SELECT * FROM runtime_sessions')
    assert row['state_version']==3 and row['snapshot_id']=='old'
    assert json.loads(row['snapshot'])==prior
    execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
    assert execution['snapshot_id'] is None and execution['input_snapshot_id']=='old'
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots')==1


@pytest.mark.asyncio
async def test_stop_at_begin_reply_rejection_uses_durable_no_reply_proof(database):
    from workmate_runtime.execution.stopping_preparation import PreparingExecutionController
    from test_execution_controller import prepare_reply
    from agentscope.message import TextBlock
    pool,admin=database
    original,_=prepare_reply([[TextBlock(text='must not execute')]])
    async def prepare(*args):
        result=await original(*args)
        await stop(admin)
        return result
    async def verify(owner):
        return True
    controller=PreparingExecutionController(pool,verify_cleanup=lambda *args: None,
        verify_preparation_cleanup=verify,poll_interval=.2)
    result=await controller.start('command','worker','node',prepare)
    assert result.status=='cancelled' and result.reply_id is None
    events=await admin.fetch('SELECT payload FROM runtime_events')
    assert [json.loads(e['payload'])['type'] for e in events]==['EXECUTION_NOT_STARTED']


@pytest.mark.asyncio
async def test_no_stop_runs_normal_core_and_unclaimed_stop_cannot_steal_preparation(database):
    from workmate_runtime.execution.stopping_preparation import PreparingExecutionController
    from test_execution_controller import prepare_reply
    from test_parked_stop import quiescent
    from agentscope.message import TextBlock
    pool,admin=database
    async def never(owner):
        raise AssertionError('Natural execution has a real snapshot verifier')
    controller=PreparingExecutionController(pool,verify_cleanup=quiescent,verify_preparation_cleanup=never)
    prepare,_=prepare_reply([[TextBlock(text='done')]])
    result=await controller.start('command','worker','node',prepare)
    assert result.status=='completed' and result.reply_id
    assert await controller.stop_unclaimed('missing','worker','node') is None


@pytest.mark.asyncio
async def test_worker_discovers_unclaimed_stop_without_inference_capacity(database):
    from workmate_runtime.execution.stopping_preparation import PreparingExecutionController
    from polling_worker import WorkerService
    from test_worker_service import bindings,wait_until
    pool,admin=database
    await stop(admin)
    async def forbidden(*args):
        raise AssertionError('Never-claimed Stop cannot prepare or execute Core')
    controller=PreparingExecutionController(pool,verify_cleanup=forbidden,verify_preparation_cleanup=forbidden)
    worker=WorkerService(pool,'node',bindings(forbidden),controller=controller,capacity=1,drain_seconds=0)
    await worker._slots.acquire()
    shutdown=asyncio.Event()
    loop=asyncio.create_task(worker.serve(shutdown))
    try:
        async def complete():
            return await admin.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
        await asyncio.wait_for(wait_until(complete),2)
        assert worker.available_capacity==0 and not worker.failures
    finally:
        shutdown.set()
        await asyncio.wait_for(loop,3)


@pytest.mark.asyncio
async def test_restart_identifies_no_start_delivery_without_authorizing_reexecution(database):
    from workmate_runtime.execution.reconciliation import inspect_sessions
    pool,admin=database
    await stop(admin)
    async with pool.acquire() as db:
        store=PreparationStopStore(db)
        owned=await store.claim('stop','worker','node',lease_seconds=30)
        await store.finish(owned,'node',preparation_joined=True,resources_quiescent=True)
        await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        observations=await inspect_sessions(db,worker_id='replacement',storage_node_id='node')
        assert observations[0].category=='terminal_delivery_pending'
        await admin.execute("UPDATE runtime_commands SET status='claimed' WHERE id='stop'")
        observations=await inspect_sessions(db,worker_id='replacement',storage_node_id='node')
        assert observations[0].category.startswith('blocked_')
