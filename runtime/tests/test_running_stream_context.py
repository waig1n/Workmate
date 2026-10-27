"""Platform acting identity survives every yield and cancellation of Core."""
import asyncio
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from agentscope.message import ToolCallBlock, ToolCallState
from workmate_runtime.assembly.platform_tools import PlatformCallBinding
from workmate_runtime.execution.running import RunningExecutionController, RunningGuard


class Arguments(BaseModel):
    pass


@pytest.mark.parametrize('interrupt', [False, True])
async def test_platform_binding_context_survives_stream_and_stop(interrupt):
    entered = asyncio.Event()
    identities = []
    owner = SimpleNamespace(session_id='session')
    guard = RunningGuard(None, owner, 'command', 'node')
    guard.reply_id = 'reply'
    current = ContextVar('platform_call', default=None)
    adapter = SimpleNamespace(schemas={'agent_create': Arguments}, current=current, guard=guard)
    binding = PlatformCallBinding(adapter)
    agent = SimpleNamespace(state=SimpleNamespace(session_id='session', reply_id='reply'))
    call = ToolCallBlock(id='call', name='agent_create', input='{}', state=ToolCallState.ALLOWED)

    async def tool(**kwargs):
        identities.append(current.get().call_id)
        yield 'tool-start'
        identities.append(current.get().call_id)
        entered.set()
        if interrupt:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                yield 'tool-interrupted'
        else:
            yield 'tool-success'
        identities.append(current.get().call_id)

    async def reply_stream(_):
        async for event in binding.on_acting(agent, {'tool_call': call}, tool):
            yield event
        assert current.get() is None
        yield 'reply-end'

    queue = asyncio.Queue()
    producer = asyncio.create_task(RunningExecutionController._produce(
        None, guard, SimpleNamespace(reply_stream=reply_stream), None, queue))
    waiting = asyncio.create_task(entered.wait())
    try:
        async with asyncio.timeout(2):
            await asyncio.wait((waiting, producer), return_when=asyncio.FIRST_COMPLETED)
            if producer.done():
                await producer
            if interrupt:
                guard.stop_claim = object()
                guard.cancel_core()
            await producer
    finally:
        waiting.cancel()
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, waiting, return_exceptions=True)
    assert identities == ['call'] * 3
    assert [queue.get_nowait() for _ in range(queue.qsize())] == [
        'tool-start', 'tool-interrupted' if interrupt else 'tool-success', 'reply-end']
    assert current.get() is None


@pytest.mark.parametrize("state", ["success", "interrupted", "error"])
async def test_final_tool_response_drains_acting_before_core_stops_consuming(state):
    """Core sequential interruption may never resume the final acting yield."""
    from agentscope.tool import ToolResponse
    guard = RunningGuard(None, SimpleNamespace(session_id='session'), 'command', 'node')
    async def check():
        pass
    guard.check = check
    closed = asyncio.Event()
    async def tool(**kwargs):
        try:
            yield ToolResponse(content=[], state=state)
        finally:
            closed.set()
    stream = guard.on_acting(None, {}, tool)
    try:
        result = await anext(stream)
        assert result.state == state
        assert closed.is_set()
        assert not guard.active_tools
    finally:
        await stream.aclose()


@pytest.mark.parametrize('extra', [False, True])
async def test_final_tool_response_does_not_hide_tail_failure(extra):
    from agentscope.tool import ToolResponse
    from workmate_runtime.execution.controller import ExecutionAborted
    guard = RunningGuard(None, SimpleNamespace(session_id='session'), 'command', 'node')
    async def check():
        pass
    guard.check = check
    async def tool(**kwargs):
        yield ToolResponse(content=[], state='interrupted')
        if extra:
            yield ToolResponse(content=[], state='success')
        else:
            raise RuntimeError('cleanup failed')
    stream = guard.on_acting(None, {}, tool)
    try:
        with pytest.raises(ExecutionAborted if extra else RuntimeError):
            await anext(stream)
        assert not guard.active_tools
    finally:
        await stream.aclose()
