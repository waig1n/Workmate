"""Ready-queue batching must not reorder boundaries or weaken Stop fencing."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from workmate_runtime.execution import running
from workmate_runtime.transport.postgres import LostOwnership


class Event(BaseModel):
    type: str = 'TEXT_BLOCK_DELTA'
    reply_id: str = 'reply'
    session_id: str = 'session'
    delta: str = 'a'


def guard():
    return SimpleNamespace(reply_id='reply', owner=SimpleNamespace(session_id='session'),
                           write_lock=asyncio.Lock(), failure=None, stop_claim=None,
                           claim_stop=AsyncMock(return_value=None))


@pytest.mark.parametrize('kind', ['TOOL_CALL_START', 'TOOL_CALL_END', 'TOOL_RESULT_END',
                                  'REPLY_END', 'REQUIRE_USER_CONFIRM', 'REPLY_START'])
async def test_ready_batch_keeps_boundary_for_next_iteration(kind):
    queue = asyncio.Queue()
    for item in (Event(delta='b'), Event(type=kind), Event(delta='c')):
        queue.put_nowait(item)
    batch, pending = running._ready_batch(guard(), Event(), queue)
    assert [item['delta'] for item in batch] == ['a', 'b']
    assert pending.type == kind
    assert queue.get_nowait().delta == 'c'


@pytest.mark.parametrize('delta,count,expected', [('a', 20, 16), ('x'*70000, 3, 1),
                                                 ('x'*140000, 2, 1), ('a', 1, 1)], ids=['count', 'bytes', 'large-first', 'empty-queue'])
async def test_ready_batch_limits_and_never_waits_for_missing_events(delta, count, expected):
    queue = asyncio.Queue()
    for _ in range(count-1):
        queue.put_nowait(Event(delta=delta))
    batch, pending = running._ready_batch(guard(), Event(delta=delta), queue)
    assert len(batch) == expected
    assert len(batch) + queue.qsize() + (pending is not None) == count


@pytest.mark.parametrize('changes', [{'reply_id': 'other'}, {'session_id': 'other'}])
async def test_every_batched_event_is_identity_checked(changes):
    queue = asyncio.Queue()
    queue.put_nowait(Event(**changes))
    with pytest.raises(running.ExecutionAborted):
        running._ready_batch(guard(), Event(), queue)


class Pool:
    @asynccontextmanager
    async def acquire(self):
        yield object()


@pytest.mark.parametrize('mode', ['normal', 'stopped', 'raced', 'lost', 'failed'])
async def test_batch_uses_write_fence_then_resolves_racing_stop(monkeypatch, mode):
    batch = [Event(delta=s).model_dump() for s in ('a','b','c')]
    buffered = []
    current = guard()
    if mode == 'stopped':
        current.stop_claim = object()
    if mode == 'failed':
        current.failure = RuntimeError('failed')
    append = AsyncMock(side_effect=LostOwnership('lost') if mode in {'raced','lost'} else None)
    monkeypatch.setattr(running, 'ExecutionStore', lambda db: SimpleNamespace(append_events=append))
    if mode == 'raced':
        async def claim(db, *, require_delivery=True):
            if append.call_count:
                current.stop_claim = object()
            return current.stop_claim
        current.claim_stop.side_effect = claim
    controller = running.RunningExecutionController(Pool(), verify_cleanup=AsyncMock())
    if mode in {'lost','failed'}:
        with pytest.raises((LostOwnership, running.ExecutionAborted)):
            await controller._persist_or_buffer(current, batch, buffered)
        assert not buffered
    else:
        await controller._persist_or_buffer(current, batch, buffered)
        if mode == 'normal':
            current.claim_stop.assert_not_awaited()
            append.assert_awaited_once_with(current.owner, 'reply', events=batch)
            assert not buffered
        else:
            assert buffered == batch
            assert append.call_count == (mode == 'raced')


def test_tool_deltas_coalesce_without_crossing_identity_or_boundaries():
    from workmate_runtime.execution.tool_deltas import coalesce_tool_deltas
    def delta(value, call='one', kind='TOOL_CALL_DELTA'):
        return {'type': kind, 'tool_call_id': call, 'reply_id': 'r', 'session_id': 's', 'delta': value}
    events = [delta('你'), delta('😀'), delta('x', 'two'), {'type': 'TOOL_CALL_END'}, delta('tail')]
    result = coalesce_tool_deltas(events)
    assert [e.get('delta') for e in result] == ['你😀', 'x', None, 'tail']
    assert events[0]['delta'] == '你'


def test_large_delta_replay_is_lossless_and_bounded():
    from workmate_runtime.execution.tool_deltas import coalesce_tool_deltas
    events = [{'type': 'TOOL_CALL_DELTA', 'tool_call_id': 'one', 'reply_id': 'r',
               'session_id': 's', 'delta': '字😀'} for _ in range(10535)]
    result = coalesce_tool_deltas(events)
    assert ''.join(e['delta'] for e in result) == '字😀' * 10535
    assert len(result) < 10
    assert all(len(e['delta'].encode()) <= 32768 for e in result)


def test_coalescer_keeps_other_metadata_and_non_tool_events():
    from workmate_runtime.execution.tool_deltas import coalesce_tool_deltas
    events = [{'type': 'TOOL_CALL_DELTA', 'tool_call_id': 'x', 'delta': 'a', 'extra': 1},
              {'type': 'TOOL_CALL_DELTA', 'tool_call_id': 'x', 'delta': 'b', 'extra': 2},
              {'type': 'TEXT_BLOCK_DELTA', 'delta': 'answer'},
              {'type': 'TEXT_BLOCK_DELTA', 'delta': '2'}]
    assert coalesce_tool_deltas(events) == events


def test_real_core_delta_ids_do_not_prevent_compaction():
    from agentscope.event import ToolCallDeltaEvent
    from workmate_runtime.execution.tool_deltas import coalesce_tool_deltas
    rows = [ToolCallDeltaEvent(reply_id='r', tool_call_id='c', delta=s).model_dump(mode='json') for s in ['你','😀']]
    assert rows[0]['id'] != rows[1]['id']
    compact = coalesce_tool_deltas(rows)
    assert len(compact) == 1
    assert compact[0]['delta'] == '你😀'
    assert compact[0]['id'] == rows[0]['id']
    assert compact[0]['created_at'] == rows[0]['created_at']
