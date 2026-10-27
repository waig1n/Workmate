"""Qualify a reset baseline against a registered, continuous Redis tail.

The injected read_view owns authorization and one consistent Product read
transaction. Neither transport positions nor reconstructed text replace the
original durable snapshot cuts. A reset ID is a reconsumption position.
"""
from dataclasses import dataclass
from copy import deepcopy

from workmate_contracts.realtime import (
    JS_SAFE_INTEGER, MessageSnapshot, RealtimeReset, TextState, apply_text_delta, message_id,
)
from .redis_log import ReplayGap


class ResetViewChanged(RuntimeError):
    """Rebuild using a fresh authorized view and log generation."""


@dataclass(frozen=True)
class ActiveDisplay:
    task_id: str
    reply_id: str
    execution_epoch: int
    refresh_possible: bool = True
    # Only a committed quiescent boundary may prove the absence of body.
    empty: bool = False

    def __post_init__(self):
        message_id(self.task_id, self.reply_id)
        if (type(self.execution_epoch) is not int or not 1 <= self.execution_epoch <= JS_SAFE_INTEGER
                or type(self.refresh_possible) is not bool or type(self.empty) is not bool):
            raise ValueError('Active display requires a valid epoch and explicit boolean evidence')


@dataclass(frozen=True)
class ResetReadView:
    conversation_id: str
    business_seq: int
    business_state: dict
    snapshots: tuple[MessageSnapshot, ...]
    active: ActiveDisplay | None = None


@dataclass(frozen=True)
class ResetHandshake:
    reset: RealtimeReset
    ref: object
    replay_from: object
    verified_through: object
    subscription: object
    active: ActiveDisplay | None = None
    content_ready: bool = True


def _position(cursor):
    return tuple(map(int, cursor.stream_id.split('-')))


class _ContentProof:
    def __init__(self, snapshots, active):
        self.active = active
        self.snapshots = {(s.task_id, s.reply_id): s for s in snapshots}
        self.blocks = {}
        self.kinds = {}
        for s in snapshots:
            for block in s.blocks:
                key = (s.message_id, block.block_id)
                self.blocks[key] = TextState(message_id=s.message_id, block_id=block.block_id,
                    content=block.text, content_version=block.content_version,
                    end_utf8=len(block.text.encode('utf-8')), closed=block.closed,
                    execution_epoch=s.execution_epoch)
                self.kinds[key] = block.kind

    def accept(self, entry):
        event = entry.envelope
        kind = event.payload['type']
        if kind not in {'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
                        'REASONING_MESSAGE_START', 'REASONING_MESSAGE_CONTENT', 'REASONING_MESSAGE_END'}:
            return True
        if event.source != 'worker':
            return False
        snapshot = self.snapshots.get((event.task_id, event.reply_id))
        if snapshot is not None:
            if snapshot.phase in {'final', 'interrupted'}:
                return True
            anchor = snapshot.covered_cursor
            if anchor is not None and anchor.generation == entry.cursor.generation and _position(entry.cursor) <= _position(anchor):
                return True
        active = self.active
        if active is None or (event.task_id, event.reply_id) != (active.task_id, active.reply_id):
            # Final historical messages and business facts are restored by the
            # Product view. They cannot supply a missing current Reply prefix.
            return True
        if event.execution_epoch < active.execution_epoch:
            return True
        if event.execution_epoch != active.execution_epoch:
            return False
        block_id = event.payload.get('messageId')
        if not isinstance(block_id, str) or not block_id:
            return False
        key = (message_id(event.task_id, event.reply_id), block_id)
        expected_kind = 'reasoning' if kind.startswith('REASONING_') else 'text'
        if key in self.kinds and self.kinds[key] != expected_kind:
            return False
        old = self.blocks.get(key)
        if kind.endswith('_START'):
            if old is None:
                self.blocks[key] = TextState(message_id=key[0], block_id=block_id,
                    content='', content_version=0, end_utf8=0, closed=False,
                    execution_epoch=active.execution_epoch)
                self.kinds[key] = expected_kind
            return True
        if old is None:
            return False
        if kind.endswith('_END'):
            self.blocks[key] = old.model_copy(update={'closed': True})
            return True
        if active.empty and event.payload.get('delta'):
            return False
        if event.text is None or event.text.block_id != block_id:
            return False
        result = apply_text_delta(old, event)
        if result.action not in {'apply', 'ignore'}:
            return False
        self.blocks[key] = result.state
        return True


