"""Retention must preserve history and refuse uncertain publication state."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from test_execution_context import database

pytestmark = pytest.mark.asyncio

async def seed(database):
    sessions, admin = database
    async with sessions.begin() as db:
        await db.execute(text("UPDATE tasks SET status='completed', updated_at=now()-interval '40 days'"))
        await db.execute(text("UPDATE runtime_commands SET status='completed'"))
        await db.execute(text("UPDATE conversations SET active_product_task_id=NULL,product_seq=2,business_seq=2"))
        await db.execute(text("UPDATE runtime_sessions SET runtime_seq=2"))
        await db.execute(text("UPDATE runtime_task_executions SET status='completed'"))
        await db.execute(text("INSERT INTO task_projection_states(task_id,session_id,reply_id,state,last_runtime_seq) VALUES ('t','s','r','{\"delivery_closed\":{\"artifact_ids\":[]}}',2)"))
        await db.execute(text("INSERT INTO runtime_projection_cursors(conversation_id,session_id,last_runtime_seq) VALUES ('c','s',2)"))
        for n, kind in [(1,'TOOL_CALL_DELTA'),(2,'TOOL_CALL_END')]:
            await db.execute(text("""INSERT INTO runtime_events(id,session_id,task_id,runtime_seq,execution_epoch,payload,created_at)
                VALUES (:id,'s','t',:seq,1,jsonb_build_object('type',CAST(:kind AS text),'tool_call_id','call'),now()-interval '40 days')"""),dict(id=f'e{n}',seq=n,kind=kind))
            await db.execute(text("""INSERT INTO product_events(id,conversation_id,product_seq,business_seq,task_id,source_runtime_event_id,projection_item_key,event_type,occurred_at,payload)
                VALUES (:id,'c',:seq,:seq,'t',:source,'0','CUSTOM',now()-interval '40 days','{}')"""),dict(id=f'p{n}',seq=n,source=f'e{n}'))
            await db.execute(text("""INSERT INTO realtime_business_outbox(event_id,conversation_id,business_seq,status,published_at,published_generation,published_incarnation,published_stream_id)
                VALUES (:id,'c',:seq,'published',now()-interval '40 days','g','i','1-0')"""),dict(id=f'p{n}',seq=n))
        await db.execute(text("""INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
            VALUES ('t','c',1,'tool_content','{"toolCallId":"call","kind":"arguments","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","incomplete": false,"sizeBytes": 123}')"""))
    return sessions,admin

async def test_preview_apply_and_repeated_apply_preserve_facts(database):
    from workmate_product.application.tool_content_retention import compact_transport
    sessions,admin=await seed(database)
    preview=await compact_transport(sessions,retention_days=30,limit=10)
    assert preview['runtime_events']==1
    assert await admin.fetchval('SELECT count(*) FROM runtime_events')==2
    assert (await compact_transport(sessions,retention_days=30,limit=10,apply=True))['runtime_events']==1
    assert (await compact_transport(sessions,retention_days=30,limit=10,apply=True))['runtime_events']==0
    assert await admin.fetchval('SELECT count(*) FROM task_events')==1
    assert await admin.fetchval('SELECT product_seq FROM conversations')==2
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==2
    assert await admin.fetchval('SELECT count(*) FROM runtime_events')==1
    from workmate_product.persistence.repositories.tool_content import find_content
    async with sessions.begin() as db:
        content=await find_content(db,'t','call','arguments')
        assert content['sha256']=='a'*64 and content['size_bytes']==123

@pytest.mark.parametrize('mutation',[
    "UPDATE tasks SET status='running'",
    "UPDATE conversations SET active_product_task_id='t'",
    "UPDATE runtime_sessions SET owner_id='worker'",
    "UPDATE runtime_commands SET status='indeterminate'",
    "UPDATE runtime_projection_cursors SET last_runtime_seq=0",
    "UPDATE realtime_business_outbox SET status='pending'",
    "DELETE FROM task_events",
    "UPDATE task_events SET payload=CAST('{\"toolCallId\":\"call\",\"kind\":\"arguments\",\"incomplete\": true}' AS json)",
])
async def test_uncertain_or_unreconstructable_data_not_deleted(database,mutation):
    from workmate_product.application.tool_content_retention import compact_transport
    sessions,admin=await seed(database)
    async with sessions.begin() as db: await db.execute(text(mutation))
    assert (await compact_transport(sessions,retention_days=30,limit=10,apply=True))['runtime_events']==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_events')==2

async def test_gc_database_guard_and_exclusive_lock(database):
    from workmate_product.persistence.repositories import tool_content_retention as repository
    sessions,admin=database
    async with sessions.begin() as db:
        with pytest.raises(ValueError,match='unsettled'):
            await repository.assert_no_writers(db)
    await seed(database)
    async with sessions.begin() as db:
        await repository.assert_no_writers(db)
        await repository.lock_object_gc(db)
        async with sessions.begin() as second:
            with pytest.raises(ValueError,match='already running'):
                await repository.lock_object_gc(second)

@pytest.mark.parametrize('days,limit',[(0,100),(1,0),(True,100),(1,1001)])
async def test_invalid_bounds(days,limit):
    from workmate_product.application.tool_content_retention import compact_transport
    with pytest.raises(ValueError): await compact_transport(None,retention_days=days,limit=limit)

async def test_delete_failure_rolls_back_outbox_and_product(database):
    from workmate_product.application.tool_content_retention import compact_transport
    sessions,admin=await seed(database)
    await admin.execute("""CREATE FUNCTION prevent_retention_delete() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'injected delete failure'; END $$;
        CREATE TRIGGER reject_delete BEFORE DELETE ON runtime_events
        FOR EACH ROW EXECUTE FUNCTION prevent_retention_delete()""")
    with pytest.raises(Exception,match='injected delete failure'):
        await compact_transport(sessions,apply=True)
    assert await admin.fetchval('SELECT count(*) FROM runtime_events')==2
    assert await admin.fetchval('SELECT count(*) FROM product_events')==2
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox')==2
