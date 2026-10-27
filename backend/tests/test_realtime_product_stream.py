"""Gateway SSE recovery over isolated real Redis; no HTTP role cutover."""
import asyncio
import hashlib
import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.services.conversation_business_state import ConversationUnavailable
from app.services.conversation_business_state import DisplaySnapshotUnavailable
from app.services.realtime_product_stream import RealtimeProductStream, StreamCapacityExceeded
from workmate_contracts.realtime import (
    RealtimeCursor, RealtimeEnvelope, MessageSnapshotReference, decode_cursor, encode_cursor,
)
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.message_accumulator import MessageAccumulator
from workmate_product.realtime.reader import ConversationReaderHub, SlowSubscriber
from workmate_product.realtime.redis_log import RedisConversationLog, FutureCursor
from workmate_product.realtime.reset_builder import ActiveDisplay, ResetReadView


class Authority:
    def __init__(self):
        self.allowed = True
        self.authorizations = 0
        self.snapshots = ()
        self.active = None
        self.business_seq = 0
        self.display_reads = []
        self.pending_confirm = None
        self.expected_approvals = []

    async def authorize(self, cid):
        assert cid == 'conversation'
        self.authorizations += 1
        if not self.allowed:
            raise ConversationUnavailable('revoked')

    async def read_view(self, cid):
        await self.authorize(cid)
        return ResetReadView(cid, self.business_seq, {'tasks':[], 'pending_confirm':self.pending_confirm},
                             self.snapshots, self.active)

    async def active_display(self, cid):
        await self.authorize(cid)
        return self.active

    async def display_snapshot(self, cid, reference, *, task_id, reply_id, expected_approval=None):
        await self.authorize(cid)
        self.display_reads.append(reference)
        self.expected_approvals.append(expected_approval)
        if expected_approval is not None and expected_approval != self.pending_confirm:
            raise DisplaySnapshotUnavailable('approval changed during display read')
        expected = MessageSnapshotReference.model_validate(reference)
        for snapshot in self.snapshots:
            if (snapshot.task_id, snapshot.reply_id) == (task_id, reply_id) and MessageSnapshotReference.from_snapshot(snapshot) == expected:
                return snapshot
        raise DisplaySnapshotUnavailable('display cut superseded or missing')


class Scene:
    def __init__(self, log, ref):
        self.log, self.ref = log, ref
        self.reader = ConversationReaderHub(log, page_events=2, block_ms=10)
        self.authority = Authority()
        self.policy = RealtimePolicy().model_copy(update={
            'authorize_interval_seconds':.02, 'heartbeat_interval_seconds':.02,
            'reset_retry_seconds':.02, 'max_gateway_subscriptions':16,
        })
        self.stream = RealtimeProductStream(self, log, self.reader, policy=self.policy)

    async def ensure(self, cid):
        assert cid == self.ref.conversation_id
        return self.ref

    async def is_current(self, ref):
        return ref == self.ref

    def origin(self):
        return RealtimeCursor(conversation_id='conversation', generation=self.ref.generation, stream_id='0-0')

    async def append(self, index, *, payload=None):
        event = RealtimeEnvelope(event_id=f'event-{index}', conversation_id='conversation',
            task_id='task', reply_id='reply', source='business', business_seq=index,
            payload=payload or {'type':'CUSTOM','name':'NOTICE','value':{'index':index}})
        return await self.log.append(self.ref, event)

    async def open(self, *, cursor=None, authority=None):
        return await self.stream.open('conversation', authority=authority or self.authority,
            last_event_id=encode_cursor(cursor) if cursor else None)

    async def rotate(self):
        self.ref = await self.log.initialize('conversation', str(uuid4()))


