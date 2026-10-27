"""Worker loop uses durable discovery and releases capacity while parked."""
import asyncio

import pytest
from agentscope.message import TextBlock, ToolCallBlock

from test_postgres_execution_store import database
from test_execution_controller import prepare_reply
from test_core_engine import ConfirmTool
from test_resume_controller import prepare_resume, allow
from test_parked_stop import stop_intent, quiescent
from workmate_runtime.execution.controller import ExecutionController
from workmate_runtime.execution.service import WorkerBindings
from polling_worker import WorkerService


async def wait_until(check):
    async with asyncio.timeout(8):
        while not await check():
            await asyncio.sleep(0.02)


def bindings(start, tool=None):
    return WorkerBindings(start, prepare_resume(tool or ConfirmTool()),
                          prepare_resume(tool or ConfirmTool()), allow, quiescent)


async def test_worker_discovers_start_and_stop_without_notifications(database):
    pool, db = database
    tool = ConfirmTool()
    start, calls = prepare_reply([[ToolCallBlock(id='a', name=tool.name, input='{}')]], tool=tool)
    worker = WorkerService(pool, 'node', bindings(start, tool), capacity=1,
                           controller=ExecutionController(pool, lease_seconds=2), lease_seconds=2)
    shutdown = asyncio.Event()
    loop = asyncio.create_task(worker.serve(shutdown))
    async def parked():
        return await db.fetchval('SELECT status FROM runtime_sessions') == 'awaiting_interaction'
    try:
        await wait_until(parked)
        await asyncio.sleep(2.2)
        assert worker.available_capacity == 1
        await stop_intent(db)
        async def stopped():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'") == 'completed'
        await wait_until(stopped)
        assert len(calls) == 1 and tool.calls == 0
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'
    finally:
        shutdown.set()
        await asyncio.wait_for(loop, 5)


async def test_worker_shutdown_joins_active_execution_and_stops_admission(database):
    pool, db = database
    entered = asyncio.Event()
    start, calls = prepare_reply([], entered=entered)
    worker = WorkerService(pool, 'node', bindings(start), capacity=1,
                           controller=ExecutionController(pool), drain_seconds=0.05)
    shutdown = asyncio.Event()
    loop = asyncio.create_task(worker.serve(shutdown))
    await asyncio.wait_for(entered.wait(), 5)
    shutdown.set()
    await asyncio.wait_for(loop, 5)
    assert not worker.active_sessions and worker.available_capacity == 1
    assert len(calls) == 1
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'claimed'


async def test_worker_records_failed_session_without_retrying_tools(database):
    pool, db = database
    calls = []
    async def broken(*args):
        calls.append(1)
        raise RuntimeError('fixture failure')
    worker = WorkerService(pool, 'node', bindings(broken), capacity=1,
                           controller=ExecutionController(pool))
    shutdown = asyncio.Event()
    loop = asyncio.create_task(worker.serve(shutdown))
    try:
        async def failed():
            return bool(worker.failures)
        await wait_until(failed)
        await asyncio.sleep(0.3)
        assert calls == [1]
        assert 'session' in worker.failures
        assert worker.available_capacity == 1
    finally:
        shutdown.set()
        await asyncio.wait_for(loop, 5)


@pytest.mark.parametrize('capacity', [0, -1, True, 1.5])
def test_worker_rejects_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        WorkerService(None, 'node', None, capacity=capacity, controller=None)


