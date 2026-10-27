"""Pure, single-writer message state; transport and persistence belong elsewhere."""
from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from workmate_contracts.realtime import (
    ContentBlock, MessageSnapshot, RealtimeCursor, RealtimeEnvelope, TextDelta,
    message_id,
)
from workmate_product.projections.translator import translate_event

_BODY = {f'{kind}_BLOCK_{action}': (kind, action)
         for kind in ('TEXT', 'THINKING') for action in ('START', 'DELTA', 'END')}
_TERMINAL = {'final', 'interrupted'}


class MessageAccumulator:
    """Accumulate a Task/Reply without retaining mutable caller-owned values.

    Mutations are committed only after the outgoing contract has validated.
    A caller retries an already produced envelope, rather than reapplying its
    raw delta: Core deltas have no source id with which to detect duplicates.
    """

    def __init__(self, conversation_id: str, task_id: str, reply_id: str,
                 execution_epoch: int, restored_snapshot: MessageSnapshot | None = None):
        identity = dict(conversation_id=conversation_id, task_id=task_id,
                        reply_id=reply_id, execution_epoch=execution_epoch)
        initial = MessageSnapshot(**identity, message_id=message_id(task_id, reply_id),
            snapshot_version=1, blocks=[], business_seq=0, phase='partial', incomplete=False)
        self._identity = identity
        self._message_id = initial.message_id
        self._blocks: dict[str, ContentBlock] = {}
        self._version = 0
        self._phase = 'partial'
        self._incomplete = False
        self._process_end = 0
        self._business_seq = 0
        self._cursor: RealtimeCursor | None = None
        if restored_snapshot is not None:
            restored = MessageSnapshot.model_validate(restored_snapshot.model_dump())
            if any(getattr(restored, key) != value for key, value in identity.items()
                   if key != 'execution_epoch') or execution_epoch < restored.execution_epoch:
                raise ValueError('Snapshot identity or execution epoch does not match')
            self._blocks = {block.block_id: block for block in restored.blocks}
            self._version = restored.snapshot_version
            self._phase = restored.phase
            self._incomplete = restored.incomplete
            self._process_end = restored.process_end
            self._business_seq = restored.business_seq
            self._cursor = restored.covered_cursor

    def apply(self, raw: dict) -> tuple[RealtimeEnvelope, ...]:
        if self._phase in _TERMINAL:
            raise ValueError('Terminal message cannot receive more events')
        if not isinstance(raw, dict):
            raise ValueError('Core event must be a JSON object')
        for key in ('conversation_id', 'task_id', 'reply_id'):
            if key in raw and raw[key] != self._identity[key]:
                raise ValueError(f'Core event {key} does not match message')
        event_type = raw.get('type')
        if not isinstance(event_type, str) or not event_type:
            raise ValueError('Core event requires a type')
        shape = _BODY.get(event_type)
        if shape is None:
            if raw['type'] == 'TOOL_CALL_START':
                self._process_end = sum(len(b.text) for b in self._blocks.values() if b.kind == 'text')
            return ()
        # Business payload bounds and sanitization belong to the writer; body
        # JSON and Unicode must validate before any local state is changed.
        RealtimeEnvelope(**self._identity, source='worker', event_id='validation', payload=raw)
        kind, action = shape
        block_id = raw.get('block_id')
        if not isinstance(block_id, str) or not block_id:
            raise ValueError('Body events require a block_id')
        old = self._blocks.get(block_id)
        expected_kind = 'text' if kind == 'TEXT' else 'reasoning'
        if old is not None and old.kind != expected_kind:
            raise ValueError('Block kind cannot change')
        if action == 'START':
            if old is not None:
                return ()
            updated = ContentBlock(block_id=block_id, kind=expected_kind, text='',
                                   content_version=0, closed=False)
        else:
            if old is None:
                raise ValueError('Body delta/end requires a preceding start')
            if action == 'END' and old.closed:
                return ()
            if old.closed:
                raise ValueError('Closed block cannot receive a delta')
            values = old.model_dump()
            if action == 'DELTA':
                delta = raw.get('delta', '')
                if not isinstance(delta, str):
                    raise ValueError('Body delta must be a string')
                values.update(text=old.text + delta, content_version=old.content_version + 1)
            else:
                values['closed'] = True
            updated = ContentBlock(**values)
        metadata = None
        if action == 'DELTA':
            metadata = TextDelta(message_id=self._message_id, block_id=block_id,
                content_version=updated.content_version,
                from_offset=len(old.text.encode('utf-8')), to_offset=len(updated.text.encode('utf-8')))
        envelopes = []
        for payload in translate_event(raw, self._identity['conversation_id']):
            # Stable within the logical block version, including after restore.
            name = json.dumps([self._identity, block_id, action, updated.content_version, payload],
                              sort_keys=True, ensure_ascii=False, separators=(',', ':'))
            envelopes.append(RealtimeEnvelope(**self._identity, source='worker',
                event_id=str(uuid5(NAMESPACE_URL, name)), payload=payload, text=metadata))
        self._blocks[block_id] = updated
        return tuple(envelopes)

    def capture(self, phase: str, covered_cursor: RealtimeCursor | None,
                business_seq: int, incomplete: bool = False, *,
                unanchored: bool = False) -> MessageSnapshot:
        if self._phase in _TERMINAL:
            raise ValueError('Terminal snapshot already captured; retry the original cut')
        if type(incomplete) is not bool:
            raise ValueError('Incomplete must be an explicit boolean')
        # Prefix-only accumulation cannot repair content lost before this cut.
        incomplete = self._incomplete or incomplete
        if type(unanchored) is not bool:
            raise ValueError('Unanchored mode must be an explicit boolean')
        if unanchored and covered_cursor is not None:
            raise ValueError('Unanchored snapshot cannot claim a covered cursor')
        if business_seq < self._business_seq:
            raise ValueError('Snapshot business sequence cannot regress')
        if self._cursor is not None:
            if covered_cursor is None and not unanchored:
                raise ValueError('Snapshot coverage cannot disappear without explicit unanchored mode')
            if (covered_cursor is not None and covered_cursor.generation == self._cursor.generation
                    and tuple(map(int, covered_cursor.stream_id.split('-')))
                    < tuple(map(int, self._cursor.stream_id.split('-')))):
                raise ValueError('Snapshot coverage cannot regress')
        blocks = [block.model_dump() for block in self._blocks.values()]
        if phase in _TERMINAL:
            for block in blocks:
                block['closed'] = True
        snapshot = MessageSnapshot(**self._identity, message_id=self._message_id,
            snapshot_version=self._version + 1, blocks=blocks, covered_cursor=covered_cursor,
            business_seq=business_seq, phase=phase, incomplete=incomplete, process_end=self._process_end)
        self._version = snapshot.snapshot_version
        self._phase = phase
        self._incomplete = snapshot.incomplete
        self._business_seq = business_seq
        self._cursor = snapshot.covered_cursor
        self._blocks = {block.block_id: block for block in snapshot.blocks}
        return snapshot.model_copy(deep=True)
