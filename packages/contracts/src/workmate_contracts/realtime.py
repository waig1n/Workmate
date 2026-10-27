"""Version 1 realtime wire values and pure recovery rules, independent of storage.

Redis positions are opaque browser cursors, never Product business sequences.
Text ranges use UTF-8 bytes. The historical ``process_end`` UI marker alone
continues to count Python Unicode codepoints, not bytes or JS UTF-16 units.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from typing import Annotated, Literal
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

JS_SAFE_INTEGER = 2**53 - 1
REDIS_ID_MAX = 2**64 - 1
MAX_JSON_DEPTH = 64
MAX_CURSOR_LENGTH = 2048
MAX_PAYLOAD_BYTES = 256 * 1024
# Keep the established Product payload budget plus bounded wire metadata.
MAX_ENVELOPE_BYTES = MAX_PAYLOAD_BYTES + 4 * 1024
Identity = Annotated[str, Field(min_length=1, max_length=128)]
UInt = Annotated[int, Field(ge=0, le=JS_SAFE_INTEGER)]
PositiveInt = Annotated[int, Field(ge=1, le=JS_SAFE_INTEGER)]
TEXT_EVENTS = {'TEXT_MESSAGE_CONTENT', 'REASONING_MESSAGE_CONTENT'}


def _utf8(value: str) -> bytes:
    if '\0' in value:
        raise ValueError('NUL is not supported in realtime strings')
    try:
        return value.encode('utf-8', errors='strict')
    except UnicodeError as error:
        raise ValueError('Realtime strings require valid Unicode scalars') from error


def _json_safe(value, depth=0):
    if depth > MAX_JSON_DEPTH:
        raise ValueError('Realtime JSON depth exceeds 64')
    if isinstance(value, str):
        _utf8(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _utf8(key)
            _json_safe(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _json_safe(item, depth + 1)
    elif type(value) in (int, float):
        if abs(value) > JS_SAFE_INTEGER or not math.isfinite(value):
            raise ValueError('Realtime numbers must be finite and JavaScript-safe')


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _generation(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError('Generation must be a canonical lowercase UUID')
    except (ValueError, AttributeError) as error:
        raise ValueError('Generation must be a canonical lowercase UUID') from error
    return value


def _position(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)-(0|[1-9][0-9]*)', value):
        raise ValueError('Stream position must be canonical milliseconds-sequence')
    if len(value) > 41:
        raise ValueError('Stream position exceeds Redis uint64 range')
    position = tuple(int(part) for part in value.split('-'))
    if any(part > REDIS_ID_MAX for part in position):
        raise ValueError('Stream position exceeds Redis uint64 range')
    return position


class WireValue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)

    @model_validator(mode='before')
    @classmethod
    def strict_version(cls, value):
        # Literal[1] alone accepts True and 1.0 under Pydantic's strict mode.
        if isinstance(value, dict) and 'schema_version' in value and type(value['schema_version']) is not int:
            raise ValueError('schema_version must be an integer')
        return value

    @model_validator(mode='after')
    def safe_json(self):
        _json_safe(self.model_dump())
        return self


class RealtimeCursor(WireValue):
    schema_version: Literal[1] = 1
    conversation_id: Identity
    generation: str
    stream_id: str

    _validate_generation = field_validator('generation')(_generation)

    @field_validator('stream_id')
    @classmethod
    def validate_position(cls, value):
        _position(value)
        return value


def encode_cursor(cursor: RealtimeCursor) -> str:
    token = 'rt1.' + base64.urlsafe_b64encode(_canonical(cursor.model_dump())).decode('ascii').rstrip('=')
    if len(token) > MAX_CURSOR_LENGTH:
        raise ValueError('Realtime cursor is too long')
    return token


def decode_cursor(token: str, *, conversation_id: str | None = None) -> RealtimeCursor:
    if (not isinstance(token, str) or len(token) > MAX_CURSOR_LENGTH
            or not re.fullmatch(r'rt1\.[A-Za-z0-9_-]+', token)):
        raise ValueError('Malformed realtime cursor')
    try:
        encoded = token[4:]
        raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        cursor = RealtimeCursor.model_validate(json.loads(raw.decode('utf-8')))
    except (ValueError, UnicodeError) as error:
        raise ValueError('Malformed realtime cursor') from error
    if encode_cursor(cursor) != token:
        raise ValueError('Realtime cursor encoding is not canonical')
    if conversation_id is not None and cursor.conversation_id != conversation_id:
        raise ValueError('Realtime cursor belongs to another Conversation')
    return cursor


class ResumeDecision(WireValue):
    action: Literal['resume', 'reset']
    reason: Literal['generation_changed', 'trimmed', 'legacy_cursor'] | None = None
    cursor: RealtimeCursor | None = None


def classify_resume(token: str, *, conversation_id: str, generation: str,
                    first_stream_id: str | None, last_stream_id: str | None) -> ResumeDecision:
    _generation(generation)
    first = _position(first_stream_id) if first_stream_id is not None else None
    last = _position(last_stream_id) if last_stream_id is not None else (0, 0)
    if first is not None and first > last:
        raise ValueError('Stream bounds are inconsistent')
    if isinstance(token, str) and re.fullmatch(r'product-[0-9a-f]{32}:(0|[1-9][0-9]{0,18})', token):
        expected = 'product-' + hashlib.sha256(_utf8(conversation_id)).hexdigest()[:32] + ':'
        if not token.startswith(expected):
            raise ValueError('Legacy cursor belongs to another Conversation')
        return ResumeDecision(action='reset', reason='legacy_cursor')
    cursor = decode_cursor(token, conversation_id=conversation_id)
    if cursor.generation != generation:
        return ResumeDecision(action='reset', reason='generation_changed', cursor=cursor)
    position = _position(cursor.stream_id)
    if position > last:
        raise ValueError('Realtime cursor is ahead of the stream')
    if first is not None and position < first:
        return ResumeDecision(action='reset', reason='trimmed', cursor=cursor)
    return ResumeDecision(action='resume', cursor=cursor)


def message_id(task_id: str, reply_id: str) -> str:
    for identity in (task_id, reply_id):
        if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
            raise ValueError('Message identity requires bounded Task and Reply identifiers')
        _utf8(identity)
    name = 'workmate:message:' + json.dumps([task_id, reply_id], ensure_ascii=False, separators=(',', ':'))
    return str(uuid5(NAMESPACE_URL, name))


class TextDelta(WireValue):
    message_id: Identity
    block_id: Identity
    content_version: PositiveInt
    from_offset: UInt
    to_offset: UInt

    @model_validator(mode='after')
    def ordered_range(self):
        if self.to_offset < self.from_offset:
            raise ValueError('Text range must be ordered')
        return self


class RealtimeEnvelope(WireValue):
    schema_version: Literal[1] = 1
    event_id: Identity
    conversation_id: Identity
    task_id: Identity | None = None
    reply_id: Identity | None = None
    source: Literal['worker', 'business']
    execution_epoch: PositiveInt | None = None
    business_seq: PositiveInt | None = None
    payload: dict[str, JsonValue]
    text: TextDelta | None = None

    @model_validator(mode='after')
    def event_shape(self):
        if not isinstance(self.payload.get('type'), str) or not self.payload['type']:
            raise ValueError('AG-UI payload requires an event type')
        if self.source == 'worker':
            if not self.task_id or not self.reply_id or self.execution_epoch is None or self.business_seq is not None:
                raise ValueError('Worker event requires Task, Reply, epoch and no business sequence')
        elif self.business_seq is None or self.execution_epoch is not None:
            raise ValueError('Business event requires business sequence and no execution epoch')
        if self.payload['type'] in TEXT_EVENTS:
            if self.source != 'worker' or self.text is None or not isinstance(self.payload.get('delta'), str):
                raise ValueError('Text event requires a Worker and typed delta metadata')
            if self.text.message_id != message_id(self.task_id, self.reply_id):
                raise ValueError('Text message identity does not match Task and Reply')
            if len(_utf8(self.payload['delta'])) != self.text.to_offset - self.text.from_offset:
                raise ValueError('Delta length does not match its UTF-8 byte range')
        elif self.text is not None:
            raise ValueError('Only text events carry typed text metadata')
        if len(_canonical(self.payload)) > MAX_PAYLOAD_BYTES:
            raise ValueError('Realtime payload exceeds 256 KiB')
        if len(_canonical(self.model_dump())) > MAX_ENVELOPE_BYTES:
            raise ValueError('Realtime envelope exceeds 260 KiB (256 KiB payload + 4 KiB metadata)')
        return self


class ContentBlock(WireValue):
    block_id: Identity
    kind: Literal['text', 'reasoning']
    text: str
    content_version: UInt
    closed: bool


class MessageSnapshot(WireValue):
    schema_version: Literal[1] = 1
    message_id: Identity
    conversation_id: Identity
    task_id: Identity
    reply_id: Identity
    execution_epoch: PositiveInt
    snapshot_version: PositiveInt
    blocks: list[ContentBlock]
    covered_cursor: RealtimeCursor | None = None
    business_seq: UInt
    phase: Literal['partial', 'approval', 'final', 'interrupted']
    incomplete: bool
    # Historical process/tool split position: Python codepoints, not UTF-8.
    process_end: UInt | None = None

    @model_validator(mode='after')
    def snapshot_shape(self):
        if self.message_id != message_id(self.task_id, self.reply_id):
            raise ValueError('Snapshot identity does not match Task and Reply')
        if self.covered_cursor is not None and self.covered_cursor.conversation_id != self.conversation_id:
            raise ValueError('Snapshot cursor belongs to another Conversation')
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError('Snapshot block identifiers must be unique')
        if self.phase == 'final' and self.incomplete:
            raise ValueError('Final snapshot cannot be incomplete')
        if self.phase in {'final', 'interrupted'} and any(not block.closed for block in self.blocks):
            raise ValueError('Terminal snapshot blocks must be closed')
        return self


class MessageSnapshotReference(WireValue):
    """Internal boundary proof of the exact display cut, without its body."""

    message_id: Identity
    snapshot_version: PositiveInt
    execution_epoch: PositiveInt
    sha256: Annotated[str, Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')]

    @classmethod
    def from_snapshot(cls, snapshot: MessageSnapshot):
        from workmate_contracts import snapshot_digest
        snapshot=MessageSnapshot.model_validate(snapshot.model_dump(mode='json'))
        return cls(message_id=snapshot.message_id,snapshot_version=snapshot.snapshot_version,
            execution_epoch=snapshot.execution_epoch,sha256=snapshot_digest(snapshot.model_dump(mode='json')))


class RealtimeReset(WireValue):
    schema_version: Literal[1] = 1
    conversation_id: Identity
    generation: str
    reason: Literal['initial', 'generation_changed', 'trimmed', 'legacy_cursor',
                    'content_gap', 'log_lost', 'slow_client']
    snapshots: list[MessageSnapshot]
    business_state: dict[str, JsonValue]
    business_seq: UInt
    sync_status: Literal['synced', 'awaiting_snapshot', 'incomplete']

    _validate_generation = field_validator('generation')(_generation)

    @model_validator(mode='after')
    def reset_shape(self):
        if any(snapshot.conversation_id != self.conversation_id for snapshot in self.snapshots):
            raise ValueError('Reset snapshots belong to another Conversation')
        if len({snapshot.message_id for snapshot in self.snapshots}) != len(self.snapshots):
            raise ValueError('Reset message identifiers must be unique')
        if self.sync_status == 'synced' and any(snapshot.incomplete for snapshot in self.snapshots):
            raise ValueError('Synced reset cannot contain incomplete snapshots')
        if self.sync_status == 'synced' and any(
                snapshot.phase in {'partial', 'approval'} and
                (snapshot.covered_cursor is None or snapshot.covered_cursor.generation != self.generation)
                for snapshot in self.snapshots):
            raise ValueError('Synced nonterminal snapshot requires an anchor in the current generation')
        if any(snapshot.business_seq > self.business_seq for snapshot in self.snapshots):
            raise ValueError('Snapshot business sequence is ahead of reset')
        return self


class TextState(WireValue):
    message_id: Identity
    block_id: Identity
    content: str
    content_version: UInt
    end_utf8: UInt
    closed: bool
    execution_epoch: PositiveInt

    @model_validator(mode='after')
    def byte_length(self):
        if len(_utf8(self.content)) != self.end_utf8:
            raise ValueError('Text state length must use UTF-8 bytes')
        return self


class DeltaResult(WireValue):
    action: Literal['apply', 'ignore', 'reset', 'reject']
    state: TextState
    reason: str | None = None


def apply_text_delta(state: TextState, event: RealtimeEnvelope) -> DeltaResult:
    """Apply one byte-ranged block update without changing durable snapshot cuts.

    A terminal snapshot initializes ``closed=True``; late transport events then
    cannot append after the committed final content. Higher epochs can continue
    the same Reply after approval, but content versions do not restart.
    """
    def result(action, reason):
        return DeltaResult(action=action, state=state, reason=reason)
    delta = event.text
    if event.source != 'worker' or event.payload['type'] not in TEXT_EVENTS or delta is None:
        return result('reject', 'not_text_delta')
    if delta.message_id != state.message_id or delta.block_id != state.block_id:
        return result('reject', 'identity_mismatch')
    if event.execution_epoch < state.execution_epoch:
        return result('ignore', 'stale_epoch')
    if state.closed:
        return result('ignore', 'closed')
    if delta.content_version < state.content_version:
        return result('ignore', 'stale_version')
    if delta.content_version > state.content_version + 1:
        return result('reset', 'version_gap')
    if delta.from_offset > state.end_utf8:
        return result('reset', 'offset_gap')
    current, incoming = _utf8(state.content), _utf8(event.payload['delta'])
    overlap_end = min(delta.to_offset, state.end_utf8)
    try:
        current[:delta.from_offset].decode('utf-8')
        current[:overlap_end].decode('utf-8')
    except UnicodeError:
        return result('reject', 'invalid_utf8_boundary')
    overlap = overlap_end - delta.from_offset
    if current[delta.from_offset:overlap_end] != incoming[:overlap]:
        return result('reset', 'content_conflict')
    if delta.content_version == state.content_version:
        if delta.to_offset > state.end_utf8:
            return result('reset', 'version_conflict')
        return result('ignore', 'duplicate')
    try:
        content = (current + incoming[overlap:]).decode('utf-8')
    except UnicodeError:
        return result('reject', 'invalid_utf8_boundary')
    updated = TextState(**{**state.model_dump(), 'content': content,
        'content_version': delta.content_version, 'end_utf8': len(_utf8(content)),
        'execution_epoch': event.execution_epoch})
    return DeltaResult(action='apply', state=updated)
