"""Bounded-memory display capture; Core and approval objects remain untouched.

Only display args/text are compacted. Tool execution, permissions and resumable
Core snapshots retain their exact original data. No database I/O occurs here.
"""
import asyncio
import hashlib
import json
import tempfile
from workmate_contracts import MAX_PAYLOAD_BYTES


_DELTAS = {'TOOL_CALL_DELTA': 'arguments', 'TOOL_RESULT_TEXT_DELTA': 'result'}
_ENDS = {'TOOL_CALL_END': 'arguments', 'TOOL_RESULT_END': 'result'}
_SPECIAL = {'Skill', 'search_knowledge'}


class ToolContentCapture:
    def __init__(self, store, *, inline_bytes=65536, preview_chars=2048):
        if store is None:
            raise ValueError('Tool content requires an object store')
        self.store = store
        self.inline_bytes = inline_bytes
        self.preview_chars = preview_chars
        self.buffers = {}
        self.special = set()

    async def transform(self, events):
        output = []
        for source in events:
            raw = dict(source)
            kind, tool_id = raw.get('type'), raw.get('tool_call_id')
            if kind in {'TOOL_CALL_START', 'TOOL_RESULT_START'} and raw.get('tool_call_name') in _SPECIAL:
                self.special.add(tool_id)
            if tool_id in self.special:
                output.append(raw)
                continue
            if kind in _DELTAS:
                key = (tool_id, _DELTAS[kind])
                if key not in self.buffers:
                    self.buffers[key] = (dict(raw), tempfile.SpooledTemporaryFile(max_size=self.inline_bytes))
                self.buffers[key][1].write(raw.get('delta', '').encode('utf-8'))
                continue
            if kind in _ENDS:
                output.extend(await self._finish((tool_id, _ENDS[kind]), incomplete=False))
            if kind == 'TOOL_RESULT_END' and raw.get('metadata'):
                # Write/Edit include entire unified diffs in metadata. Keeping
                # those here would negate externalizing the argument body.
                encoded = json.dumps(raw['metadata'], ensure_ascii=False, allow_nan=False).encode('utf-8')
                if (len(encoded) > self.inline_bytes
                        or len(json.dumps(raw, allow_nan=False).encode()) > MAX_PAYLOAD_BYTES - 4096):
                    spool = tempfile.SpooledTemporaryFile(max_size=self.inline_bytes)
                    spool.write(encoded)
                    key = (tool_id, 'metadata')
                    self.buffers[key] = (dict(raw), spool)
                    references = await self._finish(key, incomplete=False)
                    output.extend(references)
                    # Keep useful bounded scalar hints (e.g. file_path and
                    # occurrences); exact nested/large metadata is in the object.
                    summary = {name: value for name, value in raw['metadata'].items()
                        if isinstance(name, str) and len(name) <= 128
                        and (value is None or type(value) in {bool, int, float}
                             or isinstance(value, str) and len(value) <= 256)}
                    raw['metadata'] = summary if len(json.dumps(summary).encode()) <= self.inline_bytes else {}
            if kind in {'REPLY_END', 'REQUIRE_USER_CONFIRM', 'REQUIRE_EXTERNAL_EXECUTION'}:
                output.extend(await self.flush())
            output.append(raw)
        return output

    async def _finish(self, key, *, incomplete):
        item = self.buffers.get(key)
        if item is None:
            return []
        raw, spool = item
        size = spool.seek(0, 2)
        spool.seek(0)
        inline = None
        if key[1] != 'metadata' and size <= self.inline_bytes:
            candidate = {**raw, 'delta': spool.read().decode('utf-8')}
            if len(json.dumps(candidate, allow_nan=False).encode()) <= MAX_PAYLOAD_BYTES - 4096:
                inline = candidate
        if inline is not None:
            events = [inline]
        else:
            spool.seek(0)
            checksum = hashlib.sha256()
            while chunk := spool.read(65536):
                checksum.update(chunk)
            digest = checksum.hexdigest()
            storage_key = f'.tool-content/sha256/{digest[:2]}/{digest}'
            # Join object publication on cancellation, before the caller retires
            # this capture. A failed PG append may leave a harmless orphan object.
            operation = asyncio.create_task(asyncio.to_thread(self.store.publish_file, storage_key, spool, digest, size))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if operation.done() and not operation.cancelled():
                    operation.exception()
                raise
            spool.seek(0)
            preview = spool.read(self.preview_chars * 4).decode('utf-8', errors='ignore')[:self.preview_chars]
            descriptor = dict(kind=key[1], sha256=digest, size_bytes=size,
                              storage_key=storage_key, preview=preview, incomplete=incomplete)
            events = [{'type': 'TOOL_CONTENT_REFERENCE', 'reply_id': raw['reply_id'],
                       'tool_call_id': key[0], 'content_ref': descriptor}]
            if key[1] != 'metadata':
                events.insert(0, {**raw, 'delta': preview})
        del self.buffers[key]
        spool.close()
        return events

    async def flush(self):
        output = []
        for key in list(self.buffers):
            output.extend(await self._finish(key, incomplete=True))
        return output

    def close(self):
        for _, spool in self.buffers.values():
            spool.close()
        self.buffers.clear()
