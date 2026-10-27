"""Cancellation during pre-ping must not orphan an unassigned ORM connection."""
import asyncio
import os
from types import SimpleNamespace
import pytest
from sqlalchemy import text,event
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from workmate_runtime.execution.command_queue import CommandQueueSource


@pytest.mark.parametrize('method',['_classify','_quarantine','pool_acquire'])
async def test_repeated_cancel_during_checkout_joins_single_driver_cleanup(monkeypatch,method):
    dsn=os.environ.get('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:pytest.skip('isolated PostgreSQL required')
    engine=create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),pool_pre_ping=True,
        pool_size=1,max_overflow=0)
    drivers=[]
    @event.listens_for(engine.sync_engine,'connect')
    def connected(dbapi,record):drivers.append(dbapi.driver_connection)
    async with engine.connect() as connection:await connection.execute(text('SELECT 1'))
    entered=asyncio.Event();closing=asyncio.Event();release=asyncio.Event();closed=asyncio.Event()
    cancellation_counts=[];acks=[]
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
    async def classify(db,*args,**kwargs):
        await db.scalar(text('SELECT 42'));return SimpleNamespace(action='execute')
    async def quarantine(db,**kwargs):await db.scalar(text('SELECT 42'))
    async def ack(*args):acks.append(args)
    source=CommandQueueSource(SimpleNamespace(stream_key=lambda _: 'fixture',ack=ack),async_sessionmaker(engine))
    source.repository=SimpleNamespace(classify=classify,quarantine_message=quarantine)
    envelope=SimpleNamespace(command_id='fixture',delivery_generation=1)
    async def pool_operation():
        from workmate_runtime.transport.product_pool import ProductWorkerPool
        async with ProductWorkerPool(engine).acquire() as db:await db.fetchval('SELECT 42')
    operation=(pool_operation() if method=='pool_acquire' else source._classify(envelope) if method=='_classify' else
        source._quarantine('route',SimpleNamespace(envelope=envelope,message_id='1-0'),'fixture'))
    baseline=set(asyncio.all_tasks());task=asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(),3)
        task.cancel();await asyncio.wait_for(closing.wait(),3)
        for _ in range(3):task.cancel();await asyncio.sleep(.01)
        assert not task.done(),'Second cancellation interrupted SQLAlchemy checkout invalidation'
        release.set()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,5)
        assert closed.is_set() and engine.pool.checkedout()==0 and not acks
        assert cancellation_counts==[1]
        await asyncio.sleep(0)
        assert not {t for t in asyncio.all_tasks()-baseline if t is not task and not t.done()}
    finally:
        release.set();await asyncio.gather(task,return_exceptions=True)
        for driver in drivers:
            if not driver.is_closed():driver.terminate()
        await engine.dispose()
