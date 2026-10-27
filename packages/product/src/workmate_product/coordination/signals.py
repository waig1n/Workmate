"""Process-local broadcast and bounded waits, independent of transport adapters.

Version snapshots taken before reading durable facts prevent a notification
between the read and wait from being lost. Timeout fallback remains necessary.
"""
import asyncio
from contextlib import suppress

FALLBACK_SECONDS = 1.0


class Pulse:
    def __init__(self):
        self.version = 0
        self.event = asyncio.Event()
        self.users = 0

    def signal(self):
        self.version += 1
        previous, self.event = self.event, asyncio.Event()
        previous.set()

    async def wait(self, version, timeout, shutdown=None):
        if self.version != version:
            return
        event = self.event
        tasks = [asyncio.create_task(event.wait())]
        if shutdown is not None:
            tasks.append(asyncio.create_task(shutdown.wait()))
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def wait_for_wakeup(pulse, version, timeout, shutdown=None):
    if pulse is not None:
        await pulse.wait(version, timeout, shutdown)
    elif shutdown is not None:
        with suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout)
    else:
        await asyncio.sleep(timeout)
