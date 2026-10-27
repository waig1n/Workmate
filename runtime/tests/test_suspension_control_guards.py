"""A durable suspension excludes old-owner controls without losing intent."""
import pytest
from test_postgres_execution_store import database
from test_resume_controller import parked, submit, allow
from test_parked_stop import stop_intent
from workmate_runtime.transport.controls import ResumeStore
from workmate_runtime.transport.stopping import StopStore


async def suspension(db, status):
    await db.execute('''INSERT INTO runtime_suspensions
        (snapshot_id,session_id,task_id,reply_id,storage_node_id,source_worker_id,source_epoch,
         state_version,runtime_seq,resource_generation,snapshot_digest,status,cleanup_receipt,released_at)
        SELECT b.id,b.session_id,b.task_id,b.reply_id,s.storage_node_id,s.owner_id,b.execution_epoch,
            b.state_version,b.runtime_seq,'generation',repeat('a',64),$1::varchar,
            CASE WHEN $1='released' THEN '{}'::jsonb ELSE NULL END,
            CASE WHEN $1='released' THEN clock_timestamp() ELSE NULL END
        FROM runtime_snapshots b JOIN runtime_sessions s ON s.snapshot_id=b.id''', status)


@pytest.mark.parametrize('status', ['releasing', 'released', 'blocked'])
async def test_old_owner_cannot_claim_resume_during_suspension(database, status):
    pool, db = database
    first = await parked(pool, db)
    await submit(db, first.reply_id)
    await suspension(db, status)
    async with pool.acquire() as connection:
        result = await ResumeStore(connection).claim_resume('resume', 'worker', 'node', authorize_resume=allow)
    assert result is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'accepted'
    assert await db.fetchval('SELECT status FROM runtime_sessions') == 'awaiting_interaction'


@pytest.mark.parametrize('status', ['releasing', 'released', 'blocked'])
async def test_old_owner_cannot_claim_stop_during_suspension(database, status):
    pool, db = database
    await parked(pool, db)
    await stop_intent(db)
    await suspension(db, status)
    async with pool.acquire() as connection:
        result = await StopStore(connection).claim_parked('stop', 'worker', 'node')
    assert result is None
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'") == 'accepted'
    assert await db.fetchval("SELECT status FROM tasks WHERE id='task'") == 'cancelling'
