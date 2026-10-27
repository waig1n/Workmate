"""Short queue-control transactions; never wrap tool or object-store I/O."""
import asyncio
import sys
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from sqlalchemy import text

from .cancellation import cancel_once as cancel_command_once


_CLEANUP_SECONDS = 5
_INVALIDATE_SECONDS = 3




async def _finish(stack, db, exception):
    try:
        async with asyncio.timeout(_CLEANUP_SECONDS):
            await stack.__aexit__(*exception)
    except BaseException:
        if db is not None:
            async with asyncio.timeout(_INVALIDATE_SECONDS):
                await db.invalidate()
        raise


@asynccontextmanager
async def command_transaction(sessions):
    """Bound server lock lifetime even when a partition prevents rollback.

    SET LOCAL cannot leak into pooled connections or long publication work.
    Cleanup is joined before returning, including repeated cancellation.
    """
    stack = AsyncExitStack()
    db = None
    # sys.exc_info() in finally can contain the caller's handled error.
    # Only an exception escaping this context may roll it back.
    exception = (None, None, None)
    try:
        db = await stack.enter_async_context(sessions())
        await stack.enter_async_context(db.begin())
        await db.execute(text("SET LOCAL idle_in_transaction_session_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '1s'"))
        yield db
    except BaseException:
        exception = sys.exc_info()
        raise
    finally:
        task = asyncio.create_task(_finish(stack, db, exception))
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        if cancelled:
            raise asyncio.CancelledError
