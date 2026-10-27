"""Authorized SSE handshakes over a shared, bounded Conversation reader.

Each handle owns authorization, its replay cursor and disconnect cleanup; only
the underlying Redis tail is shared with other local subscribers.
"""
import asyncio
import json

from workmate_contracts.realtime import classify_resume, encode_cursor
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.reader import GenerationMismatch, SlowSubscriber
from workmate_product.realtime.redis_log import LogLost, ReplayGap
from workmate_product.realtime.reset_builder import ResetBuilder, ResetViewChanged

from .conversation_business_state import DisplaySnapshotUnavailable


class StreamCapacityExceeded(RuntimeError):
    """Gateway SSE admission is full, including in-progress handshakes."""


def _position(cursor):
    return tuple(map(int, cursor.stream_id.split('-')))


def _frame(name, value, cursor=None):
    prefix = '' if cursor is None else f'id: {encode_cursor(cursor)}\n'
    payload = {'type': 'CUSTOM', 'name': name, 'value': value}
    return prefix + 'data: ' + json.dumps(payload, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')) + '\n\n'


def _identity(active):
    return None if active is None else (active.task_id, active.reply_id, active.execution_epoch)


class RealtimeProductStream:
    def __init__(self, manager, log, reader, *, policy=None):
        self.manager, self.log, self.reader = manager, log, reader
        self.policy = RealtimePolicy() if policy is None else policy
        self._handles = set()
        self._closed = False

    async def open(self, conversation_id, *, authority, last_event_id=None):
        if self._closed or len(self._handles) >= self.policy.max_gateway_subscriptions:
            raise StreamCapacityExceeded('Gateway SSE subscription budget exceeded')
        handle = _Handle(self, conversation_id, authority)
        self._handles.add(handle)
        handle.initializer = asyncio.create_task(handle.initialize(last_event_id))
        try:
            await asyncio.shield(handle.initializer)
            if handle.closed:
                raise StreamCapacityExceeded('Gateway SSE service closed during admission')
            return handle
        except BaseException:
            await asyncio.shield(handle.aclose())
            raise

    async def close(self):
        self._closed = True
        await asyncio.gather(*(handle.aclose() for handle in tuple(self._handles)))


class _Handle:
    def __init__(self, stream, conversation_id, authority):
        self.stream, self.cid, self.authority = stream, conversation_id, authority
        self.builder = ResetBuilder(stream.manager, stream.log, stream.reader,
            authority.read_view, page_events=stream.policy.reader_page_events)
        self.initializer = self.cleanup = self.pending = self.handshake_task = None
        self.operation = None
        self.subscription = self.ref = self.cut = self.active = None
        self.final_replay_identities = set()
        self.business_seq = 0
        self.content_ready = True
        self.reset_frame = None
        self.replayed = False
        self.closed = self.consuming = False
        self.next_authorize = self.next_reset = 0

    async def initialize(self, token):
        await self.authority.authorize(self.cid)
        ref = await self.stream.manager.ensure(self.cid)
        if ref.conversation_id != self.cid:
            raise ValueError('Realtime log belongs to another Conversation')
        try:
            window = await self.stream.log.window(ref)
        except LogLost:
            await self._reset('log_lost')
            return
        if token is None or token == '':
            await self._reset('initial')
            return
        decision = classify_resume(token, conversation_id=self.cid,
            generation=ref.generation, first_stream_id=window.floor.stream_id,
            last_stream_id=window.last.stream_id)
        if decision.action == 'reset':
            await self._reset(decision.reason)
            return
        self.active = await self.authority.active_display(self.cid)
        try:
            self.subscription = await self._subscribe(ref, decision.cursor)
            self.ref = ref
            self.cut = self.subscription.replay_cut
            if (not await self.stream.manager.is_current(ref)
                    or _identity(await self.authority.active_display(self.cid)) != _identity(self.active)):
                await self._reset('generation_changed')
        except ReplayGap:
            await self._reset('trimmed')
        except LogLost:
            await self._reset('log_lost')
        self._deadlines()

    async def _subscribe(self, ref, cursor):
        try:
            return await self.stream.reader.subscribe(ref, cursor)
        except GenerationMismatch as error:
            # An obsolete initializer must never invalidate a newer local hub.
            if error.ref is None or await self.stream.manager.is_current(error.ref):
                raise ResetViewChanged('Local reader already has the current generation') from error
            await self.stream.reader.invalidate(error.ref)
            return await self.stream.reader.subscribe(ref, cursor)

    def _deadlines(self):
        now = asyncio.get_running_loop().time()
        self.next_authorize = now + self.stream.policy.authorize_interval_seconds
        self.next_reset = now + self.stream.policy.reset_retry_seconds

    async def _detach(self):
        self.final_replay_identities.clear()
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None
        if self.subscription is not None:
            await self.subscription.aclose()
            self.subscription = None

    async def _reset(self, reason):
        await self._detach()
        if self.closed:
            raise StopAsyncIteration
        self.handshake_task = asyncio.create_task(self._install_reset(reason))
        await self.handshake_task

    async def _install_reset(self, reason):
        # Qualification races are bounded. A failed handshake is a reconnect,
        # never an empty successful reset or an unbounded subscription loop.
        for attempt in range(3):
            try:
                handshake = await self.builder.build(self.cid, reason)
                break
            except (ResetViewChanged, GenerationMismatch) as error:
                conflicting = getattr(error, 'ref', None)
                if conflicting is not None and not await self.stream.manager.is_current(conflicting):
                    await self.stream.reader.invalidate(conflicting)
                if attempt == 2:
                    raise
                await asyncio.sleep(0)
        self.subscription = handshake.subscription
        self.ref, self.cut = handshake.ref, handshake.verified_through
        self.active, self.content_ready = handshake.active, handshake.content_ready
        # Only the authorized, qualified reset proves these historical Replies
        # final. This is not a cache of failed active-display lookups: an unknown
        # Reply can become current later. Overflow retains the original PG path.
        self.final_replay_identities.clear()
        for snapshot in handshake.reset.snapshots:
            if snapshot.phase == 'final' and _identity(snapshot) != _identity(self.active):
                self.final_replay_identities.add(_identity(snapshot))
                if len(self.final_replay_identities) == 64:
                    break
        self.business_seq = handshake.reset.business_seq
        self.reset_frame = _frame('REALTIME_RESET', handshake.reset.model_dump(mode='json'),
                                  handshake.replay_from)
        self.replayed = False
        self._deadlines()

    async def _maintenance(self):
        now = asyncio.get_running_loop().time()
        if now >= self.next_authorize:
            await self.authority.authorize(self.cid)
            if self.closed:
                return
            # Check the old log first, preserving loss as a distinct reset reason.
            try:
                await self.stream.log.window(self.ref)
            except LogLost:
                await self.stream.reader.invalidate(self.ref, LogLost('Conversation log was lost'))
                await self._reset('log_lost')
                return
            if self.closed:
                return
            if not await self.stream.manager.is_current(self.ref):
                await self.stream.reader.invalidate(self.ref)
                await self._reset('generation_changed')
                return
            self.next_authorize = asyncio.get_running_loop().time() + self.stream.policy.authorize_interval_seconds
        if not self.content_ready and now >= self.next_reset:
            await self._reset('content_gap')

    async def _worker_current(self, event):
        expected = (event.task_id, event.reply_id, event.execution_epoch)
        if expected != _identity(self.active):
            current = await self.authority.active_display(self.cid)
            if expected != _identity(current):
                return False
            # A continuous, proven connection can follow the next live Reply
            # immediately. Missing-prefix recovery must still qualify its cut.
            if not self.content_ready:
                await self._reset('content_gap')
                return False
            self.active = current
        return self.content_ready

    async def _boundary(self, event):
        payload = event.payload
        approval = payload.get('name') == 'HITL_APPROVAL_REQUEST'
        terminal = payload['type'] in {'RUN_FINISHED', 'RUN_ERROR'} or payload.get('name') == 'TASK_COMPLETED'
        if 'display_empty' in payload and (payload['display_empty'] is not True or 'display_snapshot' in payload):
            raise ValueError('Conflicting display boundary evidence')
        if 'display_snapshot' in payload:
            try:
                options = {'expected_approval': payload.get('value', {})} if approval else {}
                snapshot = await self.authority.display_snapshot(self.cid, payload['display_snapshot'],
                    task_id=event.task_id, reply_id=event.reply_id, **options)
            except DisplaySnapshotUnavailable:
                await self._reset('content_gap')
                return None, False
            phases = {'approval'} if approval else {'final', 'interrupted'} if terminal else {'partial', 'approval', 'final', 'interrupted'}
            if snapshot.phase not in phases:
                raise ValueError('Display snapshot phase disagrees with its boundary')
            return _frame('REALTIME_SNAPSHOT', snapshot.model_dump(mode='json')), True
        if terminal or approval:
            # Even a legitimate no-start failure needs the authoritative state
            # restored before a completion with no exact display reference.
            await self._reset('content_gap')
            return None, False
        return None, True

    async def _entry_frames(self, entry):
        event = entry.envelope
        if event.conversation_id != self.cid:
            raise ValueError('Realtime event belongs to another Conversation')
        if event.source == 'worker':
            identity = (event.task_id, event.reply_id, event.execution_epoch)
            if (identity != _identity(self.active)
                    and identity in self.final_replay_identities
                    and entry.cursor.generation == self.ref.generation == self.cut.generation
                    and _position(entry.cursor) <= _position(self.cut)):
                return ()
            if not await self._worker_current(event):
                return ()
        elif event.business_seq <= self.business_seq:
            return ()
        snapshot, publish = await self._boundary(event)
        if not publish:
            return ()
        frames = [_frame('REALTIME_EVENT', event.model_dump(mode='json'), entry.cursor)]
        if snapshot is not None:
            frames.insert(0, snapshot)
        return tuple(frames)

    async def _operate(self, awaitable):
        # Own only in-flight service I/O, not the HTTP consumer or its sends.
        task = self.operation = asyncio.create_task(awaitable)
        try:
            return await task
        finally:
            if self.operation is task:
                self.operation = None

    async def frames(self):
        if self.consuming:
            raise RuntimeError('An SSE handle requires a single consumer')
        self.consuming = True
        last_output = asyncio.get_running_loop().time()
        try:
            while not self.closed:
                await self._operate(self._maintenance())
                if self.closed:
                    return
                if self.reset_frame is not None:
                    frame, self.reset_frame = self.reset_frame, None
                    yield frame
                    last_output = asyncio.get_running_loop().time()
                    continue
                if (not self.replayed and self.pending is None
                        and _position(self.subscription.cursor) >= _position(self.cut)):
                    self.replayed = True
                    yield _frame('REPLAY_DONE', {'generation': self.ref.generation,
                                                'cursor': encode_cursor(self.cut)})
                    last_output = asyncio.get_running_loop().time()
                    continue
                if self.pending is None:
                    self.pending = asyncio.create_task(anext(self.subscription))
                pending_task = self.pending
                now = asyncio.get_running_loop().time()
                deadlines = [self.next_authorize,
                    last_output + self.stream.policy.heartbeat_interval_seconds]
                if not self.content_ready:
                    deadlines.append(self.next_reset)
                done, _ = await asyncio.wait({pending_task}, timeout=max(0, min(deadlines) - now))
                if self.closed:
                    return
                if not done:
                    if asyncio.get_running_loop().time() >= deadlines[1]:
                        yield ': heartbeat\n\n'
                        last_output = asyncio.get_running_loop().time()
                    continue
                self.pending = None
                try:
                    entry = pending_task.result()
                except (LogLost, ReplayGap, GenerationMismatch) as error:
                    reason = 'log_lost' if isinstance(error, LogLost) else 'trimmed' if isinstance(error, ReplayGap) else 'generation_changed'
                    await self._reset(reason)
                    continue
                # Authorization deadlines also apply after network reads, even
                # when a continuous producer never lets this connection idle.
                await self._operate(self._maintenance())
                if self.closed:
                    return
                if entry.cursor.generation != self.ref.generation or self.reset_frame is not None:
                    continue
                for frame in await self._operate(self._entry_frames(entry)):
                    if self.closed:
                        return
                    yield frame
                    last_output = asyncio.get_running_loop().time()
        except (StopAsyncIteration, SlowSubscriber):
            return
        finally:
            await self.aclose()

    async def aclose(self):
        if self.cleanup is None:
            self.closed = True
            self.cleanup = asyncio.create_task(self._cleanup())
        await asyncio.shield(self.cleanup)

    async def _cleanup(self):
        try:
            if self.operation is not None:
                operation = self.operation
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if self.initializer is not None and not self.initializer.done():
                self.initializer.cancel()
                await asyncio.gather(self.initializer, return_exceptions=True)
            if self.handshake_task is not None:
                if not self.handshake_task.done():
                    self.handshake_task.cancel()
                await asyncio.gather(self.handshake_task, return_exceptions=True)
            await self._detach()
        finally:
            self.stream._handles.discard(self)
