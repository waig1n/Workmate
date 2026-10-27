import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from workmate_product.coordination import WakeupBus, stage_wakeup


@pytest.mark.asyncio
async def test_broadcast_and_query_wait_race_and_release():
    bus = WakeupBus('unused')
    with bus.subscribe('conversation:c') as first, bus.subscribe('conversation:c') as second:
        assert first is second
        before = first.version
        # A notification arriving between query and wait must not be cleared.
        bus.publish_nowait('conversation:c')
        await asyncio.wait_for(asyncio.gather(first.wait(before, 10), second.wait(before, 10)), .1)
        version = first.version
        bus.publish_nowait('conversation:other')
        assert first.version == version
    assert not bus._pulses


@pytest.mark.asyncio
async def test_fallback_cancel_and_bounded_publisher():
    bus = WakeupBus('unused')
    with bus.subscribe('commands') as pulse:
        await pulse.wait(pulse.version, .001)
        task = asyncio.create_task(pulse.wait(pulse.version, 60))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for i in range(2000):
        bus.publish_nowait(str(i))
    assert bus._queue.qsize() == len(bus._pending) == 1024


@pytest.mark.asyncio
async def test_hints_only_after_outer_commit_and_savepoint_rollback():
    engine = create_async_engine('sqlite+aiosqlite://')
    bus = WakeupBus('unused')
    sessions = async_sessionmaker(engine, info={'wakeup_bus': bus})
    try:
        async with sessions() as db:
            await db.execute(text('SELECT 1'))
            stage_wakeup(db, 'outer')
            async with db.begin_nested():
                stage_wakeup(db, 'nested')
            assert bus._queue.empty()
            try:
                async with db.begin_nested():
                    stage_wakeup(db, 'rolled-back')
                    raise ValueError()
            except ValueError:
                pass
            await db.commit()
            assert bus._pending == {'outer', 'nested'}
            await db.execute(text('SELECT 1'))
            stage_wakeup(db, 'aborted')
            await db.rollback()
            await db.execute(text('SELECT 1'))
            await db.commit()
            assert bus._pending == {'outer', 'nested'}
    finally:
        await engine.dispose()


class FakePubSub:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def subscribe(self, channel):
        self.client.subscribed.set()
        if self.client.fail:
            raise OSError('secret connection URL must not be logged')

    async def get_message(self, timeout):
        return await self.client.incoming.get()


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.subscribed = asyncio.Event()
        self.incoming = asyncio.Queue()
        self.published = asyncio.Event()
        self.closed = False

    def pubsub(self):
        return FakePubSub(self)

    async def publish(self, channel, payload):
        self.published.set()
        if self.fail:
            raise OSError('secret connection URL must not be logged')
        await self.incoming.put({'type': 'message', 'data': payload})

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_redis_fanout_malformed_hints_and_shutdown():
    client = FakeRedis()
    async with WakeupBus('unused', client=client) as bus:
        with bus.subscribe('runtime') as pulse:
            await client.subscribed.wait()
            version = pulse.version
            for raw in ['not-json', '[]', '{"key": 1}', 'x' * 513]:
                await client.incoming.put({'type': 'message', 'data': raw})
            await client.incoming.put({'type': 'message', 'data': '{"key":"runtime"}'})
            await asyncio.wait_for(pulse.wait(version, 60), .2)
            assert pulse.version == version + 1
            bus.publish_nowait('delivery')
            await asyncio.wait_for(client.published.wait(), .2)
    assert client.closed
    assert all(task.done() for task in bus._tasks)


@pytest.mark.asyncio
async def test_redis_failure_does_not_break_local_notifications(caplog):
    client = FakeRedis(fail=True)
    async with WakeupBus('unused', client=client) as bus:
        with bus.subscribe('commands') as pulse:
            await client.subscribed.wait()
            version = pulse.version
            bus.publish_nowait('commands')
            await pulse.wait(version, .01)
            await client.published.wait()
            await pulse.wait(pulse.version, .001)  # reconciliation timeout still works
    assert 'secret connection' not in caplog.text
    assert client.closed
