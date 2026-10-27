"""Serialized body delivery with bounded retries and durable display cuts.

This private writer lock is not an ExecutionGuard lock. Injected callbacks must
open their own short transactions and release execution guards before returning;
no database transaction or guard lock may surround this component's Redis I/O.
Raw Core events are applied once. Only their already-built envelopes and captured
snapshots may be retried after an ambiguous transport or commit response.
"""

import asyncio
from collections import deque
import time

MAX_ACCEPT_BATCH = 64


class RealtimeUnavailable(RuntimeError):
    """Controller must stop safely; this is not a user cancellation."""


class RealtimeTransportExhausted(RealtimeUnavailable):
    """Bounded transport exhausted while durable authority may remain valid."""


class RealtimeAuthorityLost(RealtimeUnavailable):
    """The writer can no longer prove this execution owns its effects."""


class RealtimeWriter:
    def __init__(
        self,
        accumulator,
        *,
        conversation_id,
        log_manager,
        log,
        check_authority,
        read_business_seq,
        save_snapshot,
        policy,
        clock=time.monotonic,
    ):
        self.accumulator, self.conversation_id = accumulator, conversation_id
        self.log_manager, self.log, self.policy = log_manager, log, policy
        self.check_authority, self.read_business_seq, self.save_snapshot = (
            check_authority,
            read_business_seq,
            save_snapshot,
        )
        self._clock = clock
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._pending = deque()
        self._pending_bytes = 0
        self._pending_cut = None
        self._terminal_cut = None
        self._ref = None
        self._cursor = None
        self._dirty = False
        self._dirty_bytes = 0
        self._last_save = clock()
        self._outage_since = None
        self._outage_saved = False
        self._retry_at = 0.0
        self._unpublished_body = False
        self._failure = None
        self._closed = False
        self._timer_task = None

    @property
    def pending_event_count(self):
        return len(self._pending)

    @property
    def pending_bytes(self):
        return self._pending_bytes

    async def _bounded(self, callback, *args, **kwargs):
        async with asyncio.timeout(self.policy.operation_timeout_seconds):
            return await callback(*args, **kwargs)

    async def _authority(self):
        try:
            allowed = await self._bounded(self.check_authority)
        except Exception as error:
            raise RealtimeAuthorityLost("Execution authority is unavailable") from error
        if allowed is not True:
            raise RealtimeAuthorityLost("Execution authority was lost")

    def _open(self):
        if self._closed:
            raise RuntimeError("Realtime writer is closed")

    async def accept(self, events):
        events = [events] if isinstance(events, dict) else events
        if (
            not isinstance(events, list)
            or len(events) > MAX_ACCEPT_BATCH
            or any(not isinstance(e, dict) for e in events)
        ):
            raise ValueError("Core batch must contain at most 64 event objects")
        async with self._lock:
            self._open()
            if self._failure is not None:
                raise self._failure
            await self._authority()
            await self._retry_cut()
            if self._terminal_cut is not None:
                raise ValueError("Terminal message cannot accept events")
            overflowed = False
            for raw in events:
                if (
                    raw.get("conversation_id", self.conversation_id)
                    != self.conversation_id
                ):
                    raise ValueError("Core event belongs to another Conversation")
                envelopes = self.accumulator.apply(raw)
                self._dirty = True
                if overflowed:
                    # This bounded batch has already left Core. Retain its body
                    # in the accumulator without growing the transport buffer.
                    continue
                for envelope in envelopes:
                    if envelope.conversation_id != self.conversation_id:
                        raise ValueError("Accumulator belongs to another Conversation")
                    size = len(envelope.model_dump_json().encode("utf-8"))
                    self._dirty_bytes += size
                    if self._would_overflow(size):
                        await self._flush()
                        if self._would_overflow(size):
                            self._unpublished_body = True
                            overflowed = True
                            break
                    self._pending.append((envelope, size))
                    self._pending_bytes += size
            if overflowed:
                await self._stop_after_cut("Realtime buffer limit reached")
            healthy = (
                await self._flush() if self._pending or self._snapshot_due() else True
            )
            await self._after_flush(healthy)
            self._wake.set()

    def _would_overflow(self, size):
        return (
            len(self._pending) >= self.policy.max_buffer_events
            or self._pending_bytes + size > self.policy.max_buffer_bytes
        )

    def _outage_expired(self):
        return (
            self._outage_since is not None
            and self._clock() - self._outage_since >= self.policy.outage_grace_seconds
        )

    async def _flush(self, *, force=False):
        if not force and self._clock() < self._retry_at:
            return False
        try:
            await self._authority()
            ref = await self._bounded(self.log_manager.ensure, self.conversation_id)
            if ref.conversation_id != self.conversation_id:
                raise ValueError("Log reference belongs to another Conversation")
            if self._ref is not None and ref != self._ref:
                self._cursor = None
                self._dirty = True
                # Prefix frames may have existed only in the retired log. Save
                # their complete content before emitting suffixes in the new one.
                await self._cut("partial", unanchored=True)
            self._ref = ref
            while self._pending:
                await self._authority()
                envelope, size = self._pending[0]
                appended = await self._bounded(self.log.append, ref, envelope)
                if (
                    appended.cursor.conversation_id != self.conversation_id
                    or appended.cursor.generation != ref.generation
                ):
                    raise ValueError(
                        "Append receipt belongs to another Conversation log"
                    )
                if not await self._bounded(self.log_manager.is_current, ref):
                    self._cursor = None
                    raise RealtimeLogChanged("Log generation changed during append")
                self._cursor = appended.cursor
                self._dirty = True
                self._pending.popleft()
                self._pending_bytes -= size
        except RealtimeUnavailable:
            raise
        except Exception:
            # A successful but unacknowledged XADD retains the exact envelope.
            # Do not infer a cursor from a later tail or recreate a tool action.
            self._cursor = None
            self._dirty = True
            now = self._clock()
            if self._outage_since is None:
                self._outage_since = now
            self._retry_at = now + min(
                self.policy.snapshot_interval_seconds,
                self.policy.operation_timeout_seconds,
            )
            return False
        self._outage_since = None
        self._outage_saved = False
        self._retry_at = 0.0
        return True

    def _snapshot_due(self):
        return self._dirty and (
            self._clock() - self._last_save >= self.policy.snapshot_interval_seconds
            or self._dirty_bytes >= self.policy.snapshot_max_bytes
        )

    async def _after_flush(self, healthy):
        if self._outage_expired():
            await self._stop_after_cut("Realtime outage grace period exceeded")
        if self._dirty and (
            self._snapshot_due() or (not healthy and not self._outage_saved)
        ):
            await self._cut("partial", unanchored=not healthy or self._cursor is None)
            if not healthy:
                self._outage_saved = True

    async def _stop_after_cut(self, reason):
        # Save all body state already applied, including the delta which could
        # not enter the bounded transport buffer. Later raw events are refused.
        self._failure = RealtimeTransportExhausted(reason)
        await self._cut("partial", unanchored=True)
        raise self._failure

    async def _retry_cut(self):
        if self._pending_cut is None:
            return
        snapshot, unanchored = self._pending_cut
        try:
            await self._authority()
            await self._bounded(
                self.save_snapshot,
                snapshot.model_copy(deep=True),
                unanchored=unanchored,
            )
        except RealtimeAuthorityLost:
            raise
        except Exception as error:
            raise RealtimeUnavailable(
                "Display snapshot commit is unconfirmed"
            ) from error
        self._pending_cut = None
        self._last_save = self._clock()
        self._dirty = False
        self._dirty_bytes = 0

    async def _cut(self, phase, *, incomplete=False, unanchored=False):
        await self._retry_cut()
        await self._authority()
        try:
            business_seq = await self._bounded(self.read_business_seq)
        except Exception as error:
            raise RealtimeUnavailable(
                "Display snapshot business position is unavailable"
            ) from error
        unanchored = (
            unanchored
            or self._unpublished_body
            or bool(self._pending)
            or self._cursor is None
        )
        snapshot = self.accumulator.capture(
            phase,
            None if unanchored else self._cursor,
            business_seq,
            incomplete=incomplete,
            unanchored=unanchored,
        )
        if snapshot.conversation_id != self.conversation_id:
            raise ValueError("Accumulator belongs to another Conversation")
        self._pending_cut = (snapshot, unanchored)
        if phase in {"final", "interrupted"}:
            self._terminal_cut = snapshot
        await self._retry_cut()
        return snapshot.model_copy(deep=True)

    async def checkpoint(self, phase, incomplete=False):
        async with self._lock:
            self._open()
            await self._authority()
            retried = self._pending_cut[0] if self._pending_cut is not None else None
            await self._retry_cut()
            if (
                retried is not None
                and retried.phase == phase
                and (not incomplete or retried.incomplete)
            ):
                return retried.model_copy(deep=True)
            if self._terminal_cut is not None:
                if phase != self._terminal_cut.phase or (
                    incomplete and not self._terminal_cut.incomplete
                ):
                    raise ValueError("A terminal cut cannot be recaptured or changed")
                return self._terminal_cut.model_copy(deep=True)
            healthy = await self._flush(force=True)
            if self._outage_expired() and phase != "interrupted":
                await self._stop_after_cut("Realtime outage grace period exceeded")
            result = await self._cut(
                phase, incomplete=incomplete, unanchored=not healthy
            )
            if not healthy:
                self._outage_saved = True
            return result

    async def emergency_checkpoint(self):
        """Persist the consumed prefix after proven Core cleanup, without Redis I/O.

        This does not clear the delivery failure or permit another accept. Any
        unconfirmed PG cut is retried with its original immutable identity first.
        """
        async with self._lock:
            if not isinstance(self._failure, RealtimeTransportExhausted):
                raise RealtimeUnavailable("Emergency cut requires transport exhaustion")
            if self._terminal_cut is not None:
                raise RealtimeUnavailable("A terminal cut cannot be replaced")
            return await self._cut("interrupted", incomplete=True, unanchored=True)

    async def run_flush_timer(self):
        if self._timer_task is not None:
            raise RuntimeError("Realtime flush timer already running")
        self._timer_task = asyncio.current_task()
        try:
            while not self._closed:
                async with self._lock:
                    if self._closed:
                        return
                    if self._failure is not None:
                        raise self._failure
                    if self._dirty or self._pending or self._pending_cut:
                        await self._authority()
                        await self._retry_cut()
                        if self._terminal_cut is None:
                            healthy = (
                                await self._flush()
                                if self._pending or self._snapshot_due()
                                else True
                            )
                            await self._after_flush(healthy)
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        min(
                            self.policy.snapshot_interval_seconds,
                            self.policy.operation_timeout_seconds,
                        ),
                    )
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
        finally:
            self._timer_task = None

    async def close(self):
        """Only stop the timer; the controller owns approval/terminal cuts."""
        self._closed = True
        self._wake.set()
        task = self._timer_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class RealtimeLogChanged(RuntimeError):
    """Internal retry signal; a stale append receipt grants no coverage."""
