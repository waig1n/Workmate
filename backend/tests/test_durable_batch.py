"""Real live projector equivalence and large-result replay on PostgreSQL."""
import json

import pytest
from sqlalchemy import text

from test_durable_projection import projection_database, emit  # noqa: F401
from app.adapter.durable_projection import live_projector
from workmate_product.projections.store import consume_next, consume_batch, read_product_events
from workmate_contracts import ProductCursor
from app.services.durable_runtime import compose_product_runtime


async def facts(db):
    queries = {
        'state': 'SELECT state,last_runtime_seq FROM task_projection_states',
        'audit': 'SELECT seq,event_type,payload FROM task_events ORDER BY seq',
        'events': '''SELECT product_seq,source_runtime_event_id,projection_item_key,event_type,payload
                     FROM product_events ORDER BY product_seq''',
        'task': 'SELECT status,run_id FROM tasks',
        'cursor': 'SELECT session_id,last_runtime_seq FROM runtime_projection_cursors',
    }
    return {key: [dict(row) for row in (await db.execute(text(sql))).mappings()]
            for key, sql in queries.items()}


async def test_live_batch_matches_single_events_audit_and_recovery(projection_database):
    sessions, admin = projection_database
    events = [
        {'type':'REPLY_START','session_id':'s'},
        {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'Write'},
        *[{'type':'TOOL_CALL_DELTA','tool_call_id':'call','delta':s} for s in ('{"path":','"output/a.txt"}')],
        {'type':'TOOL_CALL_END','tool_call_id':'call'},
        *[{'type':'TOOL_RESULT_TEXT_DELTA','tool_call_id':'call','delta':s} for s in ('written',' successfully')],
        {'type':'TOOL_RESULT_END','tool_call_id':'call','state':'success'},
        {'type':'REPLY_END','finished_reason':'completed'},
    ]
    for seq, raw in enumerate(events, 1):
        await emit(admin, seq, raw)
    async with sessions() as db:
        async with db.begin():
            while await consume_next(db, 'c', project=live_projector()):
                pass
            expected = await facts(db)
            await db.rollback()
    # Fresh transaction and projector per batch models persisted state reload.
    while True:
        async with sessions.begin() as db:
            last = await consume_batch(db, 'c', project=live_projector())
        if last is None:
            break
    async with sessions() as db:
        assert await facts(db) == expected
        page = await read_product_events(db, ProductCursor(conversation_id='c', product_seq=0))
        assert len(page.events) == len(expected['events'])
    assert [row['event_type'] for row in expected['audit']] == ['run_started', 'tool_call', 'tool_result']


async def test_service_consumes_ready_deltas_in_one_projection_step(projection_database):
    sessions, admin = projection_database
    await emit(admin, 1, {'type':'REPLY_START','session_id':'s'})
    await emit(admin, 2, {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'read'})
    for seq in range(3, 9):
        await emit(admin, seq, {'type':'TOOL_CALL_DELTA','tool_call_id':'call','delta':'a'})
    service = compose_product_runtime(sessions)
    for _ in range(3):
        assert await service.tick('projection') == 1
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 8
    assert await admin.fetchval('SELECT version FROM runtime_projection_cursors') == 3


async def test_large_result_does_not_block_cursor_or_replay(projection_database):
    sessions, admin = projection_database
    events = [{'type':'REPLY_START','session_id':'s'},
              {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'Bash'},
              *[{'type':'TOOL_RESULT_TEXT_DELTA','tool_call_id':'call','delta':'汉'*10000} for _ in range(6)],
              {'type':'TOOL_RESULT_END','tool_call_id':'call','state':'error'},
              {'type':'REPLY_END','finished_reason':'error'}]
    for seq, raw in enumerate(events, 1):
        await emit(admin, seq, raw)
    while True:
        async with sessions.begin() as db:
            event = await consume_batch(db, 'c', project=live_projector())
        if event is None:
            break
    result = json.loads(await admin.fetchval("SELECT payload FROM product_events WHERE event_type='TOOL_CALL_RESULT'"))
    assert '已截断' in result['content']
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == len(events)
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE payload->>'name'='TOOL_RESULT_DELTA'") == 6
    audit = json.loads(await admin.fetchval("SELECT payload FROM task_events WHERE event_type='tool_result'"))
    assert audit['status'] == 'error'
