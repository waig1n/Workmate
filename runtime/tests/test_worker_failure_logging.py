"""Quarantined Worker sessions log identities and types without exception secrets."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from workmate_runtime.execution.service import WorkerBindings
from polling_worker import WorkerService


async def noop(*args):
    pass


@pytest.mark.parametrize('report_fails', [False, True])
async def test_optional_failure_report_is_best_effort_and_never_clears_quarantine(caplog, report_fails):
    calls = []
    async def broken(*args):
        raise ValueError('private original exception')
    async def report(**context):
        assert worker.failures == {'session': 'ValueError'}
        calls.append(context)
        if report_fails:
            raise OSError('private database credentials')
    worker = WorkerService(None, 'node', WorkerBindings(noop, noop, noop, noop, noop,
        report_failure=report), controller=SimpleNamespace(start=broken), capacity=1)
    worker.ready.set()
    await worker._slots.acquire()
    with caplog.at_level(logging.ERROR, logger='workmate_runtime.execution.service'):
        await worker._session('command', 'session', task_id='task')
    assert calls == [{'command_id': 'command', 'session_id': 'session', 'task_id': 'task',
        'worker_id': worker.worker_id, 'storage_node_id': 'node',
        'error_type': 'ValueError', 'cause_type': None}]
    assert worker.failures == {'session': 'ValueError'}
    assert worker.ready.is_set() and worker.available_capacity == 1
    assert 'private' not in caplog.text
    assert ('OSError' in caplog.text) == report_fails
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize('chaining', ['explicit', 'implicit', 'none'])
@pytest.mark.parametrize('kind', ['StartTask', 'StopTask'])
async def test_quarantine_logs_safe_context_without_changing_readiness_or_capacity(caplog, chaining, kind):
    calls = []
    async def broken(*args):
        calls.append(args)
        if chaining == 'none':
            raise RuntimeError('outer-secret tool arguments and model input')
        try:
            raise ValueError('inner-secret token=credential')
        except ValueError as cause:
            if chaining == 'explicit':
                raise RuntimeError('outer-secret tool arguments and model input') from cause
            raise RuntimeError('outer-secret tool arguments and model input')
    controller = SimpleNamespace(start=broken, stop_unclaimed=broken)
    worker = WorkerService(None, 'storage-node', WorkerBindings(noop, noop, noop, noop, noop),
                           controller=controller, capacity=1)
    worker.ready.set()
    if kind == 'StartTask':
        await worker._slots.acquire()
    with caplog.at_level(logging.ERROR, logger='workmate_runtime.execution.service'):
        await worker._session('command-id', 'session-id', kind, 'unclaimed', task_id='task-id')
    assert worker.failures == {'session-id': 'RuntimeError'}
    assert worker.ready.is_set()
    assert worker.available_capacity == 1
    assert len(calls) == 1
    records = [record for record in caplog.records if record.name == 'workmate_runtime.execution.service']
    assert len(records) == 1
    message = records[0].getMessage()
    for value in ('command-id', 'session-id', 'task-id', 'RuntimeError', kind):
        assert value in message
    assert ('ValueError' in message) == (chaining != 'none')
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    for private in ('outer-secret', 'inner-secret', 'credential', 'tool arguments', 'model input'):
        assert private not in caplog.text


async def test_session_cancellation_is_not_reported_as_quarantine(caplog):
    async def cancelled(*args):
        raise asyncio.CancelledError()
    worker = WorkerService(None, 'node', WorkerBindings(noop, noop, noop, noop, noop),
                           controller=SimpleNamespace(start=cancelled), capacity=1)
    await worker._slots.acquire()
    with caplog.at_level(logging.ERROR, logger='workmate_runtime.execution.service'):
        with pytest.raises(asyncio.CancelledError):
            await worker._session('command', 'session', task_id='task')
    assert worker.failures == {}
    assert worker.available_capacity == 1
    assert not caplog.records


@pytest.mark.parametrize('kind', ['StartTask', 'StopTask'])
async def test_scheduler_passes_discovered_task_identity_to_failure_log(caplog, kind):
    shutdown = asyncio.Event()
    async def broken(*args):
        shutdown.set()
        raise LookupError('private scheduler failure')
    worker = WorkerService(None, 'node', WorkerBindings(noop, noop, noop, noop, noop),
        controller=SimpleNamespace(start=broken, stop_unclaimed=broken), drain_seconds=1)
    async def empty():
        return ()
    async def commands():
        return ({'id': 'discovered-command', 'session_id': 'discovered-session',
                 'task_id': 'discovered-task', 'kind': kind, 'stop_phase': 'unclaimed'},)
    worker._inspect = empty
    worker._discover = commands if kind == 'StartTask' else empty
    worker._discover_terminal_stops = commands if kind == 'StopTask' else empty
    with caplog.at_level(logging.ERROR, logger='workmate_runtime.execution.service'):
        await asyncio.wait_for(worker.serve(shutdown), 2)
    assert worker.failures == {'discovered-session': 'LookupError'}
    assert 'task_id=discovered-task' in caplog.text
    assert 'private scheduler failure' not in caplog.text
    assert not worker.ready.is_set()
    assert not worker.active_sessions
