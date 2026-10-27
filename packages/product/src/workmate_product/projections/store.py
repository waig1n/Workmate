"""Short caller-owned PostgreSQL projection transactions and replay.

Projectors are trusted application functions: no commit, external I/O or upper
level governance locks after entry. Notifications happen only after caller commit.
"""
from workmate_product.coordination import stage_wakeup
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import uuid

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import text

from workmate_contracts import ProductCursor, ProductEvent, RuntimeEvent


STREAM_BATCH_EVENTS = 16
STREAM_BATCH_BYTES = 128 * 1024
STREAM_DELTAS = frozenset({'TEXT_BLOCK_DELTA', 'TOOL_CALL_DELTA', 'TOOL_RESULT_TEXT_DELTA'})


class ProjectionConflict(ValueError):
    """Identity, source continuity or durable state cannot be safely reconciled."""


class ProjectedEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    key: str = Field(min_length=1,max_length=128)
    event_type: str = Field(min_length=1,max_length=128)
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class ProjectionResult:
    state: dict
    events: list[ProjectedEvent]


@dataclass(frozen=True)
class EventPage:
    events: tuple[ProductEvent, ...]
    cursor: ProductCursor
    high_watermark: int


def _transaction(db):
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise ProjectionConflict('Requires a caller-owned PostgreSQL transaction')


def _json(value):
    return json.dumps(value, allow_nan=False, sort_keys=True)


def _items(items):
    if len(items) > 100 or len({item.key for item in items}) != len(items):
        raise ProjectionConflict('Projection item identities must be unique and bounded')
    for item in items:
        if item.payload.get('type') != item.event_type:
            raise ProjectionConflict('Public event type disagrees with payload')
        if len(_json(item.payload).encode()) > 262144:
            raise ProjectionConflict('Product event exceeds 256 KiB')


async def lock_product_conversation(db,cid,*,skip_locked=False):
    """Lock referenced Agent before Conversation, including implicit FK locks.

    Repeated product_seq updates in one transaction can recheck Agent's FK even
    when agent_id did not change. Taking Agent KEY SHARE first prevents a cycle
    against authority checks that lock Agent before Conversation (shared for
    readers, exclusive for mutations).
    KEY SHARE preserves independent Conversation consumer concurrency. Callers
    must enter with no later-ranked row locks, or already hold this Agent lock.
    """
    candidate=(await db.execute(text('SELECT agent_id FROM conversations WHERE id=:id'),
                                {'id':cid})).mappings().first()
    if candidate is None:
        if skip_locked:
            return None
        raise ProjectionConflict('Conversation does not exist')
    suffix=' SKIP LOCKED' if skip_locked else ''
    agent=await db.scalar(text('SELECT id FROM agents WHERE id=:id FOR KEY SHARE'+suffix),
                          {'id':candidate['agent_id']})
    if agent is None:
        if skip_locked:
            return None
        raise ProjectionConflict('Conversation Agent does not exist')
    row=(await db.execute(text('SELECT * FROM conversations WHERE id=:id FOR UPDATE'+suffix),
                           {'id':cid})).mappings().first()
    if row is None:
        if skip_locked:
            return None
        raise ProjectionConflict('Conversation does not exist')
    if row['agent_id']!=agent:
        raise ProjectionConflict('Conversation Agent changed during lock acquisition')
    return row


async def _conversation(db,cid):
    return await lock_product_conversation(db,cid)


