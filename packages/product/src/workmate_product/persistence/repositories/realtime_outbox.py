"""Reliable business notification intents, owned by the caller transaction.

The outbox references the sanitized Product event instead of copying its body.
No connection management, commits, Redis IO, tool calls or execution authority.
Publishers lease only outbox rows, and never hold those leases' row locks across
network IO. An expired claim cannot acknowledge or reschedule a newer claim.
"""
from dataclasses import dataclass
import math
import re
from uuid import uuid4

from sqlalchemy import text

from workmate_contracts import ProductEvent
from workmate_contracts.realtime import JS_SAFE_INTEGER, RealtimeCursor
from ...realtime.classification import is_business_event


class OutboxConflict(ValueError):
    """An immutable business intent conflicts with its persisted source."""


@dataclass(frozen=True)
class BusinessClaim:
    event: ProductEvent
    business_seq: int
    claim_token: str
    attempt_count: int


def _transaction(db):
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise ValueError('Requires a caller-owned PostgreSQL transaction')


def _event(row):
    return ProductEvent(product_event_id=row['id'],conversation_id=row['conversation_id'],
        product_seq=row['product_seq'],product_task_id=row['task_id'],reply_id=row['reply_id'],
        event_type=row['event_type'],occurred_at=row['occurred_at'],payload=row['payload'],
        source_runtime_event_id=row['source_runtime_event_id'])


async def stage(db, event: ProductEvent, *, business_seq: int):
    """Stage alongside the fact; caller already holds the Conversation lock."""
    _transaction(db)
    event=ProductEvent.model_validate(event.model_dump())
    if type(business_seq) is not int or not 1 <= business_seq <= JS_SAFE_INTEGER:
        raise ValueError('Business sequence must be a positive JavaScript-safe integer')
    if len(event.product_event_id) > 128:
        raise ValueError('Business event identity exceeds storage bounds')
    if not is_business_event(event.event_type,event.payload):
        raise ValueError('An ephemeral public event must not enter the business outbox')
    row=(await db.execute(text('SELECT * FROM product_events WHERE id=:id'),
        {'id':event.product_event_id})).mappings().first()
    if row is None or _event(row) != event or row['business_seq'] != business_seq:
        raise OutboxConflict('Business intent does not match its persisted Product event')
    result=await db.execute(text('''INSERT INTO realtime_business_outbox(event_id,conversation_id,business_seq)
        VALUES (:id,:cid,:seq) ON CONFLICT DO NOTHING'''),
        {'id':event.product_event_id,'cid':event.conversation_id,'seq':business_seq})
    if result.rowcount == 1:
        return True
    existing=(await db.execute(text('''SELECT conversation_id,business_seq FROM realtime_business_outbox
        WHERE event_id=:id'''),{'id':event.product_event_id})).mappings().first()
    if existing is None or existing['conversation_id'] != event.conversation_id or existing['business_seq'] != business_seq:
        raise OutboxConflict('Business event identity or sequence already has another intent')
    return False


async def lease_due(db, *, limit=32, lease_seconds=30):
    _transaction(db)
    if (type(limit) is not int or not 1 <= limit <= 1000
            or type(lease_seconds) not in (int,float) or not 0 < lease_seconds <= 3600
            or not math.isfinite(lease_seconds)):
        raise ValueError('Invalid publication lease bounds')
    rows=(await db.execute(text('''SELECT e.*,d.business_seq,d.attempt_count FROM realtime_business_outbox d
        JOIN product_events e ON e.id=d.event_id
        WHERE ((d.status='pending' AND d.next_attempt_at<=clock_timestamp())
           OR (d.status='publishing' AND d.lease_until<=clock_timestamp()))
          AND NOT EXISTS (SELECT 1 FROM realtime_business_outbox earlier
            WHERE earlier.conversation_id=d.conversation_id AND earlier.business_seq<d.business_seq
            AND earlier.status<>'published')
        ORDER BY d.next_attempt_at,d.conversation_id,d.business_seq
        LIMIT :limit FOR UPDATE OF d SKIP LOCKED'''),{'limit':limit})).mappings().all()
    claims=[]
    for row in rows:
        try:
            event=_event(row)
            if not is_business_event(event.event_type,event.payload):
                raise ValueError('Ephemeral intent')
        except ValueError:
            await db.execute(text('''UPDATE realtime_business_outbox SET status='blocked',
                claim_token=NULL,lease_until=NULL,attempt_count=attempt_count+1,error_code='protocol_invalid'
                WHERE event_id=:id'''),{'id':row['id']})
            continue
        token=str(uuid4())
        await db.execute(text('''UPDATE realtime_business_outbox SET status='publishing',
            claim_token=:token,lease_until=clock_timestamp()+:seconds*interval '1 second',
            attempt_count=attempt_count+1 WHERE event_id=:id'''),
            {'id':row['id'],'token':token,'seconds':lease_seconds})
        claims.append(BusinessClaim(event,row['business_seq'],token,row['attempt_count']+1))
    return claims


def _claim_params(claim):
    return {'id':claim.event.product_event_id,'token':claim.claim_token}


async def published(db, claim: BusinessClaim, *, generation: str, incarnation: str, stream_id: str):
    _transaction(db)
    RealtimeCursor(conversation_id=claim.event.conversation_id,generation=generation,stream_id=stream_id)
    RealtimeCursor(conversation_id=claim.event.conversation_id,generation=incarnation,stream_id='0-0')
    # A plain EXISTS uses the statement snapshot and can race a concurrent log
    # rotation. SHARE fences the PG generation until this receipt commits.
    current=await db.scalar(text('''SELECT conversation_id FROM conversation_event_logs
        WHERE conversation_id=:cid AND generation=:generation AND incarnation=:incarnation
        AND status='active' FOR SHARE'''),
        {'cid':claim.event.conversation_id,'generation':generation,'incarnation':incarnation})
    if current is None:
        return False
    result=await db.execute(text('''UPDATE realtime_business_outbox SET status='published',
        published_generation=:generation,published_incarnation=:incarnation,published_stream_id=:position,
        published_at=clock_timestamp(),claim_token=NULL,lease_until=NULL,error_code=NULL
        WHERE event_id=:id AND status='publishing' AND claim_token=:token AND lease_until>clock_timestamp()'''),
        {**_claim_params(claim),'generation':generation,'incarnation':incarnation,'position':stream_id})
    return result.rowcount == 1


async def failed(db, claim: BusinessClaim, *, error_code: str, retry_seconds: float, blocked=False):
    _transaction(db)
    if (not isinstance(error_code,str) or re.fullmatch(r'[a-z][a-z0-9_]{0,63}',error_code) is None
            or type(retry_seconds) not in (int,float) or not 0 <= retry_seconds <= 86400
            or not math.isfinite(retry_seconds) or type(blocked) is not bool):
        raise ValueError('Invalid publication failure metadata')
    result=await db.execute(text('''UPDATE realtime_business_outbox SET status=:status,
        claim_token=NULL,lease_until=NULL,error_code=:error,
        next_attempt_at=clock_timestamp()+:seconds*interval '1 second'
        WHERE event_id=:id AND status='publishing' AND claim_token=:token AND lease_until>clock_timestamp()'''),
        {**_claim_params(claim),'status':'blocked' if blocked else 'pending','error':error_code,'seconds':retry_seconds})
    return result.rowcount == 1
