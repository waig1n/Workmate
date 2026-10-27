"""SQL cancellation relays once, joins, and retains caller cancellation."""
import asyncio

import pytest

from workmate_runtime.execution.cancellation import cancel_once


@pytest.mark.parametrize('finish', ['return', 'error'])
async def test_cancel_joins_child_even_when_it_translates_cancellation(finish):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    counts = []

    @cancel_once
    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            counts.append(asyncio.current_task().cancelling())
            cleaning.set()
            await release.wait()
            if finish == 'error':
                raise RuntimeError('driver cleanup failed')
            return 'late result'

    baseline = set(asyncio.all_tasks())
    task = asyncio.create_task(operation())
    try:
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        if finish == 'error':
            assert isinstance(caught.value.__cause__, RuntimeError)
        assert counts == [1]
        assert not {t for t in asyncio.all_tasks() - baseline if not t.done()}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_uncancelled_error_keeps_identity():
    error = RuntimeError('query failed')

    @cancel_once
    async def operation():
        raise error

    with pytest.raises(RuntimeError) as caught:
        await operation()
    assert caught.value is error