async def add_second_session(db):
    await db.execute("INSERT INTO conversations(id,user_id,agent_id,active_product_task_id) VALUES ('conversation-b','user','agent','task-b')")
    await db.execute("UPDATE tasks SET conversation_id='conversation-b' WHERE id='task-b'")
    await db.execute("INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('session-b','conversation-b','node')")
    await db.execute("INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task-b','session-b')")
    await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
        kind,expected_epoch,payload,content_digest) VALUES
        ('command-b','session-b','conversation-b','task-b','user','StartTask',0,'{}',repeat('d',64))""")


async def test_parked_session_does_not_starve_another_conversation(database):
    pool, db = database
    tool = ConfirmTool()
    first, _ = prepare_reply([[ToolCallBlock(id='a',name=tool.name,input='{}')]],tool=tool)
    second, calls = prepare_reply([[TextBlock(text='independent')]])
    async def start(owner, *args):
        return await (first if owner.task_id == 'task' else second)(owner,*args)
    worker = WorkerService(pool,'node',bindings(start,tool),capacity=1,
        controller=ExecutionController(pool,lease_seconds=2),lease_seconds=2,drain_seconds=0)
    shutdown = asyncio.Event()
    loop = asyncio.create_task(worker.serve(shutdown))
    try:
        async def parked():
            return await db.fetchval("SELECT status FROM runtime_sessions WHERE id='session'") == 'awaiting_interaction'
        await wait_until(parked)
        await add_second_session(db)
        async def finished():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='command-b'") == 'completed'
        await wait_until(finished)
        assert len(calls) == 1 and tool.calls == 0
        assert await db.fetchval("SELECT status FROM runtime_sessions WHERE id='session'") == 'awaiting_interaction'
    finally:
        shutdown.set()
        await asyncio.wait_for(loop,5)


async def test_two_worker_loops_execute_one_durable_start_once(database):
    pool, db = database
    start, calls = prepare_reply([[TextBlock(text='once')]])
    workers = [WorkerService(pool,'node',bindings(start),capacity=1,
        controller=ExecutionController(pool),drain_seconds=0) for _ in range(2)]
    assert workers[0].worker_id != workers[1].worker_id
    shutdown = asyncio.Event()
    loops = [asyncio.create_task(w.serve(shutdown)) for w in workers]
    try:
        async def finished():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'completed'
        await wait_until(finished)
        assert len(calls) == 1
    finally:
        shutdown.set()
        await asyncio.wait_for(asyncio.gather(*loops),5)


async def test_startup_quarantines_expired_work_but_serves_other_sessions(database):
    from test_postgres_execution_store import claim
    pool, db = database
    await claim(pool)
    await db.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 second'")
    await add_second_session(db)
    start, calls = prepare_reply([[TextBlock(text='healthy')]])
    worker = WorkerService(pool,'node',bindings(start),controller=ExecutionController(pool),drain_seconds=0)
    shutdown = asyncio.Event()
    loop = asyncio.create_task(worker.serve(shutdown))
    try:
        async def finished():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='command-b'") == 'completed'
        await wait_until(finished)
        assert len(calls) == 1 and calls[0].task_id == 'task-b'
        assert worker.failures['session'].startswith('blocked_')
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'claimed'
    finally:
        shutdown.set()
        await asyncio.wait_for(loop,5)


@pytest.mark.parametrize('phase', ['startup_acquire','startup_fetch','discovery'])
async def test_shutdown_cancels_and_joins_blocked_discovery(phase):
    from contextlib import asynccontextmanager
    entered, exited = asyncio.Event(), asyncio.Event()
    async def block():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            if phase == 'startup_acquire':
                await block()
            yield self
        async def fetch(self, *args):
            await block()
    worker = WorkerService(Pool(),'node',None,controller=None,drain_seconds=0)
    if phase == 'discovery':
        async def empty():
            return ()
        worker._inspect = empty
    shutdown = asyncio.Event()
    loop = asyncio.create_task(worker.serve(shutdown))
    await asyncio.wait_for(entered.wait(),1)
    shutdown.set()
    await asyncio.wait_for(loop,1)
    assert exited.is_set() and not worker.active_sessions


async def test_unclaimed_command_does_not_quarantine_future_task(database):
    pool, db = database
    class Loser:
        async def start(self, *args):
            return None
    worker = WorkerService(pool,'node',bindings(None),controller=Loser(),drain_seconds=0)
    await worker._slots.acquire()
    await worker._session('command','session')
    assert 'session' not in worker.failures
    assert await worker._discover() == []
    # Replacing an unclaimed admission is Product's responsibility. The loop
    # must recognize its new command identity after that transaction completes.
    await db.execute("UPDATE runtime_commands SET status='rejected' WHERE id='command'")
    await db.execute("UPDATE conversations SET active_product_task_id='task-b'")
    await db.execute("INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task-b','session')")
    await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
        kind,expected_epoch,payload,content_digest) VALUES
        ('replacement','session','conversation','task-b','user','StartTask',0,'{}',repeat('d',64))""")
    assert [row['id'] for row in await worker._discover()] == ['replacement']


@pytest.mark.parametrize('parked',[False,True])
async def test_worker_finalizes_only_after_terminal_execution(database,parked):
    from dataclasses import replace
    pool,db=database
    tool=ConfirmTool()
    start,_=prepare_reply([[ToolCallBlock(id='a',name=tool.name,input='{}')]] if parked else [[TextBlock(text='done')]],tool=tool)
    finalized=[]
    async def finish(result):
        assert result.status in {'completed','interrupted'}
        assert await db.fetchval('SELECT count(*) FROM runtime_snapshots')>=1
        finalized.append(result)
    worker=WorkerService(pool,'node',replace(bindings(start,tool),finalize_result=finish),
        capacity=1,controller=ExecutionController(pool),drain_seconds=0)
    shutdown=asyncio.Event()
    loop=asyncio.create_task(worker.serve(shutdown))
    try:
        if parked:
            async def waiting():
                return await db.fetchval('SELECT status FROM runtime_sessions')=='awaiting_interaction'
            await wait_until(waiting)
            assert finalized==[]
            await stop_intent(db)
        async def finished():
            return len(finalized)==1
        await wait_until(finished)
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    finally:
        shutdown.set()
        await asyncio.wait_for(loop,5)


async def test_notification_discovery_avoids_idle_scans_and_recovers_lost_hints(monkeypatch):
    from workmate_product.coordination import WakeupBus
    import polling_worker as module
    monkeypatch.setattr(module, 'FALLBACK_SECONDS', .2)
    bus = WakeupBus('unused')
    scanned = asyncio.Queue()

    class IdleWorker(WorkerService):
        async def _inspect(self):
            return ()

        async def _discover(self):
            scanned.put_nowait(True)
            return []

        async def _discover_terminal_stops(self):
            return []

    worker = IdleWorker(None, 'node', None, controller=None, wakeup_bus=bus)
    shutdown = asyncio.Event()
    running = asyncio.create_task(worker.serve(shutdown))
    try:
        await asyncio.wait_for(scanned.get(), .5)
        await asyncio.sleep(.03)
        assert scanned.empty()
        bus.publish_nowait('commands')
        await asyncio.wait_for(scanned.get(), .1)
        # No second hint: the durable reconciliation scan must still happen.
        await asyncio.wait_for(scanned.get(), .4)
    finally:
        shutdown.set()
        await asyncio.wait_for(running, .5)
    assert not bus._pulses
