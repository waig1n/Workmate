"""Caller-owned queries for durable display tool content descriptors."""
from sqlalchemy import text


async def find_content(db, task_id, tool_call_id, kind):
    value = await db.scalar(text('''
        SELECT payload FROM task_events
        WHERE task_id=:task_id AND event_type='tool_content'
          AND payload->>'toolCallId'=:tool_call_id AND payload->>'kind'=:kind
        ORDER BY id DESC LIMIT 1
    '''), dict(task_id=task_id, tool_call_id=tool_call_id, kind=kind))
    if value is None:
        return None
    digest = value['sha256']
    return dict(storage_key=f'.tool-content/sha256/{digest[:2]}/{digest}',
                sha256=digest, size_bytes=value['sizeBytes'])


async def referenced_keys(db):
    values = await db.scalars(text('''
        SELECT DISTINCT payload->>'sha256' FROM task_events WHERE event_type='tool_content'
        UNION
        SELECT DISTINCT payload->'content_ref'->>'sha256' FROM runtime_events
        WHERE payload->>'type'='TOOL_CONTENT_REFERENCE'
    '''))
    return {f'.tool-content/sha256/{digest[:2]}/{digest}' for digest in values if digest}
