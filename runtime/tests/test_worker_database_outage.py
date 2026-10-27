"""Transient PG connection failures defer transport without granting execution."""
import asyncio
from types import SimpleNamespace

import asyncpg
import pytest

from workmate_runtime.execution.service import WorkerService
from workmate_runtime.execution.workspace_lifecycle import WorkerWorkspaceLifecycle


@pytest.mark.parametrize('phase', ['settle', 'poll', 'heartbeat', 'release'])
@pytest.mark.parametrize('error_type', [
    asyncpg.CannotConnectNowError, asyncpg.ConnectionDoesNotExistError,
    asyncpg.ConnectionFailureError,
])
async def test_database_unavailable_defers_background_work(phase, error_type):
    stop = asyncio.Event()
    calls = 0

    async def operation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error_type('temporary database outage')
        stop.set()
        return []

    async def noop(*args, **kwargs):
        return []

    if phase in {'settle', 'poll'}:
        source = SimpleNamespace(settle=noop, poll=noop)
        setattr(source, phase, operation)
        worker = WorkerService(None, 'node', None, controller=None,
                               command_source=source, drain_seconds=0)
        worker._inspect = noop
        await asyncio.wait_for(worker.serve(stop), 2)
        assert not worker.active_sessions and not worker.failures
    else:
        service = SimpleNamespace(node_id='node', worker_id='worker', lease_seconds=.03)
        loop = WorkerWorkspaceLifecycle(None, service, None, None, None, capacity=1)
        if phase == 'heartbeat':
            loop.heartbeat = operation
            await asyncio.wait_for(loop._heartbeats(stop, asyncio.Event()), 2)
        else:
            loop.release_tick = operation
            loop.repair_releases = noop
            await asyncio.wait_for(loop._releases(stop), 2)
    assert calls == 2


async def test_database_programming_error_is_not_retried():
    calls = 0

    async def failed():
        nonlocal calls
        calls += 1
        raise asyncpg.UndefinedTableError('schema mismatch')

    async def empty():
        return []

    worker = WorkerService(None, 'node', None, controller=None,
        command_source=SimpleNamespace(settle=failed), drain_seconds=0)
    worker._inspect = empty
    with pytest.raises(asyncpg.UndefinedTableError):
        await worker.serve(asyncio.Event())
    assert calls == 1


async def test_shutdown_does_not_wait_for_database_recovery():
    entered = asyncio.Event()

    async def unavailable():
        entered.set()
        raise asyncpg.CannotConnectNowError('temporary database outage')

    async def empty():
        return []

    worker = WorkerService(None, 'node', None, controller=None,
        command_source=SimpleNamespace(settle=unavailable), drain_seconds=0)
    worker._inspect = empty
    stop = asyncio.Event()
    running = asyncio.create_task(worker.serve(stop))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        stop.set()
        await asyncio.wait_for(running, 1)
        assert not worker.ready.is_set() and not worker.active_sessions
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_rejection_failure_does_not_mark_session_or_command_settled():
    calls = []

    async def rejected(command_id, generation):
        calls.append((command_id, generation))
        raise asyncpg.CannotConnectNowError('temporary database outage')

    worker = WorkerService(None, 'node', None, controller=None,
        command_source=SimpleNamespace(rejected=rejected), drain_seconds=0)
    await worker._remember_unclaimed('command', 3)
    assert calls == [('command', 3)]
    assert not worker._unclaimed_commands and not worker.failures
