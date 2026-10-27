"""Conservative transport pruning; caller owns transactions and lifecycle locks."""
from sqlalchemy import text
from .conversation_purge import ARTIFACT_STORAGE_LOCK
from ...projections.store import lock_product_conversation

# Only fully externalized display deltas can be reconstructed independently of
# transport. Boundary, audit, exact Core, commands and sequence rows never enter.
_CANDIDATES = '''
SELECT r.id,r.session_id,r.task_id,s.conversation_id FROM runtime_events r
JOIN runtime_sessions s ON s.id=r.session_id
JOIN conversations c ON c.id=s.conversation_id
JOIN tasks t ON t.id=r.task_id
JOIN runtime_task_executions x ON x.task_id=t.id
JOIN task_projection_states p ON p.task_id=t.id
JOIN runtime_projection_cursors cursor ON cursor.session_id=r.session_id
WHERE r.payload->>'type' IN ('TOOL_CALL_DELTA','TOOL_RESULT_TEXT_DELTA')
AND r.created_at < clock_timestamp()-make_interval(days=>:days)
AND t.updated_at < clock_timestamp()-make_interval(days=>:days)
AND t.status IN ('completed','failed','cancelled','interrupted')
AND x.status IN ('completed','failed','cancelled','interrupted')
AND p.state ? 'delivery_closed'
AND c.active_product_task_id IS NULL AND s.active_task_id IS NULL AND s.owner_id IS NULL AND s.lease_until IS NULL
AND NOT EXISTS (SELECT 1 FROM runtime_suspensions pause WHERE pause.task_id=t.id
    AND (pause.status<>'released' OR pause.resume_command_id IS NULL))
AND r.runtime_seq<=cursor.last_runtime_seq AND r.runtime_seq<=p.last_runtime_seq
AND NOT EXISTS (SELECT 1 FROM runtime_commands k WHERE k.task_id=t.id
                AND k.status IN ('accepted','claimed','indeterminate'))
AND EXISTS (SELECT 1 FROM task_events a WHERE a.task_id=t.id AND a.event_type='tool_content'
    AND a.payload->>'toolCallId'=r.payload->>'tool_call_id'
    AND a.payload->>'kind'=CASE r.payload->>'type' WHEN 'TOOL_CALL_DELTA' THEN 'arguments' ELSE 'result' END
    AND a.payload->>'incomplete'='false' AND a.payload->>'sha256' ~ '^[0-9a-f]{64}$')
AND NOT EXISTS (SELECT 1 FROM product_events e LEFT JOIN realtime_business_outbox o ON o.event_id=e.id
    WHERE e.source_runtime_event_id=r.id AND
      (e.business_operation_key IS NOT NULL OR o.status IS DISTINCT FROM 'published'
       OR o.published_at IS NULL OR o.published_at>=clock_timestamp()-make_interval(days=>:days)))
'''


def transaction(db):
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise ValueError('Retention requires a caller-owned PostgreSQL transaction')


async def candidate_conversations(db, *, days, limit):
    transaction(db)
    return list(await db.scalars(text('SELECT DISTINCT conversation_id FROM ('+_CANDIDATES+
        ') candidates ORDER BY conversation_id LIMIT :limit'),dict(days=days,limit=limit)))


async def batch(db, conversation_id, *, days, limit, apply):
    transaction(db)
    if not await lock_product_conversation(db,conversation_id,skip_locked=True):
        return dict(runtime_events=0,product_events=0,outbox=0)
    # Conversation serializes projection/control/delivery. Session lock fences
    # direct Worker ownership renewal before the eligibility recheck.
    candidate_ids=list(await db.scalars(text('SELECT DISTINCT task_id FROM ('+_CANDIDATES+
        ') candidates WHERE conversation_id=:cid ORDER BY task_id LIMIT :limit'),
        dict(days=days,cid=conversation_id,limit=limit)))
    if not candidate_ids: return dict(runtime_events=0,product_events=0,outbox=0)
    await db.execute(text('SELECT id FROM tasks WHERE id=ANY(CAST(:ids AS text[])) ORDER BY id FOR UPDATE'),dict(ids=candidate_ids))
    await db.execute(text('SELECT id FROM runtime_sessions WHERE conversation_id=:id FOR UPDATE'),dict(id=conversation_id))
    ids=list(await db.scalars(text('SELECT id FROM ('+_CANDIDATES+
        ') candidates WHERE conversation_id=:cid AND task_id=ANY(CAST(:tasks AS text[])) ORDER BY id LIMIT :limit'),
        dict(days=days,cid=conversation_id,limit=limit,tasks=candidate_ids)))
    values=dict(ids=ids)
    result=dict(runtime_events=len(ids),product_events=0,outbox=0)
    if not ids:
        return result
    result['product_events']=await db.scalar(text('SELECT count(*) FROM product_events WHERE source_runtime_event_id=ANY(CAST(:ids AS text[]))'),values)
    result['outbox']=await db.scalar(text('SELECT count(*) FROM realtime_business_outbox WHERE event_id IN (SELECT id FROM product_events WHERE source_runtime_event_id=ANY(CAST(:ids AS text[])))'),values)
    if apply:
        await db.execute(text('DELETE FROM realtime_business_outbox WHERE event_id IN (SELECT id FROM product_events WHERE source_runtime_event_id=ANY(CAST(:ids AS text[])))'),values)
        await db.execute(text('DELETE FROM product_events WHERE source_runtime_event_id=ANY(CAST(:ids AS text[]))'),values)
        await db.execute(text('DELETE FROM runtime_events WHERE id=ANY(CAST(:ids AS text[]))'),values)
    return result


async def lock_object_gc(db):
    transaction(db)
    if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),dict(key=ARTIFACT_STORAGE_LOCK)):
        raise ValueError('Product storage maintenance is already running')


async def assert_no_writers(db):
    transaction(db)
    unsafe=await db.scalar(text('''SELECT
        EXISTS(SELECT 1 FROM runtime_sessions WHERE owner_id IS NOT NULL OR active_task_id IS NOT NULL)
        OR EXISTS(SELECT 1 FROM conversations WHERE active_product_task_id IS NOT NULL)
        OR EXISTS(SELECT 1 FROM runtime_commands WHERE status IN ('accepted','claimed','indeterminate'))'''))
    if unsafe:
        raise ValueError('Object deletion requires no owned sessions, active gates or unsettled commands')