class ResetBuilder:
    def __init__(self, log_manager, log, reader, read_view, *, page_events=100):
        if type(page_events) is not int or not 1 <= page_events <= 1000:
            raise ValueError('Reset proof pages require a bounded integer')
        self.manager, self.log, self.reader, self.read_view = log_manager, log, reader, read_view
        self.page_events = page_events

    def _baseline(self, view, ref, window):
        active = view.active
        current = None
        status = 'incomplete' if any(s.incomplete for s in view.snapshots) else 'synced'
        for s in view.snapshots:
            if s.phase in {'final', 'interrupted'}:
                continue
            if (active is None or (s.task_id, s.reply_id, s.execution_epoch)
                    != (active.task_id, active.reply_id, active.execution_epoch)):
                same_reply = active is not None and (s.task_id, s.reply_id) == (active.task_id, active.reply_id)
                status = ('awaiting_snapshot' if same_reply and status != 'incomplete' else 'incomplete')
                continue
            current = s
            if s.covered_cursor is None or s.covered_cursor.generation != ref.generation:
                if status != 'incomplete':
                    status = 'awaiting_snapshot'
        if active is not None and not active.empty and current is None:
            # A complete current terminal cut is also a valid baseline while
            # asynchronous Product settlement is still catching up.
            current = next((s for s in view.snapshots
                if (s.task_id, s.reply_id, s.execution_epoch)
                == (active.task_id, active.reply_id, active.execution_epoch)), None)
            if current is None:
                if status != 'incomplete':
                    status = 'awaiting_snapshot'
        if any(s.incomplete for s in view.snapshots):
            status = 'incomplete'
        if active is not None and active.empty and any(
            (s.task_id, s.reply_id) == (active.task_id, active.reply_id)
            and any(block.text for block in s.blocks) for s in view.snapshots
        ):
            status = self._unavailable(view)
        if status == 'awaiting_snapshot' and active is not None and not active.refresh_possible:
            status = 'incomplete'
        anchor = (current.covered_cursor if current is not None
            and current.phase in {'partial', 'approval'}
            and current.covered_cursor is not None
            and current.covered_cursor.generation == ref.generation else window.floor)
        return status, anchor

    @staticmethod
    def _unavailable(view):
        return ('awaiting_snapshot' if view.active is not None and view.active.refresh_possible
            and not any(s.incomplete for s in view.snapshots) else 'incomplete')

    @staticmethod
    def _content_baseline(view, ref):
        active = view.active
        if active is None:
            return True
        if active.empty:
            return not any((s.task_id, s.reply_id) == (active.task_id, active.reply_id)
                and any(block.text for block in s.blocks) for s in view.snapshots)
        current = next((s for s in view.snapshots
            if (s.task_id, s.reply_id, s.execution_epoch)
            == (active.task_id, active.reply_id, active.execution_epoch)), None)
        return current is not None and (current.phase in {'final', 'interrupted'}
            or (current.covered_cursor is not None and current.covered_cursor.generation == ref.generation))

    async def build(self, conversation_id, reason='initial'):
        view = deepcopy(await self.read_view(conversation_id))
        if view.conversation_id != conversation_id:
            raise ValueError('Reset view belongs to another Conversation')
        # Freeze and validate before any network suspension. Callback ORM rows
        # and nested JSON must not change under this captured Product cut.
        ref = await self.manager.ensure(conversation_id)
        if ref.conversation_id != conversation_id:
            raise ValueError('Reset log belongs to another Conversation')
        baseline = RealtimeReset(conversation_id=conversation_id, generation=ref.generation,
            reason=reason, snapshots=list(view.snapshots), business_state=view.business_state,
            business_seq=view.business_seq, sync_status='awaiting_snapshot').model_copy(deep=True)
        view = ResetReadView(conversation_id, baseline.business_seq, baseline.business_state,
            tuple(baseline.snapshots), view.active)
        window = await self.log.window(ref)
        status, anchor = self._baseline(view, ref, window)
        content_ready = self._content_baseline(view, ref)
        subscription = None
        try:
            try:
                subscription = await self.reader.subscribe(ref, anchor)
            except ReplayGap:
                status = self._unavailable(view)
                content_ready = False
                window = await self.log.window(ref)
                anchor = window.floor
                subscription = await self.reader.subscribe(ref, anchor)
            cut = subscription.replay_cut
            proof = _ContentProof(view.snapshots, view.active)
            cursor = anchor
            try:
                while _position(cursor) < _position(cut):
                    page = await self.log.read(ref, cursor, count=self.page_events)
                    entries = [e for e in page.entries if _position(e.cursor) <= _position(cut)]
                    if not entries:
                        raise ReplayGap('Reset proof cannot reach its captured cut')
                    if not all(proof.accept(e) for e in entries):
                        content_ready = False
                        if status != 'incomplete':
                            status = self._unavailable(view)
                    cursor = entries[-1].cursor
            except ReplayGap:
                # The registered reader can still expose the gap on delivery;
                # never certify a baseline whose tail vanished during proof.
                raise ResetViewChanged('Log trimmed during reset qualification')
            latest = await self.read_view(conversation_id)
            if latest.conversation_id != conversation_id or latest.active != view.active or not await self.manager.is_current(ref):
                raise ResetViewChanged('Current Reply or log changed during reset qualification')
            final_window = await self.log.window(ref)
            if _position(final_window.floor) > _position(anchor):
                raise ResetViewChanged('Log trimmed during reset state recheck')
            subscription.check()
            reset = RealtimeReset(conversation_id=conversation_id, generation=ref.generation,
                reason=reason, snapshots=list(view.snapshots), business_state=view.business_state,
                business_seq=view.business_seq, sync_status=status)
            return ResetHandshake(reset, ref, anchor, cut, subscription, view.active, content_ready)
        except BaseException:
            if subscription is not None:
                await subscription.aclose()
            raise
