"""One Redis subscription per process, with bounded publication and local fanout.

Messages contain routing identities only. Duplicate/lost/malformed hints cannot
change product state. Every consumer must re-read durable facts and periodically
scan even when Redis is connected.
"""
import asyncio
from contextlib import contextmanager
import json
import logging

from .signals import Pulse

logger = logging.getLogger(__name__)
CHANNEL = 'workmate:wakeup:v1'


class WakeupBus:
    def __init__(self, url, *, client=None):
        self.url = url
        self.client = client
        self._pulses = {}
        self._pending = set()
        self._queue = asyncio.Queue(maxsize=1024)
        self._tasks = []
        self._closed = False

    @contextmanager
    def subscribe(self, key):
        pulse = self._pulses.setdefault(key, Pulse())
        pulse.users += 1
        try:
            yield pulse
        finally:
            pulse.users -= 1
            if not pulse.users:
                self._pulses.pop(key, None)

    def signal_local(self, key):
        pulse = self._pulses.get(key)
        if pulse is not None:
            pulse.signal()

    def publish_nowait(self, key):
        if self._closed:
            return
        self.signal_local(key)
        if key not in self._pending and not self._queue.full():
            self._pending.add(key)
            self._queue.put_nowait(key)

    async def __aenter__(self):
        if self.client is None:
            from redis.asyncio import Redis
            self.client = Redis.from_url(self.url, socket_connect_timeout=1,
                socket_timeout=2, decode_responses=True)
        self._tasks = [asyncio.create_task(self._listen()), asyncio.create_task(self._publish())]
        return self

    async def __aexit__(self, *_):
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.aclose()
        self._pending.clear()

    async def _publish(self):
        while True:
            key = await self._queue.get()
            self._pending.discard(key)
            try:
                await self.client.publish(CHANNEL, json.dumps({'key': key}))
            except Exception:
                # No retry ledger: consumers' durable reconciliation is enough.
                # Do not log exception strings, which can contain Redis secrets.
                await asyncio.sleep(1)

    async def _listen(self):
        failed = False
        while True:
            try:
                async with self.client.pubsub() as subscription:
                    await subscription.subscribe(CHANNEL)
                    while True:
                        message = await subscription.get_message(timeout=1)
                        if message is None:
                            continue
                        if failed:
                            logger.info('Coordination notifications reconnected')
                            failed = False
                        if message.get('type') != 'message':
                            continue
                        raw = message.get('data')
                        if not isinstance(raw, (str, bytes)) or len(raw) > 512:
                            continue
                        try:
                            value = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if (isinstance(value, dict) and isinstance(value.get('key'), str)
                                and 0 < len(value['key']) <= 256):
                            self.signal_local(value['key'])
            except Exception:
                if not failed:
                    logger.warning('Coordination notifications unavailable; durable polling remains active')
                    failed = True
                await asyncio.sleep(1)
