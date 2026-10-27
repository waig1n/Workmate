"""One connection for raw Worker transport and shared Product ORM transactions.

SQLAlchemy owns the outer transaction. Transport transaction() blocks use
asyncpg savepoints inside it. Product sessions join without committing that outer
transaction. The role composition uses this pool; no independent authorization
connection can race an execution claim while its rows are locked.
"""
from contextlib import asynccontextmanager
import asyncio
import json
import sys
import anyio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


# Cleanup bounds only: tool/object publication may legitimately hold a shared
# exclusion transaction longer, so no global transaction or idle timeout here.
_CLEANUP_SECONDS = 5
_INVALIDATE_SECONDS = 3


async def _finish(connection, transaction, exception, committed):
    try:
        async with asyncio.timeout(_CLEANUP_SECONDS):
            if transaction is not None:
                await transaction.__aexit__(*exception)
                if exception[0] is None:
                    committed()
            # Calling close directly avoids AsyncConnection.__aexit__ spawning
            # another shielded task that could outlive our cleanup deadline.
            await connection.close()
    except BaseException:
        async with asyncio.timeout(_INVALIDATE_SECONDS):
            await connection.invalidate()
            await connection.close()
        raise


async def _join_cleanup(connection, transaction, exception, committed):
    task = asyncio.create_task(_finish(connection, transaction, exception, committed))
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    return cancelled


async def _open_connection(engine):
    """Pre-ping owns its connection before acquire() can receive the handle.

    Relay cancellation once during that gap. A late successful result still
    belongs to this operation and must be closed if its caller was cancelled.
    """
    async def start():
        connection = transaction = None
        with anyio.CancelScope(shield=True):
            try:
                connection = await engine.connect()
                transaction = await connection.begin()
                await connection.exec_driver_sql('SELECT 1')
                raw = await connection.get_raw_connection()
                return connection, transaction, raw.driver_connection
            except BaseException:
                if connection is not None:
                    await _join_cleanup(connection, transaction, sys.exc_info(), lambda: None)
                raise
    task = asyncio.create_task(start())
    try:
        return await asyncio.shield(task)
    except BaseException:
        exception = sys.exc_info()
        with anyio.CancelScope(shield=True):
            if not task.done():
                task.cancel()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    break
            if not task.cancelled():
                connection, transaction, _ = task.result()
                await _join_cleanup(connection, transaction, exception, lambda: None)
        raise


class ProductConnection:
    def __init__(self, connection, driver):
        self._connection,self._driver=connection,driver
        self._active=True
        self.wakeups=set()

    def stage_wakeup(self, key):
        self.wakeups.add(key)

    @asynccontextmanager
    async def transaction(self):
        if not self._active:
            raise RuntimeError('Worker connection is closed')
        before = self.wakeups.copy()
        try:
            async with self._driver.transaction():
                yield
        except BaseException:
            self.wakeups = before
            raise

    def __getattr__(self, name):
        if not self._active or name.startswith('_') or name not in {
                'execute','executemany','fetch','fetchrow','fetchval','transaction','is_in_transaction'}:
            raise RuntimeError('Worker connection is closed or operation is not supported')
        return getattr(self._driver,name)

    async def _fetch(self, query, args, *, timeout=None):
        if not self._active:
            raise RuntimeError('Worker connection is closed')
        statement=await self._driver.prepare(query,timeout=timeout)
        attributes=statement.get_attributes()
        records=await statement.fetch(*args,timeout=timeout)
        return [dict((attribute.name,json.dumps(row[index],ensure_ascii=False)
                      if attribute.type.name in {'json','jsonb'} and row[index] is not None else row[index])
                     for index,attribute in enumerate(attributes)) for row in records]

    async def fetch(self, query, *args, timeout=None):
        return await self._fetch(query,args,timeout=timeout)

    async def fetchrow(self, query, *args, timeout=None):
        rows=await self._fetch(query,args,timeout=timeout)
        return rows[0] if rows else None

    async def fetchval(self, query, *args, column=0, timeout=None):
        row=await self.fetchrow(query,*args,timeout=timeout)
        return list(row.values())[column] if row is not None else None

    @asynccontextmanager
    async def product_session(self):
        if not self._active:
            raise RuntimeError('Worker connection is closed')
        async with AsyncSession(bind=self._connection,expire_on_commit=False,
                join_transaction_mode='rollback_only') as session:
            async with session.begin():
                yield session


class ProductWorkerPool:
    def __init__(self,engine,*,wakeup_bus=None):
        if not isinstance(engine,AsyncEngine) or engine.dialect.name!='postgresql' or engine.dialect.driver!='asyncpg':
            raise TypeError('Worker requires a PostgreSQL asyncpg SQLAlchemy engine')
        self.engine=engine
        self.wakeup_bus=wakeup_bus

    @asynccontextmanager
    async def acquire(self):
        connection = transaction = borrowed = None
        # sys.exc_info() in finally can contain the caller's handled error.
        # Only an exception escaping this context may roll it back.
        exception = (None, None, None)
        def committed():
            # Only after the actual outer commit, never after a savepoint or an
            # unknown commit result. Wakeups are lossy hints, not execution rights.
            if self.wakeup_bus is not None and borrowed is not None:
                for key in borrowed.wakeups:
                    self.wakeup_bus.publish_nowait(key)
        try:
            # Start the dialect-managed driver transaction before exposing
            # raw asyncpg; otherwise a later ORM query tries a second BEGIN.
            connection, transaction, driver = await _open_connection(self.engine)
            borrowed = ProductConnection(connection, driver)
            yield borrowed
        except BaseException:
            exception = sys.exc_info()
            raise
        finally:
            if borrowed is not None:
                borrowed._active = False
            if connection is not None:
                cancelled = await _join_cleanup(connection, transaction, exception, committed)
                if cancelled:
                    raise asyncio.CancelledError
