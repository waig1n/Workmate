"""Cancellation during pre-ping must not orphan an unassigned ORM connection."""
import asyncio
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from test_execution_context import database
from test_message_snapshots import running
from test_realtime_store import setup
from test_realtime_writer import start, delta


@pytest.mark.parametrize('method',['restore','read_business_seq','save_snapshot'])
async def test_repeated_cancel_during_checkout_joins_single_driver_cleanup(monkeypatch,method,database):
    sessions,admin=database
    await running(admin)
    engine=sessions.kw['bind']
    engine.sync_engine.pool._pre_ping=True
    guard,factory=setup(sessions)
    writer=await factory(guard,None)
    drivers=[]
    async with engine.connect() as connection:
        drivers.append((await connection.get_raw_connection()).driver_connection)
    entered=asyncio.Event();closing=asyncio.Event();release=asyncio.Event();closed=asyncio.Event()
    cancellation_counts=[]
    original_close=AsyncAdapt_asyncpg_connection._terminate_graceful_close
    async def blocked_ping(self):
        entered.set()
        try:await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_counts.append(asyncio.current_task().cancelling())
            raise
    async def slow_close(self):
        closing.set()
        try:
            await release.wait();await original_close(self)
        finally:closed.set()
    monkeypatch.setattr(AsyncAdapt_asyncpg_connection,'_async_ping',blocked_ping)
    monkeypatch.setattr(AsyncAdapt_asyncpg_connection,'_terminate_graceful_close',slow_close)
    await writer.accept([start(),delta('content')])
    snapshot=writer._writer.accumulator.capture('approval',None,0,unanchored=True)
    operation=(factory(guard,None) if method=='restore' else
        writer._writer.read_business_seq() if method=='read_business_seq' else
        writer._writer.save_snapshot(snapshot,unanchored=True))
    baseline=set(asyncio.all_tasks());task=asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(),3)
        task.cancel();await asyncio.wait_for(closing.wait(),3)
        for _ in range(3):task.cancel();await asyncio.sleep(.01)
        assert not task.done(),'Second cancellation interrupted SQLAlchemy checkout invalidation'
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,5)
        assert closed.is_set() and engine.pool.checkedout()==0
        assert all(driver.is_closed() for driver in drivers)
        assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==0
        assert cancellation_counts==[1]
        await asyncio.sleep(0)
        assert not {t for t in asyncio.all_tasks()-baseline if t is not task and not t.done()}
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True)
        for driver in drivers:
            if not driver.is_closed():driver.terminate()
        await writer.close()
