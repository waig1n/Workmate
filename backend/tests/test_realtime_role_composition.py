"""Gateway realtime composition requires a log; its role owns Redis lifetime."""
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.config import Settings
from app.services.durable_runtime import compose_product_services
from app.services.realtime_product_stream import RealtimeProductStream
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.redis_log import RedisConversationLog


def test_shared_log_factory_maps_budgets_and_preserves_client_ownership():
    from workmate_product.realtime.composition import create_realtime_log
    policy = RealtimePolicy(retention_seconds=123, ttl_seconds=321, max_log_bytes=524288)
    client = Redis(retry=Retry(NoBackoff(), 0))
    log = create_realtime_log(client, namespace='composition-test', policy=policy)
    assert isinstance(log, RedisConversationLog)
    assert log.client is client
    assert log.namespace == 'composition-test'
    assert log.retention_ms == 123000
    assert log.ttl_seconds == 321
    assert log.max_bytes == 524288


async def test_gateway_realtime_reader_maps_policy_and_uses_own_sessions(monkeypatch):
    monkeypatch.setattr('app.services.knowledge.knowledge_settings', lambda: SimpleNamespace(enabled=False))
    sessions = object()
    policy = RealtimePolicy(reader_page_events=7, client_buffer_events=32,
        client_buffer_bytes=524288, reader_block_ms=123, max_gateway_readers=11,
        max_gateway_subscriptions=17)
    settings = Settings(_env_file=None, realtime=policy)
    client = Redis(retry=Retry(NoBackoff(), 0))
    log = RedisConversationLog(client, namespace='gateway-role-test')
    close_client = AsyncMock(wraps=client.aclose)
    monkeypatch.setattr(client, 'aclose', close_client)
    stream = None
    try:
        _, _, stream = compose_product_services(sessions, settings, realtime_log=log)
        assert isinstance(stream, RealtimeProductStream)
        assert stream.policy is settings.realtime
        assert stream.log is log and stream.manager.log is log
        assert stream.manager.sessions is sessions
        reader = stream.reader
        assert reader.log is log
        assert reader.page_events == 7
        assert reader.buffer_events == 32 and reader.buffer_bytes == 524288
        assert reader.block_ms == 123 and reader.max_readers == 11
        close_reader = AsyncMock(wraps=reader.close)
        monkeypatch.setattr(reader, 'close', close_reader)
        # Role lifecycle owns both resources; composition itself neither
        # closes an injected client nor steals the reader's ownership.
        async with AsyncExitStack() as stack:
            stack.push_async_callback(reader.close)
            stack.push_async_callback(stream.close)
            assert not reader._closed
        close_reader.assert_awaited_once()
        assert reader._closed and stream._closed
        close_client.assert_not_awaited()
    finally:
        if isinstance(stream, RealtimeProductStream):
            await stream.close()
            await stream.reader.close()
        await client.aclose()


from test_durable_projection import projection_database  # noqa: E402,F401
from test_business_reset_composition import composition  # noqa: E402,F401


async def test_composed_gateways_share_real_log_without_sharing_subscriber_lifetime(composition, monkeypatch):
    import asyncio
    import json
    from app.services.conversation_business_state import ConversationBusinessState
    from workmate_contracts.realtime import RealtimeEnvelope
    sessions, _, log, _, _ = composition
    monkeypatch.setattr('app.services.knowledge.knowledge_settings', lambda: SimpleNamespace(enabled=False))
    settings = Settings(_env_file=None)
    streams = [compose_product_services(sessions, settings, realtime_log=log)[2] for _ in range(2)]
    handles = []
    try:
        for stream in streams:
            handles.append(await stream.open('c', authority=ConversationBusinessState(sessions, 'u', token_version=0)))
        frames = [handle.frames() for handle in handles]
        async def named(iterator, name):
            async with asyncio.timeout(3):
                while True:
                    frame = await anext(iterator)
                    data = next((line[6:] for line in frame.splitlines() if line.startswith('data: ')), None)
                    if data:
                        event = json.loads(data)
                        if event.get('name') == name:
                            return event['value']
        for iterator in frames:
            reset = await named(iterator, 'REALTIME_RESET')
            assert reset['conversation_id'] == 'c'
            await named(iterator, 'REPLAY_DONE')
        ref = await streams[0].manager.ensure('c')
        event = RealtimeEnvelope(event_id='composed-notice', conversation_id='c', task_id='t',
            reply_id='reply', source='business', business_seq=1,
            payload={'type':'CUSTOM','name':'NOTICE','value':{'message':'both gateways'}})
        await log.append(ref, event)
        values = await asyncio.gather(*(named(iterator, 'REALTIME_EVENT') for iterator in frames))
        assert values == [event.model_dump(mode='json')] * 2
        await streams[0].close()
        await streams[0].reader.close()
        assert not handles[1].closed
        next_event = event.model_copy(update={'event_id':'after-peer-close','business_seq':2})
        await log.append(ref, next_event)
        assert (await named(frames[1], 'REALTIME_EVENT'))['event_id'] == 'after-peer-close'
        assert sessions.kw['bind'].pool.checkedout() == 0
    finally:
        for stream in streams:
            await stream.close()
            await stream.reader.close()
