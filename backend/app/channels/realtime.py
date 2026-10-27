"""Authorized, bounded user-visible text feed for private IM delivery.

This consumes the same qualified resets and byte-ranged deltas as the workbench.
It does not read PostgreSQL per token or own channel binding authorization.
"""
import json

from workmate_contracts.realtime import (
    MessageSnapshot, RealtimeEnvelope, RealtimeReset, TextState, apply_text_delta, message_id,
)

from app.services.conversation_business_state import ConversationBusinessState


class ChannelTextGap(RuntimeError):
    """Abandon incremental state and reopen with an authorized reset."""


class ChannelTextReducer:
    def __init__(self, conversation_id, task_id, *, max_bytes=256 * 1024, max_blocks=256):
        self.conversation_id, self.task_id = conversation_id, task_id
        self.max_bytes, self.max_blocks = max_bytes, max_blocks
        self.blocks = {}
        self.process_ends = {}
        self.snapshot_versions = {}
        self.terminal_messages = set()
        self.ready = True

    def _bound(self):
        if (len(self.process_ends) > self.max_blocks or len(self.snapshot_versions) > self.max_blocks
                or len(self.blocks) > self.max_blocks
                or sum(block.end_utf8 for block in self.blocks.values()) > self.max_bytes):
            self.blocks.clear()
            raise ChannelTextGap('Channel display budget exceeded')

    def _snapshot(self, raw):
        snapshot = MessageSnapshot.model_validate(raw)
        if snapshot.conversation_id != self.conversation_id:
            raise ChannelTextGap('Snapshot belongs to another conversation')
        if snapshot.task_id != self.task_id:
            return
        mid = snapshot.message_id
        if snapshot.snapshot_version < self.snapshot_versions.get(mid, 0):
            return
        if snapshot.incomplete:
            raise ChannelTextGap('Incomplete channel snapshot')
        self.blocks = {key: value for key, value in self.blocks.items() if key[0] != mid}
        for block in snapshot.blocks:
            if block.kind == 'text':
                self.blocks[(mid, block.block_id)] = TextState(
                    message_id=mid, block_id=block.block_id, content=block.text,
                    content_version=block.content_version, end_utf8=len(block.text.encode()),
                    closed=block.closed, execution_epoch=snapshot.execution_epoch)
        self.process_ends[mid] = snapshot.process_end or 0
        self.snapshot_versions[mid] = snapshot.snapshot_version
        if snapshot.phase in {'final', 'interrupted'}:
            self.terminal_messages.add(mid)
        self._bound()

    def _event(self, raw):
        event = RealtimeEnvelope.model_validate(raw)
        if event.conversation_id != self.conversation_id:
            raise ChannelTextGap('Event belongs to another conversation')
        if event.task_id != self.task_id or not self.ready:
            return
        if event.reply_id and message_id(self.task_id, event.reply_id) in self.terminal_messages:
            return
        kind = event.payload['type']
        if kind == 'TOOL_CALL_START' and event.reply_id:
            mid = message_id(self.task_id, event.reply_id)
            self.process_ends[mid] = sum(len(value.content) for key, value in self.blocks.items() if key[0] == mid)
            self._bound()
            return
        if event.source != 'worker' or kind not in {'TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END'}:
            return
        mid = message_id(self.task_id, event.reply_id)
        block_id = event.payload.get('messageId')
        if not isinstance(block_id, str) or not block_id:
            raise ChannelTextGap('Missing channel text block identity')
        key = (mid, block_id)
        old = self.blocks.get(key)
        if kind == 'TEXT_MESSAGE_START':
            if old is None:
                self.blocks[key] = TextState(message_id=mid, block_id=block_id, content='',
                    content_version=0, end_utf8=0, closed=False, execution_epoch=event.execution_epoch)
        elif old is None:
            raise ChannelTextGap('Missing channel text prefix')
        elif kind == 'TEXT_MESSAGE_END':
            if event.execution_epoch >= old.execution_epoch:
                self.blocks[key] = old.model_copy(update={'closed': True})
        else:
            result = apply_text_delta(old, event)
            if result.action not in {'apply', 'ignore'}:
                raise ChannelTextGap(result.reason)
            self.blocks[key] = result.state
        self._bound()

    @property
    def text(self):
        if not self.ready:
            return ''
        messages = {}
        for (mid, _), block in self.blocks.items():
            messages[mid] = messages.get(mid, '') + block.content
        return '\n\n'.join(value[self.process_ends.get(mid, 0):] for mid, value in messages.items()).strip()

    def accept(self, name, value):
        if name == 'REALTIME_RESET':
            reset = RealtimeReset.model_validate(value)
            if reset.conversation_id != self.conversation_id:
                raise ChannelTextGap('Reset belongs to another conversation')
            self.blocks.clear()
            self.process_ends.clear()
            self.snapshot_versions.clear()
            self.terminal_messages.clear()
            self.ready = reset.sync_status == 'synced'
            if self.ready:
                for snapshot in reset.snapshots:
                    self._snapshot(snapshot)
        elif name == 'REALTIME_SNAPSHOT':
            if self.ready:
                self._snapshot(value)
        elif name == 'REALTIME_EVENT':
            self._event(value)
        return self.text


class ChannelTextFeed:
    def __init__(self, stream, sessions):
        self.stream, self.sessions = stream, sessions

    async def watch(self, conversation_id, task_id, user_id, on_text):
        authority = ConversationBusinessState(self.sessions, user_id, policy=self.stream.policy)
        handle = await self.stream.open(conversation_id, authority=authority)
        reducer = ChannelTextReducer(conversation_id, task_id)
        previous = ''
        try:
            async for frame in handle.frames():
                for line in frame.splitlines():
                    if not line.startswith('data: '):
                        continue
                    payload = json.loads(line[6:])
                    current = reducer.accept(payload.get('name'), payload.get('value'))
                    if current != previous:
                        await on_text(current)
                        previous = current
        finally:
            await handle.aclose()
