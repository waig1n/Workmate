"""Opt-in Redis smoke: random hints only, never modifies keys or product facts."""
import asyncio
import os
import uuid

import pytest

from workmate_product.coordination import WakeupBus


@pytest.mark.asyncio
async def test_real_redis_broadcast_idle_and_reconnect():
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires explicitly configured Redis')
    key = 'test:' + uuid.uuid4().hex
    async with WakeupBus(url) as sender, WakeupBus(url) as receiver:
        with receiver.subscribe(key) as first, receiver.subscribe(key) as second:
            async def received():
                version = first.version
                async with asyncio.timeout(5):
                    while first.version == version:
                        sender.publish_nowait(key)
                        await first.wait(version, .1)
                await second.wait(version, .1)
            await received()
            # More than the socket timeout, but healthy Pub/Sub must stay usable.
            await asyncio.sleep(3)
            await received()
            await receiver.client.connection_pool.disconnect()
            await received()
    assert all(task.done() for task in sender._tasks + receiver._tasks)