@pytest_asyncio.fixture
async def scene():
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires explicitly configured local Redis')
    client = Redis.from_url(url, decode_responses=True, retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
    log = RedisConversationLog(client, namespace='sse-test-'+uuid4().hex,
                              retention_ms=60000, max_bytes=1048576, ttl_seconds=120)
    value = Scene(log, await log.initialize('conversation', str(uuid4())))
    try:
        yield value
    finally:
        await value.stream.close()
        await value.reader.close()
        keys = [key async for key in client.scan_iter(match=f'workmate:{log.namespace}:*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()


def parse(frame):
    assert isinstance(frame, str)
    lines = frame.splitlines()
    data = next((line[6:] for line in lines if line.startswith('data: ')), None)
    event_id = next((line[4:] for line in lines if line.startswith('id: ')), None)
    return (json.loads(data) if data else None), event_id


async def named(frames, name):
    async with asyncio.timeout(3):
        while True:
            payload, event_id = parse(await anext(frames))
            if payload and payload.get('name') == name:
                return payload['value'] if 'value' in payload else payload, event_id


async def replay_done(frames):
    _, event_id = await named(frames, 'REPLAY_DONE')
    assert event_id is None


@pytest.mark.parametrize('operation', ['maintenance', 'entry'])
async def test_service_close_joins_active_authority_reads(scene, monkeypatch, operation):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()

    if operation == 'maintenance':
        monkeypatch.setattr(scene.authority, 'authorize', blocked)
        handle.next_authorize = 0
    else:
        monkeypatch.setattr(handle, '_entry_frames', blocked)
        await scene.append(1)
    consuming = asyncio.create_task(anext(frames))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        closing = asyncio.create_task(scene.stream.close())
        await asyncio.wait_for(cancelling.wait(), 1)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 2)
        await asyncio.gather(consuming, return_exceptions=True)
        assert not scene.stream._handles
    finally:
        release.set()
        consuming.cancel()
        await asyncio.gather(consuming, return_exceptions=True)
        if closing is not None:
            await closing
        await frames.aclose()


@pytest.mark.parametrize('bad', ['malformed', 'cross_conversation', 'future'])
async def test_invalid_cursor_rejected_by_open_before_streaming(scene, bad):
    cursor = scene.origin()
    token = {'malformed':'rt1.!!!!',
             'cross_conversation':encode_cursor(cursor.model_copy(update={'conversation_id':'other'})),
             'future':encode_cursor(cursor.model_copy(update={'stream_id':'9999999999999-0'}))}[bad]
    with pytest.raises((ValueError, FutureCursor)):
        await scene.stream.open('conversation', authority=scene.authority, last_event_id=token)


@pytest.mark.parametrize('mode', ['initial', 'legacy', 'generation_changed', 'trimmed'])
async def test_recovery_modes_emit_explicit_reset_with_replay_cursor(scene, mode):
    token = None
    if mode == 'legacy':
        token = 'product-'+hashlib.sha256(b'conversation').hexdigest()[:32]+':0'
    elif mode == 'generation_changed':
        token = encode_cursor(scene.origin())
        await scene.rotate()
    elif mode == 'trimmed':
        first = await scene.append(1)
        token = encode_cursor(first.cursor)
        await scene.append(2)
        scene.log.max_bytes = 1
        await scene.log.window(scene.ref)
        scene.log.max_bytes = 1048576
    handle = await scene.stream.open('conversation', authority=scene.authority, last_event_id=token)
    frames = handle.frames()
    reset, cursor = await named(frames, 'REALTIME_RESET')
    assert reset['generation'] == scene.ref.generation
    assert reset['reason'] == {'initial':'initial','legacy':'legacy_cursor',
                               'generation_changed':'generation_changed','trimmed':'trimmed'}[mode]
    assert decode_cursor(cursor).generation == scene.ref.generation
    await replay_done(frames)
    await handle.aclose()


async def test_valid_resume_has_independent_replay_without_forced_reset(scene):
    first = await scene.append(1)
    second = await scene.append(2)
    left, right = await scene.open(cursor=first.cursor), await scene.open(cursor=first.cursor)
    left_frames, right_frames = left.frames(), right.frames()
    for frames in (left_frames, right_frames):
        payload, event_id = parse(await anext(frames))
        assert payload['name'] == 'REALTIME_EVENT'
        assert payload['value']['event_id'] == 'event-2'
        assert decode_cursor(event_id) == second.cursor
        await replay_done(frames)
    await left.aclose()
    third = await scene.append(3)
    event, cursor = await named(right_frames, 'REALTIME_EVENT')
    assert event['event_id'] == 'event-3' and decode_cursor(cursor) == third.cursor
    await right.aclose()


async def test_idle_heartbeats_keep_one_pending_reader_alive(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    for _ in range(3):
        frame = await asyncio.wait_for(anext(frames), 1)
        assert parse(frame)[0] is None and frame.startswith(':')
    receipt = await scene.append(1)
    event, token = await named(frames, 'REALTIME_EVENT')
    assert event['event_id'] == 'event-1' and decode_cursor(token) == receipt.cursor
    await handle.aclose()


async def test_continuous_events_cannot_starve_periodic_authorization(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    before = scene.authority.authorizations
    for index in range(1, 13):
        await scene.append(index)
        await named(frames, 'REALTIME_EVENT')
        await asyncio.sleep(.005)
    assert scene.authority.authorizations > before
    await handle.aclose()


async def test_revocation_closes_only_its_subscription(scene):
    denied = Authority()
    left = await scene.open(cursor=scene.origin(), authority=denied)
    right = await scene.open(cursor=scene.origin())
    left_frames, right_frames = left.frames(), right.frames()
    await replay_done(left_frames)
    await replay_done(right_frames)
    denied.allowed = False
    async with asyncio.timeout(2):
        with pytest.raises((ConversationUnavailable, StopAsyncIteration)):
            while True:
                payload, _ = parse(await anext(left_frames))
                assert payload is None
    await scene.append(1)
    assert (await named(right_frames, 'REALTIME_EVENT'))[0]['event_id'] == 'event-1'
    await left.aclose()
    await right.aclose()


async def test_active_generation_change_rehandshakes_without_delivering_old_generation(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    old = scene.ref
    await scene.rotate()
    reset, token = await named(frames, 'REALTIME_RESET')
    assert reset['generation'] != old.generation
    assert decode_cursor(token).generation == scene.ref.generation
    await handle.aclose()


async def test_subscription_capacity_released_by_handle_close(scene):
    stream = RealtimeProductStream(scene, scene.log, scene.reader,
        policy=scene.policy.model_copy(update={'max_gateway_subscriptions':1}))
    first = await stream.open('conversation', authority=scene.authority)
    try:
        with pytest.raises(StreamCapacityExceeded):
            await stream.open('conversation', authority=scene.authority)
        await first.aclose()
        replacement = await stream.open('conversation', authority=scene.authority)
        await replacement.aclose()
    finally:
        await stream.close()


async def test_cancelled_open_releases_capacity_and_subscription(scene):
    entered = asyncio.Event()
    original = scene.authority.read_view
    async def blocked(cid):
        entered.set()
        await asyncio.Event().wait()
    scene.authority.read_view = blocked
    stream = RealtimeProductStream(scene, scene.log, scene.reader,
        policy=scene.policy.model_copy(update={'max_gateway_subscriptions':1}))
    opening = asyncio.create_task(stream.open('conversation', authority=scene.authority))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        scene.authority.read_view = original
        handle = await stream.open('conversation', authority=scene.authority)
        await handle.aclose()
    finally:
        if not opening.done():
            opening.cancel()
        await asyncio.gather(opening, return_exceptions=True)
        await stream.close()


async def append_body(scene, accumulator, kind, **values):
    receipt = None
    for event in accumulator.apply({'type':kind,'reply_id':'reply','block_id':'b',**values}):
        receipt = await scene.log.append(scene.ref, event)
    return receipt.cursor if receipt else None


async def test_awaiting_prefix_suppresses_raw_body_until_periodic_snapshot_recovery(scene):
    accumulator = MessageAccumulator('conversation','task','reply',1)
    scene.authority.active = ActiveDisplay('task','reply',1)
    await append_body(scene, accumulator, 'TEXT_BLOCK_START')
    tail = await append_body(scene, accumulator, 'TEXT_BLOCK_DELTA', delta='missing prefix')
    handle = await scene.open()
    frames = handle.frames()
    first, _ = await named(frames, 'REALTIME_RESET')
    assert first['sync_status'] == 'awaiting_snapshot'
    for _ in range(2):
        payload, _ = parse(await asyncio.wait_for(anext(frames), 1))
        assert not (payload and payload.get('name') == 'REALTIME_EVENT'
                    and payload['value']['payload']['type'].startswith('TEXT_MESSAGE'))
    scene.authority.snapshots = (accumulator.capture('partial', tail, 0),)
    async with asyncio.timeout(3):
        while True:
            reset, _ = await named(frames, 'REALTIME_RESET')
            if reset['sync_status'] == 'synced':
                break
    assert reset['snapshots'][0]['blocks'][0]['text'] == 'missing prefix'
    await handle.aclose()


async def test_terminal_snapshot_precedes_exact_terminal_notification(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    accumulator = MessageAccumulator('conversation','task','reply',1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'final body'})
    snapshot = accumulator.capture('final',None,0,unanchored=True)
    scene.authority.snapshots = (snapshot,)
    reference = MessageSnapshotReference.from_snapshot(snapshot).model_dump(mode='json')
    receipt = await scene.append(1, payload={'type':'RUN_FINISHED','display_snapshot':reference})
    first, event_id = parse(await asyncio.wait_for(anext(frames), 2))
    assert first['name'] == 'REALTIME_SNAPSHOT' and event_id is None
    assert first['value'] == snapshot.model_dump(mode='json')
    second, event_id = parse(await asyncio.wait_for(anext(frames), 2))
    assert second['name'] == 'REALTIME_EVENT'
    assert second['value']['payload']['display_snapshot'] == reference
    assert decode_cursor(event_id) == receipt.cursor
    await handle.aclose()


async def test_superseded_approval_reference_resets_and_suppresses_old_card(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    accumulator = MessageAccumulator('conversation','task','reply',1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'already stopped'})
    old = accumulator.capture('approval',None,0,unanchored=True)
    current = accumulator.capture('interrupted',None,1,unanchored=True)
    scene.authority.snapshots = (current,)
    scene.authority.business_seq = 1
    reference = MessageSnapshotReference.from_snapshot(old).model_dump(mode='json')
    await scene.append(1,payload={'type':'CUSTOM','name':'HITL_APPROVAL_REQUEST',
        'value':{'taskId':'task','toolCalls':[{'id':'call'}]},'display_snapshot':reference})
    async with asyncio.timeout(3):
        while True:
            payload, _ = parse(await anext(frames))
            assert not (payload and payload.get('name') == 'REALTIME_EVENT'
                        and payload['value']['payload'].get('name') == 'HITL_APPROVAL_REQUEST')
            if payload and payload.get('name') == 'REALTIME_RESET':
                assert payload['value']['snapshots'][0]['phase'] == 'interrupted'
                break
    await handle.aclose()


async def test_log_loss_rotates_generation_and_emits_explicit_reset(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    old = scene.ref
    async def ensure(cid):
        if not await scene.log.client.exists(*scene.log.keys(cid, scene.ref.generation)):
            await scene.rotate()
        return scene.ref
    scene.ensure = ensure
    await scene.log.client.delete(*scene.log.keys(old.conversation_id, old.generation))
    reset, token = await named(frames, 'REALTIME_RESET')
    assert reset['reason'] == 'log_lost'
    assert reset['generation'] != old.generation
    assert decode_cursor(token).generation == scene.ref.generation
    await handle.aclose()


async def test_live_tail_retention_gap_rehandshakes_explicitly(scene, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    original_tail = scene.log.tail
    async def delayed_tail(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_tail(*args, **kwargs)
    monkeypatch.setattr(scene.log, 'tail', delayed_tail)
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    await asyncio.wait_for(entered.wait(), 1)
    await scene.append(1)
    last = await scene.append(2)
    scene.log.max_bytes = 1
    assert (await scene.log.window(scene.ref)).floor == last.cursor
    scene.log.max_bytes = 1048576
    release.set()
    reset, token = await named(frames, 'REALTIME_RESET')
    assert reset['reason'] == 'trimmed'
    assert decode_cursor(token) == last.cursor
    await handle.aclose()


async def test_slow_client_is_closed_and_does_not_restart_an_unbounded_queue(scene, monkeypatch):
    subscriptions = []
    original_subscribe = scene.reader.subscribe
    async def subscribe(*args, **kwargs):
        sub = await original_subscribe(*args, **kwargs)
        subscriptions.append(sub)
        return sub
    monkeypatch.setattr(scene.reader, 'subscribe', subscribe)
    scene.reader.buffer_events = 1
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    await scene.append(1)
    await scene.append(2)
    async with asyncio.timeout(2):
        while not isinstance(subscriptions[0].error, SlowSubscriber):
            await asyncio.sleep(.005)
    async with asyncio.timeout(2):
        with pytest.raises((StopAsyncIteration, SlowSubscriber)):
            while True:
                await anext(frames)
    assert subscriptions[0].closed
    assert len(subscriptions) == 1
    await handle.aclose()


async def test_stream_close_cancels_an_inflight_noninitial_reset(scene):
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = scene.authority.read_view
    async def blocked(cid):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return await original(cid)
    # Builder captures read_view during handle construction. Resume admission
    # does not call it; the subsequent generation reset will block here.
    scene.authority.read_view = blocked
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    await scene.rotate()
    handle.next_authorize = 0
    reading = asyncio.create_task(anext(frames))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(scene.stream.close(), 2)
        assert cancelled.is_set(), 'Shutdown must cancel the ongoing reset handshake'
        release.set()
        await asyncio.gather(reading, return_exceptions=True)
        assert not scene.reader._hubs, 'A completed close must not allow a late reset subscription'
        assert handle.subscription is None or handle.subscription.closed
    finally:
        release.set()
        if not reading.done():
            reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        await handle.aclose()


async def test_heartbeat_yield_cannot_publish_replay_done_before_pending_final_entry(scene, monkeypatch):
    first = await scene.append(1)
    handle = await scene.open(cursor=scene.origin())
    subscription = handle.subscription
    original = type(subscription).__anext__
    release = asyncio.Event()
    async def delayed(self):
        if self is subscription:
            await release.wait()
        return await original(self)
    monkeypatch.setattr(type(subscription), '__anext__', delayed)
    frames = handle.frames()
    try:
        heartbeat = await asyncio.wait_for(anext(frames), 2)
        assert parse(heartbeat)[0] is None
        # Finish the background read while the HTTP consumer is suspended at
        # heartbeat yield. Reader.cursor advances before SSE publishes it.
        pending = handle.pending
        release.set()
        await asyncio.wait_for(asyncio.shield(pending), 2)
        assert subscription.cursor == first.cursor
        payload, token = parse(await asyncio.wait_for(anext(frames), 2))
        assert payload['name'] == 'REALTIME_EVENT'
        assert payload['value']['event_id'] == 'event-1'
        assert decode_cursor(token) == first.cursor
        await replay_done(frames)
    finally:
        release.set()
        await handle.aclose()


async def test_actionable_approval_snapshot_precedes_its_exact_notification(scene):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    accumulator = MessageAccumulator('conversation', 'task', 'reply', 1)
    accumulator.apply({'type':'TEXT_BLOCK_START','reply_id':'reply','block_id':'b'})
    accumulator.apply({'type':'TEXT_BLOCK_DELTA','reply_id':'reply','block_id':'b','delta':'Please approve'})
    snapshot = accumulator.capture('approval', None, 0, unanchored=True)
    pending = {'taskId':'task','decisionVersion':'v1',
               'toolCalls':[{'id':'write-1','name':'write','input':'{}'}]}
    scene.authority.active = ActiveDisplay('task', 'reply', 1, refresh_possible=False)
    scene.authority.snapshots = (snapshot,)
    scene.authority.pending_confirm = pending
    reference = MessageSnapshotReference.from_snapshot(snapshot).model_dump(mode='json')
    receipt = await scene.append(1, payload={'type':'CUSTOM','name':'HITL_APPROVAL_REQUEST',
                                          'value':pending,'display_snapshot':reference})
    try:
        first, token = parse(await asyncio.wait_for(anext(frames), 2))
        assert first['name'] == 'REALTIME_SNAPSHOT' and token is None
        assert first['value'] == snapshot.model_dump(mode='json')
        second, token = parse(await asyncio.wait_for(anext(frames), 2))
        assert second['name'] == 'REALTIME_EVENT'
        assert second['value']['payload']['value'] == pending
        assert decode_cursor(token) == receipt.cursor
        assert scene.authority.expected_approvals == [pending]
    finally:
        await handle.aclose()


@pytest.mark.parametrize('close_owner', ['handle', 'stream'])
async def test_close_during_maintenance_authorization_never_emits_or_dereferences_detached_subscription(scene, close_owner):
    handle = await scene.open(cursor=scene.origin())
    entered, release = asyncio.Event(), asyncio.Event()
    original = scene.authority.authorize
    async def paused(cid):
        entered.set()
        await release.wait()
        await original(cid)
    scene.authority.authorize = paused
    handle.next_authorize = 0
    frames = handle.frames()
    reading = asyncio.create_task(anext(frames))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(handle.aclose() if close_owner == 'handle' else scene.stream.close(), 2)
        assert handle.subscription is None
        release.set()
        with pytest.raises((StopAsyncIteration, asyncio.CancelledError)):
            await asyncio.wait_for(reading, 2)
        assert not scene.reader._hubs
    finally:
        release.set()
        if not reading.done():
            reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        await handle.aclose()


async def test_close_while_waiting_for_entry_never_dereferences_cleared_pending(scene, monkeypatch):
    handle = await scene.open(cursor=scene.origin())
    frames = handle.frames()
    await replay_done(frames)
    entered, release = asyncio.Event(), asyncio.Event()
    original_wait = asyncio.wait
    async def paused_wait(tasks, *args, **kwargs):
        own_read = handle.pending is not None and handle.pending in tasks
        if own_read:
            entered.set()
        result = await original_wait(tasks, *args, **kwargs)
        if own_read:
            # Let cleanup finish clearing Handle.pending before returning the
            # cancelled entry task through the actual asyncio.wait result.
            await release.wait()
        return result
    monkeypatch.setattr(asyncio, 'wait', paused_wait)
    reading = asyncio.create_task(anext(frames))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(handle.aclose(), 2)
        assert handle.pending is None
        release.set()
        with pytest.raises((StopAsyncIteration, asyncio.CancelledError)):
            await asyncio.wait_for(reading, 2)
        assert not scene.reader._hubs
    finally:
        release.set()
        if not reading.done():
            reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        await handle.aclose()
