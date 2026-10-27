"""A late Stop settles an already immutable, quiescent terminal boundary."""
import asyncio
import json

import pytest
from agentscope.message import TextBlock

from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.terminal_stop import TerminalStopController
from polling_worker import WorkerService
from test_execution_controller import prepare_reply
from test_parked_stop import quiescent
from test_postgres_execution_store import database
from test_worker_service import bindings,wait_until


async def terminal(pool):
    prepare,calls = prepare_reply([[TextBlock(text='natural result')]])
    result = await RunningExecutionController(pool,verify_cleanup=quiescent).start(
        'command','original-worker','node',prepare)
    return result,calls


async def admit(db):
    # Same resolved identities that Product accept_stop writes under its locks.
    async with db.transaction():
        await db.execute("UPDATE tasks SET status='cancelling' WHERE id='task'")
        await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
            kind,expected_epoch,payload,content_digest,reply_id)
            SELECT 'stop',id,conversation_id,active_task_id,'user','StopTask',execution_epoch,'{}',
                repeat('d',64),reply_id FROM runtime_sessions WHERE id='session'""")


async def test_late_stop_settles_once_without_execution_or_new_runtime_facts(database):
    pool,db = database
    result,calls = await terminal(pool)
    before = await db.fetchrow('SELECT snapshot_id,state_version,runtime_seq,owner_id FROM runtime_sessions')
    await admit(db)
    # No new ownership or lease is needed: the immutable proof is terminal.
    await db.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 hour'")
    controller = TerminalStopController(pool)
    results = await asyncio.gather(*(controller.settle('stop',f'settler-{i}','node') for i in range(3)))
    assert all(results)
    assert len(calls)==1
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
    assert await db.fetchrow('SELECT snapshot_id,state_version,runtime_seq,owner_id FROM runtime_sessions')==before
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'")==1
    end=json.loads(await db.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='REPLY_END'"))
    assert end['finished_reason']=='completed'
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    assert await db.fetchval('SELECT active_product_task_id FROM conversations')=='task'
    assert result.status=='completed'


@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_commands SET expected_epoch=0 WHERE id='stop'",
    "UPDATE runtime_commands SET reply_id='foreign' WHERE id='stop'",
    "UPDATE runtime_commands SET actor_user_id='foreign' WHERE id='stop'",
    "UPDATE runtime_sessions SET storage_node_id='foreign'",
    "UPDATE runtime_sessions SET state_version=state_version+1",
    "UPDATE conversations SET active_product_task_id='other-task'",
    "UPDATE runtime_commands SET status='claimed' WHERE id='command'",
    "UPDATE runtime_events SET payload=payload-'resource_cleanup' WHERE payload->>'type'='EXECUTION_BOUNDARY'",
    "UPDATE runtime_events SET payload=jsonb_set(payload,'{resource_cleanup,resources_quiescent}','false') WHERE payload->>'type'='EXECUTION_BOUNDARY'",
    "UPDATE runtime_events SET payload=jsonb_set(payload,'{finished_reason}','\"error\"') WHERE payload->>'type'='REPLY_END'",
])
async def test_late_stop_rejects_inconsistent_or_unproven_terminal(database,mutation):
    pool,db = database
    await terminal(pool)
    await admit(db)
    if "actor_user_id='foreign'" in mutation:
        await db.execute("INSERT INTO users VALUES ('foreign','active')")
    await db.execute(mutation)
    assert await TerminalStopController(pool).settle('stop','settler','node') is False
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='accepted'


async def test_projected_cancellation_can_settle_and_completed_retry_survives_gate_release(database):
    pool,db = database
    await terminal(pool)
    await admit(db)
    await db.execute("UPDATE tasks SET status='cancelled' WHERE id='task'")
    controller=TerminalStopController(pool)
    assert await controller.settle('stop','settler','node')
    await db.execute('UPDATE runtime_sessions SET active_task_id=NULL')
    await db.execute('UPDATE conversations SET active_product_task_id=NULL')
    assert await controller.settle('stop','settler-2','node')


async def test_worker_discovers_late_terminal_stop_after_execution_loop_exited(database):
    pool,db = database
    await terminal(pool)
    async def forbidden(*args,**kwargs):
        raise AssertionError('Late Stop cannot assemble or execute Core')
    class NoExecution:
        start=forbidden
    worker=WorkerService(pool,'node',bindings(forbidden),controller=NoExecution(),drain_seconds=0)
    shutdown=asyncio.Event()
    loop=asyncio.create_task(worker.serve(shutdown))
    try:
        await admit(db)
        async def completed():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
        await wait_until(completed)
        assert not worker.failures
    finally:
        shutdown.set()
        await asyncio.wait_for(loop,5)


async def test_late_stop_completion_joins_caller_transaction_without_early_commit(database):
    from workmate_runtime.transport.terminal_stop import TerminalStopStore
    pool,db=database
    await terminal(pool)
    await admit(db)
    async with pool.acquire() as connection:
        with pytest.raises(RuntimeError):
            async with connection.transaction():
                assert await TerminalStopStore(connection).settle('stop','settler','node')
                assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='accepted'
                raise RuntimeError('fixture outer rollback')
    assert await TerminalStopController(pool).settle('stop','retry-worker','node')


async def test_full_inference_capacity_does_not_delay_late_stop_settlement(database):
    from test_worker_service import add_second_session
    pool,db=database
    await terminal(pool)
    await add_second_session(db)
    entered=asyncio.Event()
    prepare,calls=prepare_reply([],entered=entered)
    worker=WorkerService(pool,'node',bindings(prepare),capacity=1,
        controller=RunningExecutionController(pool,verify_cleanup=quiescent),drain_seconds=0)
    shutdown=asyncio.Event()
    loop=asyncio.create_task(worker.serve(shutdown))
    try:
        await asyncio.wait_for(entered.wait(),3)
        assert worker.available_capacity==0
        await admit(db)
        async def completed():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
        await asyncio.wait_for(wait_until(completed),2)
        assert worker.available_capacity==0 and len(calls)==1
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command-b'")=='claimed'
    finally:
        shutdown.set()
        await asyncio.wait_for(loop,5)