async def _append(db,cid,items,*,task_id,reply_id,source_id=None,operation=None,occurred_at):
    _items(items)
    from workmate_product.realtime.classification import is_business_event
    from workmate_product.persistence.repositories.realtime_outbox import stage
    classified=[(item,is_business_event(item.event_type,item.payload)) for item in items]
    seqs=[]
    for item,important in classified:
        counters=(await db.execute(text('''UPDATE conversations SET product_seq=product_seq+1,
            business_seq=business_seq+:increment WHERE id=:id RETURNING product_seq,business_seq'''),
            {'id':cid,'increment':1 if important else 0})).mappings().one()
        seq=counters['product_seq']
        business_seq=counters['business_seq'] if important else None
        event_id=str(uuid.uuid4())
        await db.execute(text('''INSERT INTO product_events(id,conversation_id,product_seq,business_seq,task_id,reply_id,
            source_runtime_event_id,projection_item_key,business_operation_key,event_type,occurred_at,payload)
            VALUES (:id,:cid,:seq,:business_seq,:tid,:reply,:source,:item,:op,:type,:at,CAST(:payload AS jsonb))'''),
            {'id':event_id,'cid':cid,'seq':seq,'business_seq':business_seq,'tid':task_id,'reply':reply_id,'source':source_id,
             'item':item.key,'op':operation,'type':item.event_type,'at':occurred_at,'payload':_json(item.payload)})
        # Admission can precede both browser and Worker log initialization.
        # Stage from the committed Product fact, never from Redis readiness.
        if important:
            await stage(db,ProductEvent(product_event_id=event_id,conversation_id=cid,product_seq=seq,
                product_task_id=task_id,reply_id=reply_id,event_type=item.event_type,occurred_at=occurred_at,
                payload=item.payload,source_runtime_event_id=source_id),business_seq=business_seq)
        seqs.append(seq)
    if seqs:
        stage_wakeup(db, "conversation:" + cid)
    return tuple(seqs)


async def consume_next(db, conversation_id: str, *, project) -> RuntimeEvent | None:
    """Apply exactly one source event; failures roll back the caller-visible savepoint."""
    return await _consume(db, conversation_id, project=project, limit=1, skip_locked=False)


async def consume_batch(db, conversation_id: str, *, project, skip_locked=False) -> RuntimeEvent | None:
    """Fold a bounded contiguous delta batch, or one boundary; return the last event."""
    return await _consume(db, conversation_id, project=project,
                          limit=STREAM_BATCH_EVENTS, skip_locked=skip_locked)


def _batch_rows(rows):
    first = rows[0]
    raw = first['payload']
    if not isinstance(raw, dict):
        raise ProjectionConflict('Runtime source payload must be an object')
    identity = (first['task_id'], first['execution_epoch'], raw.get('reply_id'))
    size = 0
    for index, row in enumerate(rows):
        payload = row['payload']
        if not isinstance(payload, dict):
            raise ProjectionConflict('Runtime source payload must be an object')
        if index and (raw.get('type') not in STREAM_DELTAS
                      or payload.get('type') not in STREAM_DELTAS
                      or (row['task_id'], row['execution_epoch'], payload.get('reply_id')) != identity):
            break
        length = len(_json(payload).encode())
        if index and size + length > STREAM_BATCH_BYTES:
            break
        yield row
        size += length


