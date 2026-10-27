"""Real PostgreSQL lock scope at production Runtime entry points."""
import asyncio

import pytest

from test_postgres_execution_store import database  # noqa: F401
from test_resume_controller import parked, submit, allow
from test_suspension_store import begin, receipt
from workmate_runtime.transport.postgres import ExecutionStore
from workmate_runtime.transport.controls import ResumeStore
from workmate_runtime.transport.stopping import StopStore
from workmate_runtime.transport.suspension import SuspensionStore


async def seed_peer(db):
    await db.execute("""
        INSERT INTO conversations(id,user_id,agent_id,active_product_task_id)
            VALUES ('peer-conversation','user','agent',NULL);
        INSERT INTO tasks(id,conversation_id,status) VALUES ('peer-task','peer-conversation','created');
        UPDATE conversations SET active_product_task_id='peer-task' WHERE id='peer-conversation';
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id)
            VALUES ('peer-session','peer-conversation','node');
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
            VALUES ('peer-command','peer-session','peer-conversation','peer-task','user','StartTask',0,'{}',repeat('d',64));
        INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('peer-task','peer-session');
    """)


async def wait_blocked(observer, waiting, blocking):
    async with asyncio.timeout(3):
        while blocking not in await observer.fetchval('SELECT pg_blocking_pids($1)', waiting):
            await asyncio.sleep(.01)


async def prepare_entry(pool, db, kind):
    if kind == 'start':
        return lambda connection: ExecutionStore(connection).claim_start('command','worker','node',lease_seconds=30)
    initial = await parked(pool, db)
    if kind in {'cold_resume', 'cold_stop'}:
        claim = await begin(db, initial)
        assert claim is not None
        await SuspensionStore(db).release(claim, receipt(claim))
    if kind == 'suspend':
        snapshot_id = await db.fetchval("SELECT snapshot_id FROM runtime_sessions WHERE id='session'")
        return lambda connection: SuspensionStore(connection).begin(initial.owner,'node',snapshot_id,'generation-1')
    await submit(db, initial.reply_id)
    if kind in {'stop','cold_stop'}:
        await db.execute("UPDATE runtime_commands SET kind='StopTask',payload='{}' WHERE id='resume'")
        await db.execute("UPDATE tasks SET status='cancelling' WHERE id='task'")
        return lambda connection: StopStore(connection).claim_parked('resume','replacement' if kind=='cold_stop' else 'worker','node')
    return lambda connection: ResumeStore(connection).claim_resume('resume','replacement' if kind=='cold_resume' else 'worker','node',authorize_resume=allow)


@pytest.mark.parametrize('entry', ['start','resume','stop','suspend','cold_resume','cold_stop'])
@pytest.mark.parametrize('mutation', ["UPDATE agents SET deleted_at=clock_timestamp() WHERE id='agent'",
                                     "DELETE FROM agents WHERE id='agent'"])
async def test_same_agent_other_session_progresses_but_agent_mutation_waits(database, entry, mutation):
    pool, db = database
    invoke = await prepare_entry(pool, db, entry)
    await seed_peer(db)
    tasks = []
    update = None
    async with pool.acquire() as blocker, pool.acquire() as first, pool.acquire() as peer, pool.acquire() as writer:
        transaction = blocker.transaction()
        await transaction.start()
        mutation_transaction = writer.transaction()
        await mutation_transaction.start()
        try:
            # Production lock order acquires Agent before this downstream row.
            await blocker.fetchrow("SELECT id FROM runtime_sessions WHERE id='session' FOR UPDATE")
            waiting = asyncio.create_task(invoke(first)); tasks.append(waiting)
            await wait_blocked(db, first.get_server_pid(), blocker.get_server_pid())
            winner = await asyncio.wait_for(ExecutionStore(peer).claim_start(
                'peer-command','peer-worker','node',lease_seconds=30), 1)
            assert winner is not None and winner.session_id == 'peer-session'
            assert not waiting.done()
            update = asyncio.create_task(writer.execute(mutation)); tasks.append(update)
            await wait_blocked(db, writer.get_server_pid(), first.get_server_pid())
            assert not update.done()
            update.cancel()
            await asyncio.gather(update, return_exceptions=True)
            await mutation_transaction.rollback()
            mutation_transaction = None
            await transaction.rollback()
            transaction = None
            assert await asyncio.wait_for(waiting, 3) is not None
        finally:
            # Join the mutator before using its connection for rollback,
            # including timeout/assertion failures while it is still blocked.
            if update is not None:
                if not update.done():
                    update.cancel()
                await asyncio.gather(update, return_exceptions=True)
            # Roll back before releasing the reader: cancellation alone is
            # not a server commit barrier.
            if mutation_transaction is not None:
                await mutation_transaction.rollback()
            if transaction is not None:
                await transaction.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert await db.fetchval("SELECT deleted_at FROM agents WHERE id='agent'") is None


async def test_same_session_start_still_has_single_winner(database):
    pool, db = database
    async def claim(number):
        async with pool.acquire() as connection:
            return await ExecutionStore(connection).claim_start('command',f'worker-{number}','node',lease_seconds=30)
    winners = [owner for owner in await asyncio.gather(*(claim(i) for i in range(6))) if owner is not None]
    assert len(winners) == 1
    assert await db.fetchval("SELECT execution_epoch FROM runtime_sessions WHERE id='session'") == 1
    assert await db.fetchval("SELECT claimed_by FROM runtime_commands WHERE id='command'") == winners[0].worker_id
