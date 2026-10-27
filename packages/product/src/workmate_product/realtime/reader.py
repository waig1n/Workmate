"""One Redis tail per local Conversation, with independent bounded subscribers.

LogRefs must come from an authorized, generation-checked application boundary.
This component neither grants access nor rotates logs. Network reads never hold
the registry lock; a reader failure is an explicit recovery boundary.
"""
import asyncio
from collections import deque

from workmate_contracts.realtime import MAX_ENVELOPE_BYTES, RealtimeCursor
from .redis_log import LogEntry, ReplayGap


class SlowSubscriber(RuntimeError):
    """The client must reconnect from its last applied cursor."""


class ReaderClosed(RuntimeError):
    """The local reader was shut down; this is not a successful empty replay."""


class GenerationMismatch(RuntimeError):
    """The caller must explicitly invalidate an obsolete local reader."""
    def __init__(self, message, *, ref=None):
        super().__init__(message)
        self.ref = ref


class ReaderCapacityExceeded(RuntimeError):
    """A new Conversation cannot exceed this Gateway's reader budget."""


def _position(cursor):
    return tuple(map(int, cursor.stream_id.split('-')))


def _bounded(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError('Reader limits require bounded integers')
    return value


class ConversationReaderHub:
    def __init__(self, log, *, page_events=100, buffer_events=256,
                 buffer_bytes=2097152, block_ms=1000, max_readers=1024):
        self.log = log
        self.page_events = _bounded(page_events, 1, 1000)
        self.buffer_events = _bounded(buffer_events, 1, 8192)
        self.buffer_bytes = _bounded(buffer_bytes, 1, 67108864)
        self.block_ms = _bounded(block_ms, 1, 60000)
        self.max_readers = _bounded(max_readers, 1, 16384)
        # Bound a replay page before allocating it, including worst-case frames.
        self.replay_events = min(self.page_events, self.buffer_events,
            max(1, self.buffer_bytes // MAX_ENVELOPE_BYTES))
        self._lock = asyncio.Lock()
        self._hubs = {}
        self._closed = False

    async def subscribe(self, ref, cursor):
        cursor = RealtimeCursor.model_validate(cursor.model_dump())
        if cursor.conversation_id != ref.conversation_id or cursor.generation != ref.generation:
            raise ValueError('Cursor belongs to another Conversation log')
        while True:
            async with self._lock:
                if self._closed:
                    raise ReaderClosed('Reader registry is closed')
                hub = self._hubs.get(ref.conversation_id)
                if hub is None:
                    if len(self._hubs) >= self.max_readers:
                        raise ReaderCapacityExceeded('Gateway Conversation reader budget exceeded')
                    hub = _Conversation(self, ref)
                    self._hubs[ref.conversation_id] = hub
                if not hub.closing:
                    if hub.ref != ref:
                        raise GenerationMismatch('Invalidate the current log before changing generation', ref=hub.ref)
                    if hub.error is not None:
                        raise hub.error
                    sub = _Subscription(hub, cursor)
                    hub.subscribers.add(sub)
                    break
                finished = hub.finished
            await finished.wait()
        try:
            await hub.ready.wait()
            sub._check()
            # Registration precedes this atomic read and its cut. Events arriving
            # concurrently are buffered, then deduplicated against replay.
            page = await self.log.read(ref, cursor, count=self.replay_events)
            sub._check()
            sub.cut = page.window.last
            sub.replay.extend(page.entries)
            sub._prune_buffer()
            return sub
        except BaseException:
            await asyncio.shield(sub.aclose())
            raise

    async def _detach(self, sub):
        hub = sub.hub
        async with self._lock:
            hub.subscribers.discard(sub)
            if hub.subscribers:
                return
            hub.closing = True
            cleanup = self._start_retire(hub)
        await asyncio.shield(cleanup)

    def _start_retire(self, hub):
        if hub.cleanup is None:
            hub.cleanup = asyncio.create_task(self._retire(hub))
        return hub.cleanup

    async def _retire(self, hub):
        try:
            if not hub.task.done():
                hub.task.cancel()
            await asyncio.gather(hub.task, return_exceptions=True)
        finally:
            async with self._lock:
                if self._hubs.get(hub.ref.conversation_id) is hub:
                    self._hubs.pop(hub.ref.conversation_id)
                hub.finished.set()

    async def invalidate(self, ref, error=None):
        """Retire exactly this reference; a stale callback cannot close a new log."""
        async with self._lock:
            hub = self._hubs.get(ref.conversation_id)
            if hub is None or hub.ref != ref:
                return False
            if not hub.closing:
                hub.closing = True
                hub.fail(error or GenerationMismatch('Conversation log was superseded'))
            cleanup = self._start_retire(hub)
        await asyncio.shield(cleanup)
        return True

    async def close(self):
        async with self._lock:
            self._closed = True
            hubs = list(self._hubs.values())
            retiring = []
            for hub in hubs:
                if not hub.closing:
                    hub.closing = True
                    hub.fail(ReaderClosed('Reader registry is closed'))
                retiring.append(self._start_retire(hub))
        await asyncio.gather(*(asyncio.shield(task) for task in retiring))


class _Conversation:
    def __init__(self, registry, ref):
        self.registry, self.ref = registry, ref
        self.subscribers = set()
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.error = None
        self.closing = False
        self.cleanup = None
        self.task = asyncio.create_task(self.run())

    def fail(self, error):
        if self.error is None:
            self.error = error
        for sub in self.subscribers:
            sub.fail(self.error)
        self.ready.set()

    async def run(self):
        try:
            window = await self.registry.log.window(self.ref)
            cursor = window.last
            self.ready.set()
            while True:
                page = await self.registry.log.tail(self.ref, cursor,
                    count=self.registry.page_events, block_ms=self.registry.block_ms)
                for entry in page.entries:
                    if _position(entry.cursor) <= _position(cursor):
                        raise ReplayGap('Shared tail did not advance monotonically')
                    size = len(entry.envelope.model_dump_json().encode('utf-8'))
                    for sub in tuple(self.subscribers):
                        sub.feed(entry, size)
                        if sub.error is not None:
                            self.subscribers.discard(sub)
                    cursor = entry.cursor
                    if not self.subscribers:
                        return
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail(error)
        finally:
            self.ready.set()
            async with self.registry._lock:
                self.closing = True
                if self.registry._hubs.get(self.ref.conversation_id) is self:
                    self.registry._hubs.pop(self.ref.conversation_id)
                self.finished.set()


class _Subscription:
    def __init__(self, hub, cursor):
        self.hub, self.cursor = hub, cursor
        self.cut = None
        self.replay = deque()
        self.buffer = deque()
        self.bytes = 0
        self.error = None
        self.closed = False
        self._reading = False
        self._cleanup = None
        self.wake = asyncio.Event()

    def __aiter__(self):
        return self

    @property
    def replay_cut(self):
        return self.cut

    def check(self):
        """Check handshake liveness without consuming or advancing a cursor."""
        self._check()

    def _check(self):
        if self.error is not None:
            raise self.error
        if self.closed:
            raise StopAsyncIteration

    def fail(self, error):
        if self.error is None:
            self.error = error
        self.buffer.clear()
        self.replay.clear()
        self.bytes = 0
        self.wake.set()

    def feed(self, entry, size):
        if self.closed or self.error is not None:
            return
        covered = self.cut or self.cursor
        if _position(entry.cursor) <= _position(covered):
            return
        limits = self.hub.registry
        if len(self.buffer) >= limits.buffer_events or self.bytes + size > limits.buffer_bytes:
            self.fail(SlowSubscriber('Client buffer exceeded; resume from the last applied cursor'))
            return
        self.buffer.append((entry, size))
        self.bytes += size
        self.wake.set()

    def _prune_buffer(self):
        while self.buffer and _position(self.buffer[0][0].cursor) <= _position(self.cut):
            _, size = self.buffer.popleft()
            self.bytes -= size

    def _deliver(self, entry):
        if len(entry.envelope.model_dump_json().encode('utf-8')) > self.hub.registry.buffer_bytes:
            raise SlowSubscriber('Event exceeds the client byte budget')
        self.cursor = entry.cursor
        # Frozen envelopes still contain mutable JSON. One client must not
        # mutate another client's shared replay or tail payload.
        return LogEntry(entry.cursor, entry.envelope.model_copy(deep=True))

    async def __anext__(self):
        if self._reading:
            raise RuntimeError('A subscription requires a single consumer')
        self._reading = True
        try:
            while True:
                self._check()
                if self.replay:
                    return self._deliver(self.replay.popleft())
                if _position(self.cursor) < _position(self.cut):
                    page = await self.hub.registry.log.read(self.hub.ref, self.cursor,
                        count=self.hub.registry.replay_events)
                    self._check()
                    self.replay.extend(e for e in page.entries
                        if _position(e.cursor) <= _position(self.cut))
                    if not self.replay:
                        raise ReplayGap('Replay cannot reach the captured handoff position')
                    continue
                while self.buffer:
                    entry, size = self.buffer.popleft()
                    self.bytes -= size
                    if _position(entry.cursor) > _position(self.cursor):
                        return self._deliver(entry)
                self.wake.clear()
                await self.wake.wait()
        except BaseException:
            await asyncio.shield(self.aclose())
            raise
        finally:
            self._reading = False

    async def aclose(self):
        if self._cleanup is None:
            self.closed = True
            self.buffer.clear()
            self.replay.clear()
            self.bytes = 0
            self.wake.set()
            self._cleanup = asyncio.create_task(self.hub.registry._detach(self))
        await asyncio.shield(self._cleanup)
