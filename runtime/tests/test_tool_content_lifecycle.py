"""Real Core/PG lifecycle with private display capture; never replay tool effects."""
import asyncio
import hashlib
import json
import threading
from contextlib import aclosing

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.tool import ToolChunk

from test_postgres_execution_store import database  # noqa: F401
from test_running_realtime import prepare_reply
from test_running_stop import WaitingTool, stop_intent
from test_parked_stop import quiescent
from workmate_runtime.execution.controller import ExecutionAborted
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.tool_content import ToolContentCapture


class ObjectStore:
    def __init__(self, *, unavailable=False, blocked=False):
        self.objects = {}
        self.unavailable = unavailable
        self.blocked = blocked
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.completed = False
        self.sources = []

    def publish_file(self, key, source, digest, size):
        self.sources.append(source)
        self.loop.call_soon_threadsafe(self.entered.set)
        if self.blocked:
            assert self.release.wait(5), 'test must release the object upload'
        if self.unavailable:
            raise OSError('object storage unavailable')
        source.seek(0)
        value = source.read()
        assert len(value) == size and hashlib.sha256(value).hexdigest() == digest
        self.objects[key] = value
        self.completed = True


class ObservedCapture(ToolContentCapture):
    def __init__(self, store):
        super().__init__(store, inline_bytes=32, preview_chars=8)
        self.closed = False

    def close(self):
        super().close()
        self.closed = True


async def events(db):
    return [json.loads(row['payload']) for row in await db.fetch(
        'SELECT payload FROM runtime_events ORDER BY runtime_seq')]


class LongWaitingTool(WaitingTool):
    output = '执行中的工具输出😀' * 100

    async def __call__(self, **kwargs):
        self.calls += 1
        try:
            yield ToolChunk(content=[TextBlock(text=self.output)], is_last=False)
            self.entered.set()
            await asyncio.Event().wait()
        finally:
            self.exited += 1


async def test_controlled_stop_preserves_large_result_and_proven_terminal(database):
    pool, db = database
    store = ObjectStore()
    capture = ObservedCapture(store)
    tool = LongWaitingTool()
    prepare, _ = prepare_reply([[ToolCallBlock(id='call', name=tool.name, input='{}')]], tool=tool)
    controller = RunningExecutionController(pool, verify_cleanup=quiescent, poll_interval=.01,
        tool_content_factory=lambda: capture)
    run = asyncio.create_task(controller.start('command', 'worker', 'node', prepare))
    try:
        await asyncio.wait_for(tool.entered.wait(), 3)
        await stop_intent(db)
        result = await asyncio.wait_for(run, 5)
        assert result.status == 'interrupted'
        assert tool.exited == 1 and capture.closed and not capture.buffers
        saved = await events(db)
        reference = next(row['content_ref'] for row in saved if row['type'] == 'TOOL_CONTENT_REFERENCE')
        # Core may append its own interruption description. The actual emitted
        # tool output must be retained byte-for-byte at the beginning.
        assert store.objects[reference['storage_key']].startswith(tool.output.encode())
        assert saved[-1]['stop_cleanup']['resources_quiescent'] is True
        assert await db.fetchval("SELECT count(*) FROM runtime_commands WHERE status='completed'") == 2
    finally:
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.parametrize('unavailable', [False, True])
async def test_producer_exception_preserves_claim_and_partial_display_or_original_failure(database, unavailable):
    pool, db = database
    store = ObjectStore(unavailable=unavailable)
    capture = ObservedCapture(store)
    # The real model/Core emits an argument delta; interrupt its producer before
    # TOOL_CALL_END. This is an unexpected producer error, not a product Stop.
    tool = WaitingTool()
    source = json.dumps({'content': '未完成参数' * 100}, ensure_ascii=False)
    base_prepare, _ = prepare_reply([[ToolCallBlock(id='call', name=tool.name, input=source)]], tool=tool)

    async def prepare(*args):
        prepared = await base_prepare(*args)
        original = prepared.engine.reply_stream
        async def broken(inputs):
            async with aclosing(original(inputs)) as stream:
                async for event in stream:
                    yield event
                    if event.type == 'TOOL_CALL_DELTA':
                        raise RuntimeError('producer failed after arguments')
        prepared.engine.reply_stream = broken
        return prepared

    with pytest.raises(ExecutionAborted) as raised:
        await RunningExecutionController(pool, verify_cleanup=quiescent,
            tool_content_factory=lambda: capture).start('command', 'worker', 'node', prepare)
    assert 'producer failed after arguments' in str(raised.value.__cause__)
    assert capture.closed and not capture.buffers and tool.calls == 0
    saved = await events(db)
    refs = [row['content_ref'] for row in saved if row['type'] == 'TOOL_CONTENT_REFERENCE']
    if unavailable:
        assert refs == [] and not store.objects
        assert 'Partial tool display flush failed: OSError' in raised.value.__cause__.__notes__
    else:
        assert len(refs) == 1 and refs[0]['incomplete'] is True
        assert store.objects[refs[0]['storage_key']] == source.encode()
    assert not any(row['type'] == 'EXECUTION_BOUNDARY' for row in saved)
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'claimed'


async def test_cancelled_upload_is_joined_without_reference_or_terminal(database):
    pool, db = database
    store = ObjectStore(blocked=True)
    capture = ObservedCapture(store)
    tool = WaitingTool()
    source = json.dumps({'content': '完整审批参数' * 100}, ensure_ascii=False)
    prepare, _ = prepare_reply([[ToolCallBlock(id='call', name=tool.name, input=source)]], tool=tool)
    run = asyncio.create_task(RunningExecutionController(pool, verify_cleanup=quiescent,
        tool_content_factory=lambda: capture).start('command', 'worker', 'node', prepare))
    try:
        await asyncio.wait_for(store.entered.wait(), 3)
        run.cancel()
        await asyncio.sleep(.05)
        assert not run.done(), 'shutdown must join the publication thread before closing its spool'
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 5)
        assert store.completed and capture.closed and all(source.closed for source in store.sources)
        saved = await events(db)
        assert not any(row['type'] in {'TOOL_CONTENT_REFERENCE', 'EXECUTION_BOUNDARY'} for row in saved)
        assert await db.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'") == 'claimed'
    finally:
        store.release.set()
        if not run.done():
            run.cancel()
        await asyncio.gather(run, return_exceptions=True)
