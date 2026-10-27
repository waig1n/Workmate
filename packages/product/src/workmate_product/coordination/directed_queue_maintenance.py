"""Bounded maintenance mechanics for directed Streams, never node work queues.

Subclasses establish durable disposition before ACK. Consumer-group/last-entry
metadata is retained; queue-key deletion requires a separate producer fence.
"""
from collections import deque
import re

from .command_queue import QueueRoute, _bounded, _text


class DirectedQueueMaintenance:
    consumer = 'gateway-maintenance'

    def __init__(self, sessions, queue, registry, *, suffixes):
        self.sessions, self.queue, self.registry = sessions, queue, registry
        self._scan_cursor = 0
        self._streams = deque()
        self._pending_after = {}
        self._pending_first = {}
        self._pattern = re.compile(re.escape(f'workmate:{queue.namespace}:worker:') +
            r'([A-Za-z0-9_.-]{1,128}):(' + '|'.join(re.escape(suffix) for suffix in suffixes) + r')\Z')
        self._scan_pattern = f'workmate:{queue.namespace}:worker:*:' + (suffixes[0] if len(suffixes)==1 else '*')

    async def _next_route(self, count):
        if not self._streams:
            self._scan_cursor, keys = await self.queue.client.scan(
                cursor=self._scan_cursor, match=self._scan_pattern, count=count, _type='stream')
            for key in keys:
                try:
                    match = self._pattern.fullmatch(_text(key))
                except UnicodeError:
                    continue
                if match:
                    # Node does not enter directed keys. Disposition verifies
                    # each envelope's actual node against its PG record.
                    self._streams.append(QueueRoute('maintenance', match[1], control=match[2]!='execute'))
        return self._streams.popleft() if self._streams else None

    async def _disposition(self, route, message_id, fields):
        raise NotImplementedError

    async def _records(self, route, count):
        prefer_pending = not self._pending_first.get(route, False)
        self._pending_first[route] = prefer_pending
        pending_count = count//2 + (count % 2 if prefer_pending else 0)
        unread_count = count - pending_count
        records = []
        if pending_count:
            pending = await self.queue.pending(route, count=pending_count,
                start=self._pending_after.get(route, '-'))
            if len(pending) == pending_count:
                self._pending_after[route] = '(' + pending[-1].message_id
            else:
                self._pending_after.pop(route, None)
            for item in pending:
                records.extend(await self.queue.client.xrange(self.queue.stream_key(route),
                    min=item.message_id, max=item.message_id, count=1))
            unread_count += pending_count - len(pending)
        if unread_count:
            streams = await self.queue.client.xreadgroup(self.queue.group, self.consumer,
                {self.queue.stream_key(route):'>'}, count=unread_count)
            records.extend(record for _, batch in streams for record in batch)
        return records

    async def tick(self, *, count: int, retain_ms: int) -> bool:
        """Visit at most one stream and count messages; caller schedules the next page."""
        _bounded(count)
        _bounded(retain_ms, minimum=0, maximum=2**53-1)
        route = await self._next_route(count)
        if route is None:
            return False
        health = await self.registry.worker(route.worker_id, allow_draining=True)
        if health is None:
            await self.queue.ensure_group(route)
            for message_id, fields in await self._records(route, count):
                if await self._disposition(route, _text(message_id), fields):
                    await self.queue.ack(route, [_text(message_id)])
        # This requires all consumer-group confirmation progress, including any
        # PEL entries held by the original Worker. It never trims unread records.
        await self.queue.trim_acknowledged(route, retain_ms=retain_ms)
        return True
