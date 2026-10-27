"""Real PG deadlines protect command locks without changing execution rights."""
import asyncio
import os
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from workmate_runtime.execution.command_queue import CommandQueueSource


@pytest_asyncio.fixture
async def database():
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('isolated PostgreSQL required')
    import asyncpg
    admin = await asyncpg.connect(dsn)
    table = 'command_guard_' + uuid4().hex
    await admin.execute(f'CREATE TABLE {table} (id int primary key, value int)')
    await admin.execute(f'INSERT INTO {table} VALUES (1,0)')
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), admin, table
    finally:
        await engine.dispose()
        await admin.execute(f'DROP TABLE {table}')
        await admin.close()


def source(sessions, classify):
    result = CommandQueueSource(None, sessions)
    result.repository = SimpleNamespace(classify=classify)
    return result


ENVELOPE = SimpleNamespace(command_id='fixture', delivery_generation=1)


async def test_idle_command_transaction_released_by_server(database):
    sessions, admin, table = database
    locked = asyncio.Event()
    release = asyncio.Event()
    pids = []

    async def classify(db, *_args, **_kw):
        pids.append(await db.scalar(text('SELECT pg_backend_pid()')))
        await db.execute(text(f'UPDATE {table} SET value=1 WHERE id=1'))
        locked.set()
        await release.wait()  # Model a client that cannot send COMMIT/ROLLBACK.
        return SimpleNamespace(action='execute')

    task = asyncio.create_task(source(sessions, classify)._classify(ENVELOPE))
    try:
        await asyncio.wait_for(locked.wait(), 3)
        started = time.monotonic()
        async with asyncio.timeout(13):
            while await admin.fetchval('SELECT count(*) FROM pg_stat_activity WHERE pid=$1', pids[0]):
                await asyncio.sleep(.05)
        assert time.monotonic() - started < 13
        await admin.execute(f'UPDATE {table} SET value=2 WHERE id=1')
        release.set()
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(task, 5)
        assert await admin.fetchval(f'SELECT value FROM {table}') == 2
        assert sessions.kw['bind'].pool.checkedout() == 0
    finally:
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_control_settings_are_local_and_exception_rolls_back(database):
    sessions, admin, table = database
    async def classify(db, *_args, **_kw):
        assert await db.scalar(text('SHOW idle_in_transaction_session_timeout')) == '10s'
        assert await db.scalar(text('SHOW lock_timeout')) == '1s'
        await db.execute(text(f'UPDATE {table} SET value=1 WHERE id=1'))
        raise ValueError('fixture failure')
    with pytest.raises(ValueError, match='fixture failure'):
        await source(sessions, classify)._classify(ENVELOPE)
    assert await admin.fetchval(f'SELECT value FROM {table}') == 0
    async with sessions() as db:
        assert await db.scalar(text('SHOW idle_in_transaction_session_timeout')) == '0'
        assert await db.scalar(text('SHOW lock_timeout')) == '0'


async def test_command_lock_wait_is_bounded(database):
    sessions, admin, table = database
    async def classify(db, *_args, **_kw):
        await db.execute(text(f'UPDATE {table} SET value=1 WHERE id=1'))
        return SimpleNamespace(action='execute')
    async with admin.transaction():
        await admin.execute(f'SELECT * FROM {table} FOR UPDATE')
        async with asyncio.timeout(3):
            with pytest.raises(DBAPIError) as raised:
                await source(sessions, classify)._classify(ENVELOPE)
        assert raised.value.orig.sqlstate == '55P03'
    assert await admin.fetchval(f'SELECT value FROM {table}') == 0


@pytest.mark.parametrize('cancel_count', [1, 5])
async def test_cancel_active_query_returns_connection_and_does_not_commit(database, cancel_count):
    sessions, admin, table = database
    entered = asyncio.Event()
    pid = []
    async def classify(db, *_args, **_kw):
        pid.append(await db.scalar(text('SELECT pg_backend_pid()')))
        await db.execute(text(f'UPDATE {table} SET value=1 WHERE id=1'))
        entered.set()
        await db.execute(text('SELECT pg_sleep(30)'))
        return SimpleNamespace(action='execute')
    task = asyncio.create_task(source(sessions, classify)._classify(ENVELOPE))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with asyncio.timeout(3):
            while not await admin.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid=$1 AND query LIKE '%pg_sleep%'", pid[0]):
                await asyncio.sleep(.01)
        task.cancel()
        for _ in range(cancel_count - 1):
            await asyncio.sleep(.001)
            if not task.done():
                task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert sessions.kw['bind'].pool.checkedout() == 0
        assert await admin.fetchval(f'SELECT value FROM {table}') == 0
        assert not await admin.fetchval("SELECT count(*) FROM pg_stat_activity WHERE pid=$1 AND state='active'", pid[0])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
