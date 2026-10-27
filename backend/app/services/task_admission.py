"""Bound outstanding work using a short, shared database admission transaction."""

import asyncio
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from fastapi import HTTPException
from workmate_product.application.task_capacity import TaskCapacityPolicy, TaskCapacityExceeded
from workmate_product.persistence.repositories.task_capacity import lock_capacity

from ..config import get_settings

_sqlite_locks = WeakKeyDictionary()


@asynccontextmanager
async def task_admission(db, user_id):
    """Hold until the caller commits Task + message; no Runtime I/O here.

    PostgreSQL coordinates all API processes. SQLite is a single-process test
    backend; its local lock is not a distributed deployment guarantee.
    """
    lock = None
    if db.bind.dialect.name == "postgresql":
        await lock_capacity(db)
    else:
        loop = asyncio.get_running_loop()
        lock = _sqlite_locks.setdefault(loop, asyncio.Lock())
        await lock.acquire()
    try:
        try:
            await TaskCapacityPolicy.from_settings(get_settings()).check(db, user_id)
        except TaskCapacityExceeded as error:
            raise HTTPException(
                status_code=429,
                detail=str(error),
                headers={"Retry-After": "5"},
            ) from error
        yield
    except BaseException:
        await db.rollback()
        raise
    finally:
        if lock is not None:
            lock.release()
