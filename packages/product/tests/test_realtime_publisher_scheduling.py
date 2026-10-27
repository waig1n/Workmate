"""Busy Conversations drain in order without paying a polling interval per event."""

import asyncio

import pytest

from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.realtime.business_publisher import BusinessPublisher


@pytest.mark.parametrize("outcome", ["empty", "failed"])
async def test_publisher_drains_ready_heads_and_waits_only_when_idle(
    monkeypatch, outcome
):
    shutdown = asyncio.Event()
    publisher = BusinessPublisher(None, None, None, CommandQueuePolicy())
    cycles, waits = [], []

    async def publish():
        cycles.append(len(cycles) + 1)
        if len(cycles) < 4:
            return 1
        if outcome == "failed":
            raise RuntimeError("transient storage failure")
        return 0

    async def idle_wait(awaitable, timeout):
        waits.append(len(cycles))
        shutdown.set()
        return await awaitable

    monkeypatch.setattr(publisher, "publish_once", publish)
    monkeypatch.setattr(asyncio, "wait_for", idle_wait)
    async with asyncio.timeout(2):
        await publisher.serve(shutdown)
    assert cycles == [1, 2, 3, 4]
    assert waits == [4], "Polling must not impose 200ms delay on every queued event"


async def test_busy_publisher_honors_shutdown_without_an_idle_cycle(monkeypatch):
    shutdown = asyncio.Event()
    publisher = BusinessPublisher(None, None, None, CommandQueuePolicy())
    cycles = []

    async def publish():
        cycles.append(1)
        if len(cycles) == 4:
            shutdown.set()
        return 1

    async def no_idle(awaitable, timeout):
        awaitable.close()
        pytest.fail("A busy publisher should not enter idle polling")

    monkeypatch.setattr(publisher, "publish_once", publish)
    monkeypatch.setattr(asyncio, "wait_for", no_idle)
    async with asyncio.timeout(2):
        await publisher.serve(shutdown)
    assert len(cycles) == 4