async def _consume(db, conversation_id, *, project, limit, skip_locked):
    _transaction(db)
    async with db.begin_nested():
        if not await lock_product_conversation(db, conversation_id, skip_locked=skip_locked):
            return None
        session=(await db.execute(text('SELECT id,runtime_seq FROM runtime_sessions WHERE conversation_id=:id AND superseded_at IS NULL'),
                                   {'id':conversation_id})).mappings().first()
        if session is None:
            return None
        cursor=(await db.execute(text('SELECT * FROM runtime_projection_cursors WHERE conversation_id=:id'),
                                  {'id':conversation_id})).mappings().first()
        if cursor and cursor['session_id'] != session['id']:
            raise ProjectionConflict('Cursor belongs to a different Session')
        last=cursor['last_runtime_seq'] if cursor else 0
        rows=(await db.execute(text('''SELECT * FROM runtime_events WHERE session_id=:sid AND runtime_seq>:seq
            ORDER BY runtime_seq LIMIT :limit'''),{'sid':session['id'],'seq':last,'limit':limit})).mappings().all()
        if not rows:
            if session['runtime_seq'] > last:
                raise ProjectionConflict('Committed Runtime event is missing')
            return None
        row=rows[0]
        task=(await db.execute(text('SELECT id,conversation_id FROM tasks WHERE id=:id FOR UPDATE'),
                               {'id':row['task_id']})).mappings().first()
        execution=(await db.execute(text('SELECT * FROM runtime_task_executions WHERE task_id=:id'),
                                    {'id':row['task_id']})).mappings().first()
        saved=(await db.execute(text('SELECT * FROM task_projection_states WHERE task_id=:id'),
                                {'id':row['task_id']})).mappings().first()
        reply=row['payload'].get('reply_id') if isinstance(row['payload'], dict) else None
        if saved and (saved['format_version'] != 1 or saved['session_id'] != session['id']
                      or saved['reply_id'] != reply or saved['last_runtime_seq'] > last):
            raise ProjectionConflict('Unsupported or inconsistent projection state')
        state=json.loads(_json(saved['state'])) if saved else {}
        for row in _batch_rows(rows):
            if row['runtime_seq'] != last+1:
                raise ProjectionConflict('Runtime sequence has a gap')
            event, boundary = await _source_event(db, conversation_id, session, task, execution, row)
            result=await project(db,event,state,boundary)
            if not isinstance(result,ProjectionResult) or not isinstance(result.state,dict):
                raise ProjectionConflict('Projector must return explicit durable state and public events')
            await _append(db,conversation_id,result.events,task_id=task['id'],reply_id=reply,
                          source_id=event.runtime_event_id,occurred_at=event.occurred_at)
            state=result.state
            last=event.runtime_seq
        await db.execute(text('''INSERT INTO task_projection_states(task_id,session_id,reply_id,format_version,state,last_runtime_seq)
            VALUES (:tid,:sid,:reply,1,CAST(:state AS jsonb),:seq) ON CONFLICT (task_id) DO UPDATE
            SET state=EXCLUDED.state,last_runtime_seq=EXCLUDED.last_runtime_seq'''),
            {'tid':task['id'],'sid':session['id'],'reply':reply,'state':_json(state),'seq':event.runtime_seq})
        await db.execute(text('''INSERT INTO runtime_projection_cursors(conversation_id,session_id,last_runtime_seq,version)
            VALUES (:cid,:sid,:seq,1) ON CONFLICT (conversation_id) DO UPDATE
            SET last_runtime_seq=EXCLUDED.last_runtime_seq,version=runtime_projection_cursors.version+1'''),
            {'cid':conversation_id,'sid':session['id'],'seq':event.runtime_seq})
        if 'terminal_boundary_id' in state or 'terminal_control_event_id' in state:
            stage_wakeup(db, 'delivery')
        return event


async def _source_event(db, conversation_id, session, task, execution, row):
    raw=row['payload']
    reply=raw.get('reply_id') if isinstance(raw,dict) else None
    if (not task or task['conversation_id'] != conversation_id or not execution
            or execution['session_id'] != session['id']
            or (not reply and (not isinstance(raw,dict) or raw.get('type')!='EXECUTION_NOT_STARTED'))
            or reply != execution['reply_id'] or row['execution_epoch'] > execution['execution_epoch']
            or raw.get('session_id',session['id']) != session['id']):
        raise ProjectionConflict('Runtime source has inconsistent Task/Session/Reply identity')
    event=RuntimeEvent(runtime_event_id=row['id'],conversation_id=conversation_id,
        runtime_session_id=session['id'],product_task_id=task['id'],reply_id=reply,
        runtime_seq=row['runtime_seq'],execution_epoch=row['execution_epoch'],
        event_type=raw['type'],occurred_at=row['created_at'],payload=raw)
    if event.event_type == 'EXECUTION_NOT_STARTED':
        from .unstarted import validate_unstarted
        await validate_unstarted(db,event,execution)
    boundary=None
    if event.event_type == 'EXECUTION_BOUNDARY':
        boundary=(await db.execute(text('''SELECT * FROM runtime_snapshots WHERE session_id=:sid
            AND task_id=:tid AND execution_epoch=:epoch AND runtime_seq=:seq'''),
            {'sid':session['id'],'tid':task['id'],'epoch':event.execution_epoch,'seq':event.runtime_seq})).mappings().first()
        if not boundary or boundary['reply_id'] != reply or boundary['status'] != raw.get('status'):
            raise ProjectionConflict('Execution boundary has no matching immutable snapshot')
        boundary=dict(boundary)
    return event, boundary


