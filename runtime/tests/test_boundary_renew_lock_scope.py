"""Boundary persistence is atomic and serializes renewal only within its Session."""
import asyncio
import json
import secrets

from test_postgres_execution_store import database  # noqa: F401
from test_agent_claim_lock_scope import seed_peer, wait_blocked
from workmate_runtime.transport.postgres import ExecutionStore


async def test_boundary_event_insert_blocks_own_renew_not_other_session(database):
    pool, db = database
    await seed_peer(db)
    gate = secrets.randbelow(2**62)
    # The trigger exists only in this unique disposable fixture schema. It
    # pauses the real event INSERT after save_boundary obtained session locks.
    await db.execute(f"""CREATE FUNCTION pause_boundary_insert() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM pg_advisory_xact_lock({gate}); RETURN NEW; END $$;
        CREATE TRIGGER pause_boundary BEFORE INSERT ON runtime_events
        FOR EACH ROW EXECUTE FUNCTION pause_boundary_insert();""")
    async with pool.acquire() as save, pool.acquire() as own_renew, pool.acquire() as peer, pool.acquire() as blocker:
        owner = await ExecutionStore(save).claim_start('command','worker','node',lease_seconds=30)
        peer_owner = await ExecutionStore(peer).claim_start('peer-command','peer-worker','node',lease_seconds=30)
        assert owner is not None and peer_owner is not None
        transaction = blocker.transaction()
        await transaction.start()
        tasks = []
        try:
            await blocker.execute('SELECT pg_advisory_xact_lock($1)', gate)
            saving = asyncio.create_task(ExecutionStore(save).save_boundary(owner,
                expected_state_version=0, status='awaiting_interaction', reply_id='reply',
                snapshot={'pending':['call']}, events=[{'type':'ask'}]))
            tasks.append(saving)
            await wait_blocked(db, save.get_server_pid(), blocker.get_server_pid())
            renewing = asyncio.create_task(ExecutionStore(own_renew).renew(owner,lease_seconds=30))
            tasks.append(renewing)
            await wait_blocked(db, own_renew.get_server_pid(), save.get_server_pid())
            await asyncio.wait_for(ExecutionStore(peer).renew(peer_owner,lease_seconds=30),1)
            assert not renewing.done()
            # Other connections see neither the new snapshot nor a partial event.
            assert await db.fetchval("SELECT state_version FROM runtime_sessions WHERE id='session'") == 0
            assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE session_id='session'") == 0
            await transaction.rollback()
            transaction = None
            assert await asyncio.wait_for(saving,3) == (1,1)
            await asyncio.wait_for(renewing,3)
            assert await db.fetchval("SELECT state_version FROM runtime_sessions WHERE id='session'") == 1
            assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE session_id='session'") == 1
            assert json.loads(await db.fetchval("SELECT snapshot FROM runtime_sessions WHERE id='session'")) == {'pending':['call']}
        finally:
            if transaction is not None:
                await transaction.rollback()
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
