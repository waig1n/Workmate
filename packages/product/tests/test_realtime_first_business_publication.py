"""First Product notifications publish without a browser or Worker-created log."""
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

from test_execution_context import database  # noqa: F401
from test_realtime_redis_log import log  # noqa: F401
from workmate_contracts.realtime import RealtimeCursor
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.persistence.models import Conversation
from workmate_product.projections.store import ProjectedEvent, append_business_events
from workmate_product.realtime.business_publisher import BusinessPublisher
from workmate_product.realtime.log_manager import EventLogManager


async def append(sessions, cid='c', *, operation='first', items=None):
    items = items or [ProjectedEvent(key='start', event_type='RUN_STARTED',
        payload={'type':'RUN_STARTED','threadId':cid,'runId':'reply'})]
    async with sessions.begin() as db:
        return await append_business_events(db, cid, operation_key=operation,
            task_id='t' if cid == 'c' else None, reply_id='reply', items=items)


async def entries(log, manager, cid='c'):
    ref = await manager.ensure(cid)
    page = await log.read(ref, RealtimeCursor(conversation_id=cid,
        generation=ref.generation, stream_id='0-0'))
    return page.entries


async def test_first_run_started_and_tool_progress_publish_without_prior_log(database, log, monkeypatch):
    sessions, admin = database
    log.max_bytes = 1048576
    calls = [
        {'type':'RUN_STARTED','threadId':'c','runId':'reply'},
        {'type':'STEP_STARTED','stepName':'model'},
        {'type':'TOOL_CALL_START','toolCallId':'tool','toolCallName':'read'},
        {'type':'TOOL_CALL_ARGS','toolCallId':'tool','delta':'{"path":"report"}'},
        {'type':'CUSTOM','name':'TOOL_RESULT_DELTA','value':{'toolCallId':'tool','delta':'preview'}},
        {'type':'TOOL_CALL_RESULT','toolCallId':'tool','content':'preview'},
        {'type':'STEP_FINISHED','stepName':'model'},
    ]
    items = [ProjectedEvent(key=str(index), event_type=payload['type'], payload=payload)
             for index, payload in enumerate(calls)]
    assert await admin.fetchval('SELECT count(*) FROM conversation_event_logs') == 0
    assert await append(sessions, items=items) == tuple(range(1, 8))
    assert await append(sessions, items=items) == tuple(range(1, 8))
    assert await admin.fetchval('SELECT count(*) FROM conversation_event_logs') == 0
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == len(items)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == len(items)
    # Verify no PG transaction/row lock spans actual Redis commands, including
    # the first log initialization. Redis client lifecycle stays caller-owned.
    original = log._call
    network_calls = []
    async def outside_transaction(*args, **kwargs):
        assert sessions.kw['bind'].pool.checkedout() == 0
        async with sessions.begin() as db:
            await db.execute(text('SELECT event_id FROM realtime_business_outbox FOR UPDATE NOWAIT'))
        network_calls.append(args[0])
        return await original(*args, **kwargs)
    monkeypatch.setattr(log, '_call', outside_transaction)
    manager = EventLogManager(sessions, log)
    publisher = BusinessPublisher(sessions, manager, log, CommandQueuePolicy())
    for _ in items:
        assert await publisher.publish_once() == 1
    assert await publisher.publish_once() == 0
    received = await entries(log, manager)
    assert [entry.envelope.payload for entry in received] == calls
    assert [entry.envelope.business_seq for entry in received] == list(range(1, 8))
    assert len({entry.envelope.event_id for entry in received}) == len(items)
    assert network_calls
    assert await admin.fetchval("SELECT count(*) FROM realtime_business_outbox WHERE status='published'") == len(items)
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='cmd'") == 'accepted'


async def test_rollback_before_first_log_leaves_no_notification_or_redis_side_effect(database, log):
    sessions, admin = database
    with pytest.raises(RuntimeError, match='rollback'):
        async with sessions.begin() as db:
            await append_business_events(db, 'c', operation_key='aborted', task_id='t',
                items=[ProjectedEvent(key='start', event_type='RUN_STARTED', payload={'type':'RUN_STARTED'})])
            assert await db.scalar(text('SELECT count(*) FROM realtime_business_outbox')) == 1
            raise RuntimeError('rollback')
    publisher = BusinessPublisher(sessions, EventLogManager(sessions, log), log, CommandQueuePolicy())
    assert await publisher.publish_once() == 0
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == 0
    assert await admin.fetchval('SELECT count(*) FROM conversation_event_logs') == 0
    assert [key async for key in log.client.scan_iter(match=f'workmate:{log.namespace}:*')] == []


async def test_first_log_outage_preserves_notification_while_other_conversation_publishes(database, log, monkeypatch):
    sessions, admin = database
    async with sessions.begin() as db:
        db.add(Conversation(id='other', user_id='u', agent_id='a', model_profile='chat'))
    await append(sessions)
    await append(sessions, 'other')
    first_id = await admin.fetchval("SELECT id FROM product_events WHERE conversation_id='c'")
    original = log.initialize
    async def fail_first(cid, generation):
        if cid == 'c':
            raise RedisConnectionError('isolated log unavailable')
        return await original(cid, generation)
    monkeypatch.setattr(log, 'initialize', fail_first)
    manager = EventLogManager(sessions, log)
    publisher = BusinessPublisher(sessions, manager, log, CommandQueuePolicy())
    assert await publisher.publish_once() == 2
    statuses = await admin.fetch('SELECT conversation_id,status FROM realtime_business_outbox ORDER BY conversation_id')
    assert [tuple(row) for row in statuses] == [('c','pending'), ('other','published')]
    assert len(await entries(log, manager, 'other')) == 1
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='cmd'") == 'accepted'
    monkeypatch.setattr(log, 'initialize', original)
    await admin.execute("UPDATE realtime_business_outbox SET next_attempt_at=clock_timestamp() WHERE status='pending'")
    assert await publisher.publish_once() == 1
    received = await entries(log, manager)
    assert len(received) == 1 and received[0].envelope.event_id == first_id
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 2
