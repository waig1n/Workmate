"""Join cancellable SQL operations without interrupting driver cleanup twice."""
import asyncio
from functools import wraps

import anyio


def cancel_once(method):
    """Join the full ORM operation, including pre-ping before Session owns it.

    A second cancellation during dialect invalidation can strand a connection
    before it is assigned to the Session, beyond the finally block's reach.
    Forward cancellation once and let that driver cleanup finish; never hand an
    unknown live connection back to the pool to meet a caller deadline.
    """
    @wraps(method)
    async def wrapped(*args, **kwargs):
        async def run():
            with anyio.CancelScope(shield=True):
                return await method(*args, **kwargs)
        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except BaseException as original:
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
                    try:
                        task.result()
                    except Exception as cleanup_error:
                        if isinstance(original, asyncio.CancelledError):
                            raise original from cleanup_error
                        raise
            raise
    return wrapped
