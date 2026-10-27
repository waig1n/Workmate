"""Real Product authorization, PG cuts and independent Redis reset readers."""
import asyncio
import json
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from test_durable_projection import projection_database  # noqa: F401
from app.services.conversation_business_state import ConversationBusinessState
from app.services.conversation_business_state import ConversationUnavailable
from app.services.realtime_product_stream import RealtimeProductStream
from workmate_contracts.realtime import RealtimeEnvelope, MessageSnapshotReference, decode_cursor
from workmate_product.application.message_snapshots import save_message_snapshot
from workmate_product.realtime.log_manager import EventLogManager
from workmate_product.realtime.message_accumulator import MessageAccumulator
from workmate_product.realtime.reader import ConversationReaderHub
from workmate_product.realtime.redis_log import RedisConversationLog
from workmate_product.realtime.reset_builder import ResetBuilder


@pytest_asyncio.fixture
async def composition(projection_database):
    sessions, admin = projection_database
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires explicitly configured local Redis')
    await admin.execute("""UPDATE tasks SET status='running',run_id='reply';
        UPDATE runtime_sessions SET owner_id='worker',lease_until=now()+interval '60 seconds'""")
    client = Redis.from_url(url, decode_responses=True,
        retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
    namespace = 'business-reset-' + uuid4().hex
    log = RedisConversationLog(client, namespace=namespace)
    manager = EventLogManager(sessions, log)
    readers = [ConversationReaderHub(log, block_ms=20) for _ in range(2)]
    try:
        yield sessions, admin, log, manager, readers
    finally:
        for reader in readers:
            await reader.close()
        keys = [key async for key in client.scan_iter(match=f'workmate:{namespace}:*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_two_authorized_gateway_components_restore_exact_pg_prefix_and_share_live_tail(composition):
    sessions, _, log, manager, readers = composition
    ref = await manager.ensure('c')
    accumulator = MessageAccumulator('c', 't', 'reply', 1)
    for raw in [
        {'type': 'TEXT_BLOCK_START', 'reply_id': 'reply', 'block_id': 'b'},
        {'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'reply', 'block_id': 'b', 'delta': '你好'},
    ]:
        for envelope in accumulator.apply(raw):
            receipt = await log.append(ref, envelope)
    snapshot = accumulator.capture('partial', receipt.cursor, 0)
    async with sessions.begin() as db:
        await save_message_snapshot(db, snapshot, owner_id='worker')
    builders = [ResetBuilder(manager, log, reader,
        ConversationBusinessState(sessions, 'u', token_version=0).read_view) for reader in readers]
    handshakes = await asyncio.gather(*(builder.build('c') for builder in builders))
    try:
        for handshake in handshakes:
            assert handshake.reset.sync_status == 'synced'
            assert handshake.reset.snapshots == [snapshot]
            assert handshake.replay_from == snapshot.covered_cursor
            assert handshake.reset.business_state['tasks'][0]['status'] == 'running'
        envelope = accumulator.apply({'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'reply',
                                      'block_id': 'b', 'delta': '🙂'})[0]
        receipt = await log.append(ref, envelope)
        entries = await asyncio.wait_for(asyncio.gather(
            *(anext(handshake.subscription) for handshake in handshakes)), 3)
        assert all(entry.cursor == receipt.cursor and entry.envelope == envelope for entry in entries)
        assert all(handshake.reset.snapshots[0].blocks[0].text == '你好' for handshake in handshakes)
    finally:
        for handshake in handshakes:
            await handshake.subscription.aclose()


@pytest.mark.parametrize('actor,disabled', [('other', False), ('u', True)])
async def test_failed_authorization_never_initializes_or_reads_redis(composition, actor, disabled):
    sessions, admin, log, manager, readers = composition
    if disabled:
        await admin.execute("UPDATE users SET status='disabled'")
    manager.ensure = AsyncMock(wraps=manager.ensure)
    builder = ResetBuilder(manager, log, readers[0],
        ConversationBusinessState(sessions, actor, token_version=0).read_view)
    with pytest.raises(ConversationUnavailable):
        await builder.build('c')
    manager.ensure.assert_not_awaited()


async def test_two_sse_adapters_restore_live_body_and_exact_terminal_cut(composition):
    sessions, admin, log, manager, readers = composition
    ref = await manager.ensure('c')
    accumulator = MessageAccumulator('c', 't', 'reply', 1)
    for raw in [
        {'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'},
        {'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'initial body'},
    ]:
        for event in accumulator.apply(raw):
            receipt = await log.append(ref, event)
    baseline = accumulator.capture('partial', receipt.cursor, 0)
    async with sessions.begin() as db:
        await save_message_snapshot(db, baseline, owner_id='worker')
    streams = [RealtimeProductStream(manager, log, reader) for reader in readers]
    handles = await asyncio.gather(*(stream.open('c', authority=ConversationBusinessState(
        sessions, 'u', token_version=0)) for stream in streams))
    frames = [handle.frames() for handle in handles]

    def parse(frame):
        lines = frame.splitlines()
        payload = json.loads(next(line[6:] for line in lines if line.startswith('data: ')))
        token = next((line[4:] for line in lines if line.startswith('id: ')), None)
        return payload, token

    try:
        for iterator in frames:
            reset, token = parse(await anext(iterator))
            assert reset['name'] == 'REALTIME_RESET'
            assert reset['value']['snapshots'] == [baseline.model_dump(mode='json')]
            assert decode_cursor(token) == baseline.covered_cursor
            assert parse(await anext(iterator))[0]['name'] == 'REPLAY_DONE'
        event = accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply',
                                    'block_id':'b','delta':' and live tail'})[0]
        live = await log.append(ref, event)
        for iterator in frames:
            payload, token = parse(await asyncio.wait_for(anext(iterator), 3))
            assert payload['name'] == 'REALTIME_EVENT'
            assert payload['value'] == event.model_dump(mode='json')
            assert decode_cursor(token) == live.cursor
        terminal = accumulator.capture('final', live.cursor, 0)
        async with sessions.begin() as db:
            await save_message_snapshot(db, terminal, owner_id='worker')
        await admin.execute('UPDATE conversations SET business_seq=1')
        boundary = RealtimeEnvelope(event_id='terminal', conversation_id='c', task_id='t',
            reply_id='reply', source='business', business_seq=1, payload={'type':'RUN_FINISHED',
                'display_snapshot': MessageSnapshotReference.from_snapshot(terminal).model_dump(mode='json')})
        end = await log.append(ref, boundary)
        for iterator in frames:
            snapshot, token = parse(await asyncio.wait_for(anext(iterator), 3))
            assert snapshot['name'] == 'REALTIME_SNAPSHOT' and token is None
            assert snapshot['value'] == terminal.model_dump(mode='json')
            notification, token = parse(await anext(iterator))
            assert notification['value'] == boundary.model_dump(mode='json')
            assert decode_cursor(token) == end.cursor
        await handles[0].aclose()
        assert not readers[0]._hubs and readers[1]._hubs
    finally:
        await asyncio.gather(*(stream.close() for stream in streams))
