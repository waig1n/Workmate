"""Real PG lease fence and immutable checkpoint reference publication."""
import json
import pytest
from test_postgres_execution_store import database
from test_resume_controller import parked
from workmate_runtime.transport.checkpoints import CheckpointStore
from workmate_runtime.transport.delivery import DeliveryConflict


async def test_checkpoint_records_approval_owner(database):
    pool, db = database
    first = await parked(pool,db)
    checkpoint_id = await CheckpointStore(db).record(first.owner,'node',boundary='approval',
        manifest={'version':1,'files':[],'directories':[],'total_bytes':0})
    row = await db.fetchrow('SELECT * FROM workspace_checkpoints WHERE id=$1',checkpoint_id)
    assert row['status']=='available'
    assert json.loads(row['manifest'])['total_bytes']==0
    assert row['conversation_id']=='conversation'


async def test_expired_checkpoint_cannot_publish(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    with pytest.raises(DeliveryConflict):
        await CheckpointStore(db).record(first.owner,'node',boundary='approval',manifest={})
    assert await db.fetchval('SELECT count(*) FROM workspace_checkpoints')==0
