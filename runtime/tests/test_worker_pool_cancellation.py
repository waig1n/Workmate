"""Real PostgreSQL Worker transactions must join bounded cancellation cleanup."""
import asyncio
import pytest
from sqlalchemy.ext.asyncio import AsyncTransaction,AsyncConnection
from test_execution_context import database
from workmate_runtime.transport import product_pool as module


async def test_repeated_cancellation_joins_outer_rollback_before_return(database,monkeypatch):
    sessions,admin=database;engine=sessions.kw['bind'];pool=module.ProductWorkerPool(engine)
    entered=asyncio.Event();release=asyncio.Event();finished=asyncio.Event()
    original=AsyncTransaction.__aexit__
    async def delayed(self,*args):
        entered.set();await release.wait()
        try:return await original(self,*args)
        finally:finished.set()
    monkeypatch.setattr(AsyncTransaction,'__aexit__',delayed)
    async def work():
        async with pool.acquire() as db:
            await db.execute("UPDATE tasks SET title='never committed' WHERE id='t'")
            raise ValueError('rollback fixture')
    task=asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(),3)
        for _ in range(3):
            task.cancel();await asyncio.sleep(.02)
        assert not task.done(),'Caller returned while transaction cleanup was unfinished'
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,3)
        assert finished.is_set() and engine.pool.checkedout()==0
        assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='Task'
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True)


async def test_cleanup_deadline_invalidates_connection_and_does_not_publish(database,monkeypatch):
    sessions,admin=database;engine=sessions.kw['bind']
    from workmate_product.coordination import WakeupBus
    bus=WakeupBus('unused');pool=module.ProductWorkerPool(engine,wakeup_bus=bus)
    original_exit=AsyncTransaction.__aexit__;original_invalidate=AsyncConnection.invalidate
    invalidated=[]
    async def stalled(self,*args):await asyncio.Event().wait()
    async def invalidate(self,*args,**kwargs):
        invalidated.append(True);return await original_invalidate(self,*args,**kwargs)
    monkeypatch.setattr(module,'_CLEANUP_SECONDS',.05,raising=False)
    monkeypatch.setattr(AsyncTransaction,'__aexit__',stalled)
    monkeypatch.setattr(AsyncConnection,'invalidate',invalidate)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(2):
                async with pool.acquire() as db:
                    await db.execute("UPDATE tasks SET title='never committed' WHERE id='t'")
                    db.stage_wakeup('never-publish')
        assert invalidated,'Timed-out transaction cleanup must invalidate its connection'
        assert engine.pool.checkedout()==0 and bus._queue.empty()
        assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='Task'
    finally:monkeypatch.setattr(AsyncTransaction,'__aexit__',original_exit)


async def test_cancel_running_query_leaves_no_transaction_or_checkout(database):
    sessions,admin=database;engine=sessions.kw['bind'];pool=module.ProductWorkerPool(engine)
    entered=asyncio.Event();pids=[]
    async def work():
        async with pool.acquire() as db:
            pids.append(await db.fetchval('SELECT pg_backend_pid()'))
            await db.execute("UPDATE tasks SET title='never committed' WHERE id='t'")
            entered.set();await db.execute('SELECT pg_sleep(30)')
    task=asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(),3)
        async with asyncio.timeout(3):
            while not await admin.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid=$1 AND query='SELECT pg_sleep(30)'",pids[0]):
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,8)
        assert engine.pool.checkedout()==0
        assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='Task'
        assert not await admin.fetchval('SELECT count(*) FROM pg_stat_activity WHERE pid=$1 AND xact_start IS NOT NULL',pids[0])
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


async def test_ambiguous_outer_commit_does_not_publish_or_replay(database,monkeypatch):
    sessions,admin=database;engine=sessions.kw['bind']
    from workmate_product.coordination import WakeupBus
    bus=WakeupBus('unused');pool=module.ProductWorkerPool(engine,wakeup_bus=bus)
    original=AsyncTransaction.__aexit__
    async def lost_receipt(self,*args):
        await original(self,*args)
        raise ConnectionError('commit acknowledgement lost')
    monkeypatch.setattr(AsyncTransaction,'__aexit__',lost_receipt)
    with pytest.raises(ConnectionError,match='acknowledgement lost'):
        async with pool.acquire() as db:
            await db.execute("UPDATE tasks SET title='committed once' WHERE id='t'")
            db.stage_wakeup('unknown-result')
    assert bus._queue.empty() and engine.pool.checkedout()==0
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='committed once'


async def test_cancel_during_successful_commit_joins_and_publishes_after_commit(database,monkeypatch):
    sessions,admin=database;engine=sessions.kw['bind']
    from workmate_product.coordination import WakeupBus
    bus=WakeupBus('unused');pool=module.ProductWorkerPool(engine,wakeup_bus=bus)
    entered=asyncio.Event();release=asyncio.Event();original=AsyncTransaction.__aexit__
    async def delayed(self,*args):
        entered.set();await release.wait();return await original(self,*args)
    monkeypatch.setattr(AsyncTransaction,'__aexit__',delayed)
    async def work():
        async with pool.acquire() as db:
            await db.execute("UPDATE tasks SET title='committed once' WHERE id='t'")
            db.stage_wakeup('known-commit')
    task=asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(),3)
        task.cancel();await asyncio.sleep(.01)
        assert not task.done() and bus._queue.empty()
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,3)
        assert bus._pending=={'known-commit'} and engine.pool.checkedout()==0
        assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='committed once'
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True)


async def test_cancel_racing_successful_startup_closes_unhanded_connection(database,monkeypatch):
    sessions,admin=database;engine=sessions.kw['bind'];pool=module.ProductWorkerPool(engine)
    original=AsyncConnection.get_raw_connection;caller=None;entered=[]
    async def ready_then_cancel(self):
        result=await original(self)
        caller.cancel()
        return result
    monkeypatch.setattr(AsyncConnection,'get_raw_connection',ready_then_cancel)
    async def work():
        async with pool.acquire():entered.append(True)
    caller=asyncio.create_task(work())
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(caller,3)
    assert entered==[] and engine.pool.checkedout()==0
    assert await admin.fetchval('SELECT 42')==42


@pytest.mark.parametrize('stage',['begin','SELECT 1'])
async def test_startup_failure_preserves_error_and_returns_connection(database,monkeypatch,stage):
    sessions,admin=database;engine=sessions.kw['bind'];pool=module.ProductWorkerPool(engine)
    if stage=='begin':
        async def broken(self):raise ValueError('begin fixture failed')
        monkeypatch.setattr(AsyncConnection,'begin',broken)
    else:
        async def broken(self,*args,**kwargs):raise ValueError('SELECT 1 fixture failed')
        monkeypatch.setattr(AsyncConnection,'exec_driver_sql',broken)
    with pytest.raises(ValueError,match=stage+' fixture failed'):
        async with pool.acquire():raise AssertionError('Must not expose failed startup')
    assert engine.pool.checkedout()==0
    assert await admin.fetchval('SELECT 42')==42


async def test_successful_transaction_inside_handled_exception_commits(database):
    sessions,admin=database
    pool=module.ProductWorkerPool(sessions.kw['bind'])
    try:
        raise ValueError('already handled preparation failure')
    except ValueError:
        async with pool.acquire() as db:
            await db.execute("UPDATE tasks SET title='settled' WHERE id='t'")
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='settled'