async def append_business_events(db,conversation_id: str,*,operation_key: str,items:list[ProjectedEvent],
                                 task_id:str|None=None,reply_id:str|None=None) -> tuple[int,...]:
    """Append replay for a trusted Product operation; does not perform that operation."""
    _transaction(db)
    if not operation_key or len(operation_key)>128 or not items:
        raise ProjectionConflict('Business operation identity and events are required')
    _items(items)
    async with db.begin_nested():
        await _conversation(db,conversation_id)
        if task_id:
            tid=await db.scalar(text('SELECT id FROM tasks WHERE id=:id AND conversation_id=:cid FOR KEY SHARE'),
                                {'id':task_id,'cid':conversation_id})
            if not tid:
                raise ProjectionConflict('Task belongs to a different Conversation')
        existing=(await db.execute(text('''SELECT * FROM product_events
            WHERE conversation_id=:cid AND business_operation_key=:op ORDER BY product_seq'''),
            {'cid':conversation_id,'op':operation_key})).mappings().all()
        if existing:
            expected=[(item.key,item.event_type,item.payload,task_id,reply_id) for item in items]
            actual=[(r['projection_item_key'],r['event_type'],r['payload'],r['task_id'],r['reply_id']) for r in existing]
            if _json(actual) != _json(expected):
                raise ProjectionConflict('Business operation identity already has different content')
            return tuple(r['product_seq'] for r in existing)
        return await _append(db,conversation_id,items,task_id=task_id,reply_id=reply_id,
                             operation=operation_key,occurred_at=datetime.now(timezone.utc))


async def read_product_events(db,cursor:ProductCursor,*,limit:int=100) -> EventPage:
    """Trusted repository read: caller must authorize Conversation access first."""
    if type(limit) is not int or not 1<=limit<=1000:
        raise ValueError('limit must be an integer from 1 to 1000')
    high=await db.scalar(text('SELECT product_seq FROM conversations WHERE id=:id'),{'id':cursor.conversation_id})
    if high is None or cursor.product_seq>high:
        raise ProjectionConflict('Unknown Conversation or future Product cursor')
    rows=(await db.execute(text('''SELECT * FROM product_events WHERE conversation_id=:cid
        AND product_seq>:seq AND product_seq<=:high ORDER BY product_seq LIMIT :limit'''),
        {'cid':cursor.conversation_id,'seq':cursor.product_seq,'high':high,'limit':limit})).mappings().all()
    events=[]
    for row in rows:
        if row['product_seq'] != cursor.product_seq+len(events)+1:
            raise ProjectionConflict('Product replay has a gap; snapshot recovery is required')
        events.append(ProductEvent(product_event_id=row['id'],conversation_id=row['conversation_id'],
            product_seq=row['product_seq'],product_task_id=row['task_id'],reply_id=row['reply_id'],
            event_type=row['event_type'],occurred_at=row['occurred_at'],payload=row['payload'],
            source_runtime_event_id=row['source_runtime_event_id']))
    if not events and cursor.product_seq<high:
        raise ProjectionConflict('Product replay is missing; snapshot recovery is required')
    return EventPage(tuple(events),ProductCursor(conversation_id=cursor.conversation_id,
                     product_seq=events[-1].product_seq if events else cursor.product_seq),high)
