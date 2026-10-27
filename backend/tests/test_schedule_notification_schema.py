"""Schedule notifications enforce durable delivery origins and uniqueness."""
import pytest
from test_channel_schema import migrated, seed
from test_schedules_schema import seed as seed_schedule


async def setup(migrated):
    db,_,apply=migrated
    await seed(db)
    await seed_schedule(db)
    await db.execute("""INSERT INTO channel_outbox(id,channel_id,inbox_id,kind,dedupe_key,target,text)
        VALUES('old','ch','i','reply','old','{}','old')""")
    return db,apply

async def test_notification_origins_and_uniqueness(migrated):
    db,apply=await setup(migrated)
    assert await db.fetchval("SELECT notification FROM schedules WHERE id='s'") is None
    assert await db.fetchval("SELECT inbox_id FROM channel_outbox WHERE id='old'")=='i'
    await db.execute("INSERT INTO channel_outbox(id,channel_id,schedule_occurrence_id,kind,dedupe_key,target,text) VALUES('new','ch','o','schedule','new','{}','result')")
    for assignment in ("inbox_id='i'",'schedule_occurrence_id=NULL'):
        with pytest.raises(Exception,match='ck_channel_outbox_origin'):
            await db.execute('UPDATE channel_outbox SET '+assignment+" WHERE id='new'")
    with pytest.raises(Exception,match='uq_channel_outbox_schedule_occurrence'):
        await db.execute("INSERT INTO channel_outbox(id,channel_id,schedule_occurrence_id,kind,dedupe_key,target,text) VALUES('dup','ch','o','schedule','dup','{}','result')")
