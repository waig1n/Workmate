"""Reset handshakes prove real Redis tails without rewriting durable snapshots."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from test_realtime_redis_log import log  # noqa: F401
from workmate_contracts.realtime import RealtimeCursor, RealtimeEnvelope
from workmate_product.realtime.message_accumulator import MessageAccumulator
from workmate_product.realtime.reader import ConversationReaderHub
from workmate_product.realtime.reset_builder import (
    ActiveDisplay, ResetReadView, ResetBuilder, ResetViewChanged,
)


class Scene:
    def __init__(self, log, ref):
        self.log, self.ref = log, ref
        self.reader = ConversationReaderHub(log, page_events=1, block_ms=20)
        self.manager = type('Manager', (), {})()
        self.manager.ensure = AsyncMock(return_value=ref)
        self.manager.is_current = AsyncMock(return_value=True)
        self.accumulator = MessageAccumulator('conversation', 'task', 'reply', 1)
        self.active = ActiveDisplay('task', 'reply', 1)
        self.snapshots = ()
        self.read_view = AsyncMock(side_effect=self.view)
        self.builder = ResetBuilder(self.manager, log, self.reader, self.read_view, page_events=1)

    async def view(self, cid):
        assert cid == 'conversation'
        return ResetReadView(cid, 0, {'task_status': 'running'}, self.snapshots, self.active)

    def envelopes(self, kind, **values):
        return self.accumulator.apply({'type': kind, 'reply_id': 'reply', 'block_id': 'b', **values})

    async def emit(self, kind, **values):
        receipt = None
        for envelope in self.envelopes(kind, **values):
            receipt = await self.log.append(self.ref, envelope)
        return receipt.cursor if receipt else None

    async def prefix(self, *, anchored=True):
        await self.emit('TEXT_BLOCK_START')
        anchor = await self.emit('TEXT_BLOCK_DELTA', delta='你')
        snapshot = self.accumulator.capture('partial', anchor if anchored else None,
                                             0, unanchored=not anchored)
        self.snapshots = (snapshot,)
        return snapshot

    async def build(self):
        before = [snapshot.model_dump(mode='json') for snapshot in self.snapshots]
        result = await self.builder.build('conversation', reason='initial')
        assert [snapshot.model_dump(mode='json') for snapshot in self.snapshots] == before
        assert [snapshot.model_dump(mode='json') for snapshot in result.reset.snapshots] == before
        return result


@pytest_asyncio.fixture
async def scene(log):
    log.max_bytes = 1048576
    value = Scene(log, await log.initialize('conversation', str(uuid4())))
    try:
        yield value
    finally:
        await value.reader.close()


def historical_final():
    accumulator = MessageAccumulator('conversation', 'old-task', 'old-reply', 1)
    accumulator.apply({'type': 'TEXT_BLOCK_START', 'reply_id': 'old-reply', 'block_id': 'old'})
    accumulator.apply({'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'old-reply', 'block_id': 'old', 'delta': 'history'})
    cursor = RealtimeCursor(conversation_id='conversation', generation=str(uuid4()), stream_id='9999999999999-0')
    return accumulator.capture('final', cursor, 0)


async def test_synced_reset_proves_utf8_versioned_tail_and_ignores_historical_anchor(scene):
    snapshot = await scene.prefix()
    scene.snapshots = (historical_final(), snapshot)
    first = await scene.emit('TEXT_BLOCK_DELTA', delta='😀')
    last = await scene.emit('TEXT_BLOCK_DELTA', delta='好')
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'synced'
    assert handshake.ref == scene.ref
    assert handshake.replay_from == snapshot.covered_cursor
    assert handshake.verified_through == last
    entries = [await asyncio.wait_for(anext(handshake.subscription), 3) for _ in range(2)]
    assert [entry.cursor for entry in entries] == [first, last]
    assert [(entry.envelope.text.from_offset, entry.envelope.text.to_offset,
             entry.envelope.text.content_version) for entry in entries] == [(3, 7, 2), (7, 10, 3)]
    assert snapshot.blocks[0].text == '你' and snapshot.snapshot_version == 1
    await handshake.subscription.aclose()


@pytest.mark.parametrize('prefix', ['unanchored', 'old_generation', 'missing'])
@pytest.mark.parametrize('refresh_possible', [True, False])
async def test_unproven_active_prefix_is_not_synced(scene, prefix, refresh_possible):
    snapshot = await scene.prefix(anchored=prefix != 'unanchored')
    if prefix == 'old_generation':
        old_cursor = snapshot.covered_cursor.model_copy(update={'generation': str(uuid4())})
        scene.snapshots = (snapshot.model_copy(update={'covered_cursor': old_cursor}),)
    elif prefix == 'missing':
        scene.snapshots = (historical_final(),)
    scene.active = replace(scene.active, refresh_possible=refresh_possible)
    handshake = await scene.build()
    assert handshake.reset.sync_status == ('awaiting_snapshot' if refresh_possible else 'incomplete')
    assert all(not snapshot.incomplete for snapshot in handshake.reset.snapshots)
    await handshake.subscription.aclose()


@pytest.mark.parametrize('problem', ['version_gap', 'content_conflict', 'missing_start'])
async def test_discontinuous_tail_cannot_claim_synced(scene, problem):
    await scene.prefix()
    if problem == 'missing_start':
        scene.envelopes('TEXT_BLOCK_START', block_id='new')
        event = scene.envelopes('TEXT_BLOCK_DELTA', block_id='new', delta='x')[0]
    else:
        event = scene.envelopes('TEXT_BLOCK_DELTA', delta='好')[0]
        raw = event.model_dump()
        if problem == 'version_gap':
            raw['text']['content_version'] += 1
        else:
            raw['text'].update(content_version=1, from_offset=0, to_offset=3)
        event = RealtimeEnvelope.model_validate(raw)
    await scene.log.append(scene.ref, event)
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'awaiting_snapshot'
    assert not handshake.reset.snapshots[0].incomplete
    await handshake.subscription.aclose()


async def test_lost_tail_without_refresh_is_incomplete(scene):
    await scene.prefix()
    event = scene.envelopes('TEXT_BLOCK_DELTA', delta='gap')[0]
    raw = event.model_dump()
    raw['text']['content_version'] += 2
    await scene.log.append(scene.ref, RealtimeEnvelope.model_validate(raw))
    scene.active = replace(scene.active, refresh_possible=False)
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'incomplete'
    await handshake.subscription.aclose()


@pytest.mark.parametrize('change', ['task', 'reply', 'epoch', 'empty', 'refresh'])
async def test_changed_active_identity_or_refresh_eligibility_rejects_handshake(scene, change):
    await scene.prefix()
    original = await scene.view('conversation')
    changed = replace(scene.active, **{
        'task': {'task_id': 'new-task'}, 'reply': {'reply_id': 'new-reply'},
        'epoch': {'execution_epoch': 2},
        'empty': {'empty': True}, 'refresh': {'refresh_possible': False},
    }[change])
    scene.read_view.side_effect = [original, replace(original, active=changed)]
    with pytest.raises(ResetViewChanged):
        await scene.build()
    assert scene.read_view.await_count == 2


async def test_generation_race_rejects_handshake(scene):
    await scene.prefix()
    scene.manager.is_current.return_value = False
    with pytest.raises(ResetViewChanged):
        await scene.build()


async def test_newer_snapshot_and_business_sequence_do_not_invalidate_proven_old_cut(scene):
    original_snapshot = await scene.prefix()
    last = await scene.emit('TEXT_BLOCK_DELTA', delta='tail')
    original = await scene.view('conversation')
    newer = scene.accumulator.capture('partial', last, 1)
    scene.read_view.side_effect = [original, replace(original, snapshots=(newer,), business_seq=1)]
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'synced'
    assert handshake.reset.snapshots[0] == original_snapshot
    assert handshake.reset.business_seq == 0
    assert handshake.replay_from == original_snapshot.covered_cursor
    await handshake.subscription.aclose()


async def test_explicit_safe_empty_active_can_sync_without_snapshot(scene):
    scene.active = replace(scene.active, empty=True)
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'synced'
    assert handshake.reset.snapshots == []
    await handshake.subscription.aclose()


async def test_safe_empty_claim_cannot_hide_existing_active_content(scene):
    await scene.prefix()
    scene.active = replace(scene.active, empty=True)
    handshake = await scene.build()
    assert handshake.reset.sync_status != 'synced'
    await handshake.subscription.aclose()


@pytest.mark.parametrize('phase', ['final', 'interrupted'])
async def test_empty_claim_conflicts_with_current_terminal_snapshot(scene, phase):
    snapshot = await scene.prefix()
    terminal = scene.accumulator.capture(phase, snapshot.covered_cursor, 0)
    scene.snapshots = (terminal,)
    scene.active = replace(scene.active, empty=True)
    handshake = await scene.build()
    assert handshake.reset.sync_status != 'synced'
    await handshake.subscription.aclose()


async def test_cancelling_handshake_closes_registered_subscription(scene, monkeypatch):
    await scene.prefix()
    registered = []
    original_subscribe = scene.reader.subscribe
    async def subscribe(*args, **kwargs):
        subscription = await original_subscribe(*args, **kwargs)
        registered.append(subscription)
        return subscription
    monkeypatch.setattr(scene.reader, 'subscribe', subscribe)
    rechecking = asyncio.Event()
    original_view = scene.view
    count = 0
    async def read_view(cid):
        nonlocal count
        count += 1
        if count == 2:
            rechecking.set()
            await asyncio.Event().wait()
        return await original_view(cid)
    scene.read_view.side_effect = read_view
    task = asyncio.create_task(scene.builder.build('conversation'))
    try:
        await asyncio.wait_for(rechecking.wait(), 3)
        assert len(registered) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert registered[0].closed
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('problem', ['unanchored', 'old_generation', 'missing', 'version_gap'])
async def test_incomplete_history_is_never_downgraded_to_awaiting_or_synced(scene, problem):
    active = await scene.prefix(anchored=problem != 'unanchored')
    historical = historical_final().model_copy(update={'phase': 'interrupted', 'incomplete': True})
    if problem == 'old_generation':
        active = active.model_copy(update={'covered_cursor': active.covered_cursor.model_copy(
            update={'generation': str(uuid4())})})
    scene.snapshots = (historical,) if problem == 'missing' else (historical, active)
    if problem == 'version_gap':
        event = scene.envelopes('TEXT_BLOCK_DELTA', delta='gap')[0]
        raw = event.model_dump()
        raw['text']['content_version'] += 2
        await scene.log.append(scene.ref, RealtimeEnvelope.model_validate(raw))
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'incomplete'
    assert handshake.reset.snapshots[0].incomplete is True
    await handshake.subscription.aclose()


async def test_final_view_recheck_cannot_certify_a_trimmed_replay_prefix(scene):
    snapshot = await scene.prefix()
    last = await scene.emit('TEXT_BLOCK_DELTA', delta='tail')
    original = await scene.view('conversation')
    reads = 0
    async def read_view(cid):
        nonlocal reads
        reads += 1
        if reads == 2:
            # Trim using the actual retention operation after tail proof, while
            # the second consistent Product read is in progress.
            scene.log.max_bytes = 1
            window = await scene.log.window(scene.ref)
            assert window.floor == last
            assert tuple(map(int, window.floor.stream_id.split('-'))) > tuple(
                map(int, snapshot.covered_cursor.stream_id.split('-')))
        return original
    scene.read_view.side_effect = read_view
    with pytest.raises(ResetViewChanged):
        await scene.build()
    assert reads == 2


async def test_orphan_nonterminal_incomplete_is_sticky_when_current_tail_has_gap(scene):
    active = await scene.prefix()
    orphan = MessageAccumulator('conversation', 'orphan-task', 'orphan-reply', 1)
    orphan.apply({'type': 'TEXT_BLOCK_START', 'reply_id': 'orphan-reply', 'block_id': 'old'})
    orphan.apply({'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'orphan-reply', 'block_id': 'old', 'delta': 'orphan'})
    unfinished = orphan.capture('partial', active.covered_cursor, 0)
    assert not unfinished.incomplete
    scene.snapshots = (unfinished, active)
    event = scene.envelopes('TEXT_BLOCK_DELTA', delta='gap')[0]
    raw = event.model_dump()
    raw['text']['content_version'] += 2
    await scene.log.append(scene.ref, RealtimeEnvelope.model_validate(raw))
    handshake = await scene.build()
    assert handshake.reset.sync_status == 'incomplete'
    await handshake.subscription.aclose()


async def test_incomplete_history_does_not_block_verified_current_content(scene):
    active = await scene.prefix()
    history = historical_final().model_copy(update={'phase': 'interrupted', 'incomplete': True})
    scene.snapshots = (history, active)
    await scene.emit('TEXT_BLOCK_DELTA', delta='current safe tail')
    handshake = await scene.build()
    try:
        assert handshake.reset.sync_status == 'incomplete'
        assert handshake.content_ready is True
    finally:
        await handshake.subscription.aclose()


@pytest.mark.parametrize('problem', ['missing', 'version_gap'])
async def test_incomplete_history_cannot_mask_unproven_current_content(scene, problem):
    active = await scene.prefix()
    history = historical_final().model_copy(update={'phase': 'interrupted', 'incomplete': True})
    scene.snapshots = (history,) if problem == 'missing' else (history, active)
    if problem == 'version_gap':
        raw = scene.envelopes('TEXT_BLOCK_DELTA', delta='missing prefix')[0].model_dump()
        raw['text']['content_version'] += 2
        await scene.log.append(scene.ref, RealtimeEnvelope.model_validate(raw))
    handshake = await scene.build()
    try:
        assert handshake.reset.sync_status == 'incomplete'
        assert handshake.content_ready is False
    finally:
        await handshake.subscription.aclose()
