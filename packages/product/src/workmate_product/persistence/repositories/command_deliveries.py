"""Durable dispatch persistence, using the caller's transaction.

No Redis IO, command payload copying, execution claims, commits or connection
management. Delivery generations fence transport routing, never execution epochs.
Lock order is Session -> Residency -> Command -> Delivery; publishers lock only Delivery.
"""
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text


@dataclass(frozen=True)
class DeliveryClaim:
    command_id: str
    delivery_generation: int
    storage_node_id: str
    target_incarnation: str | None
    channel: str
    lease_token: str
    attempt_count: int


@dataclass(frozen=True)
class Classification:
    action: str
    reason: str
    command: dict | None = None


async def _row(db, sql, **params):
    return (await db.execute(text(sql), params)).mappings().first()


async def _command(db, command_id, *, lock=False):
    if lock:
        await db.execute(text('''SELECT s.id FROM runtime_sessions s JOIN runtime_commands c
            ON c.session_id=s.id WHERE c.id=:id FOR UPDATE OF s'''), {'id': command_id})
    if lock:
        await db.execute(text('''SELECT r.session_id FROM workspace_residencies r JOIN runtime_commands c
            ON c.session_id=r.session_id WHERE c.id=:id FOR UPDATE OF r'''), {'id': command_id})
    return await _row(db, '''SELECT c.*,s.owner_id AS session_owner_id,s.superseded_at AS session_superseded_at,
        s.status AS session_status,s.active_task_id AS session_active_task_id,
        s.snapshot_id AS session_snapshot_id,s.storage_node_id,t.status AS task_status,
        s.execution_epoch AS session_epoch,s.lease_until>clock_timestamp() AS session_lease_live,
        r.status AS residency_status,r.holder_worker_id AS residency_holder
        FROM runtime_commands c JOIN runtime_sessions s ON s.id=c.session_id
        LEFT JOIN tasks t ON t.id=c.task_id
        LEFT JOIN workspace_residencies r ON r.session_id=s.id WHERE c.id=:id'''
        + (' FOR UPDATE OF c' if lock else ''), id=command_id)


def _route(command):
    # An expired owner is not proof that its resources have stopped. Q1 safe
    # release clears owner_id; only that durable fact permits node routing.
    worker=command['session_owner_id']
    if worker is None and command['residency_status']=='held':
        worker=command['residency_holder']
    return {'node': command['storage_node_id'], 'worker': worker,
            'channel': 'control' if command['kind'] == 'StopTask' or (
                command['kind'] == 'ResumeInteraction' and command['session_owner_id'] is not None) else 'execute'}


def _residency_wait(command):
    return command['session_owner_id'] is None and command['residency_status'] in {'releasing','blocked'}


async def stage(db, command_id):
    """Called after INSERT command in the same admission transaction."""
    command = await _command(db, command_id)
    if not command or command['kind'] not in ('StartTask','ResumeInteraction','StopTask'):
        raise ValueError('Unsupported dispatch command')
    if command['status'] != 'accepted' or command['session_superseded_at'] is not None:
        return False
    result = await db.execute(text('''INSERT INTO command_deliveries
        (command_id,delivery_generation,storage_node_id,target_incarnation,channel)
        VALUES (:id,1,:node,:worker,:channel) ON CONFLICT DO NOTHING'''),
        {'id': command_id, **_route(command)})
    return result.rowcount == 1


async def lease_due(db, *, limit=32, lease_seconds=30):
    """Lease publication briefly; caller commits before XADD."""
    if not 1 <= limit <= 1000 or lease_seconds <= 0:
        raise ValueError('Invalid dispatch lease bounds')
    rows = (await db.execute(text('''SELECT d.* FROM command_deliveries d
        JOIN runtime_commands c ON c.id=d.command_id
        JOIN runtime_sessions s ON s.id=c.session_id
        WHERE s.superseded_at IS NULL AND c.status='accepted' AND ((d.status='pending' AND d.next_attempt_at<=clock_timestamp())
          OR (d.status='publishing' AND d.lease_until<=clock_timestamp()))
        ORDER BY d.next_attempt_at,d.command_id,d.delivery_generation
        LIMIT :limit FOR UPDATE OF d SKIP LOCKED'''), {'limit':limit})).mappings().all()
    claims=[]
    for row in rows:
        token=uuid4().hex
        await db.execute(text('''UPDATE command_deliveries SET status='publishing',
            lease_token=:token,lease_until=clock_timestamp()+:seconds*interval '1 second',attempt_count=attempt_count+1
            WHERE command_id=:id AND delivery_generation=:generation'''),
            {'id':row['command_id'],'generation':row['delivery_generation'],'token':token,'seconds':lease_seconds})
        claims.append(DeliveryClaim(row['command_id'],row['delivery_generation'],row['storage_node_id'],
            row['target_incarnation'],row['channel'],token,row['attempt_count']+1))
    return claims


def _claim_params(claim):
    return {'id':claim.command_id,'generation':claim.delivery_generation,'token':claim.lease_token}


async def published(db, claim, stream_id):
    result=await db.execute(text('''UPDATE command_deliveries SET status='published',published_at=clock_timestamp(),
        stream_message_id=:mid,lease_token=NULL,lease_until=NULL,error_code=NULL
        WHERE command_id=:id AND delivery_generation=:generation AND status='publishing'
        AND lease_token=:token AND lease_until>clock_timestamp()'''),{**_claim_params(claim),'mid':stream_id})
    return result.rowcount == 1


async def failed(db, claim, error, *, next_attempt_at: datetime, max_attempts=10):
    if not error or len(error)>128 or max_attempts<1 or next_attempt_at.tzinfo is None:
        raise ValueError('Invalid delivery failure policy')
    result=await db.execute(text('''UPDATE command_deliveries
        SET status=CASE WHEN attempt_count>=:max_attempts THEN 'quarantined' ELSE 'pending' END,
        next_attempt_at=:next,error_code=:error,lease_token=NULL,lease_until=NULL
        WHERE command_id=:id AND delivery_generation=:generation AND status='publishing'
        AND lease_token=:token AND lease_until>clock_timestamp()'''),
        {**_claim_params(claim),'next':next_attempt_at,'error':error,'max_attempts':max_attempts})
    return result.rowcount == 1


async def reroute(db, command_id, *, expected_generation, target_worker=None, max_generations=20):
    """Persist a new route before the previous message can be acknowledged.

    A warm-worker hint may only target an ownerless session. Current actual owner
    always overrides hints. Callers must validate warm-worker health/generation.
    """
    if max_generations<1:
        raise ValueError('Invalid route retry limit')
    command=await _command(db,command_id,lock=True)
    if not command or command['session_superseded_at'] is not None or command['status'] != 'accepted' or _residency_wait(command):
        return None
    latest=await _row(db,'''SELECT * FROM command_deliveries WHERE command_id=:id
        ORDER BY delivery_generation DESC LIMIT 1 FOR UPDATE''',id=command_id)
    if not latest or latest['delivery_generation'] != expected_generation or latest['status'] in ('settled','quarantined','superseded'):
        return None
    if expected_generation-latest['retry_origin_generation']+1>=max_generations:
        await db.execute(text('''UPDATE command_deliveries SET status='quarantined',
            lease_token=NULL,lease_until=NULL,error_code='route_retry_exhausted'
            WHERE command_id=:id AND delivery_generation=:generation'''),
            {'id':command_id,'generation':expected_generation})
        return None
    route=_route(command)
    if route['worker'] is None and command['residency_status'] is None:
        route['worker']=target_worker
    generation=expected_generation+1
    await db.execute(text('''UPDATE command_deliveries SET status='superseded',lease_token=NULL,lease_until=NULL,
        error_code='route_replaced' WHERE command_id=:id AND delivery_generation=:generation'''),
        {'id':command_id,'generation':expected_generation})
    await db.execute(text('''INSERT INTO command_deliveries
        (command_id,delivery_generation,retry_origin_generation,storage_node_id,target_incarnation,channel)
        VALUES (:id,:generation,:origin,:node,:worker,:channel)'''),
        {'id':command_id,'generation':generation,'origin':latest['retry_origin_generation'],**route})
    return generation


async def repair_candidates(db, *, older_than: datetime, limit=32):
    """Bounded low-frequency repair input, never execution scheduling.

    Inspect latest generations only; returned claimed records are reconciliation
    input, not permission to replay. Missing accepted intent can be staged.
    """
    if not 1<=limit<=1000 or older_than.tzinfo is None:
        raise ValueError('Invalid repair bounds')
    return [dict(row) for row in (await db.execute(text('''SELECT c.id AS command_id,c.status AS command_status,
        d.delivery_generation,d.status AS delivery_status,d.published_at,d.target_incarnation,
        d.stream_message_id,d.channel,
        s.owner_id AS session_owner_id,s.storage_node_id,c.kind,s.superseded_at AS session_superseded_at,
        r.status AS residency_status,r.holder_worker_id AS residency_holder
        FROM runtime_commands c JOIN runtime_sessions s ON s.id=c.session_id
        LEFT JOIN workspace_residencies r ON r.session_id=s.id
        LEFT JOIN LATERAL (SELECT * FROM command_deliveries WHERE command_id=c.id
            ORDER BY delivery_generation DESC LIMIT 1) d ON true
        WHERE c.kind IN ('StartTask','ResumeInteraction','StopTask') AND
          ((s.superseded_at IS NOT NULL AND d.status IN ('pending','publishing','published')) OR
           (s.superseded_at IS NULL AND d.command_id IS NULL AND c.status='accepted') OR
           (d.status='published' AND d.published_at<:cutoff AND d.next_attempt_at<=clock_timestamp()) OR
           (d.status IN ('pending','publishing') AND c.status IN ('completed','rejected','indeterminate')) OR
           (d.status IN ('pending','publishing') AND c.status='claimed' AND d.created_at<:cutoff
            AND d.next_attempt_at<=clock_timestamp()))
        ORDER BY COALESCE(d.published_at,c.created_at),c.id LIMIT :limit'''),
        {'cutoff':older_than,'limit':limit})).mappings()]


def repair_route_changed(command):
    if _residency_wait(command):
        return False
    route=_route(command)
    return command['target_incarnation']!=route['worker'] or command['channel']!=route['channel']


async def touch_repair(db, command_id, generation, *, next_attempt_at: datetime):
    """Defer inspected in-flight records so old live PEL cannot starve repair."""
    if next_attempt_at.tzinfo is None:
        raise ValueError('Repair time must include timezone')
    result=await db.execute(text("""UPDATE command_deliveries SET next_attempt_at=:next
        WHERE command_id=:id AND delivery_generation=:generation AND status IN ('pending','publishing','published')"""),
        {'id':command_id,'generation':generation,'next':next_attempt_at})
    return result.rowcount == 1


async def settle(db, command_id, generation):
    """Record terminal transport disposition only from durable command facts."""
    command=await _command(db,command_id,lock=True)
    if not command or command['status'] not in ('completed','rejected','indeterminate'):
        return False
    status='quarantined' if command['status']=='indeterminate' else 'settled'
    result=await db.execute(text('''UPDATE command_deliveries SET status=:status,
        lease_token=NULL,lease_until=NULL,error_code=:error
        WHERE command_id=:id AND delivery_generation=:generation
          AND status NOT IN ('superseded','settled','quarantined')'''),
        {'id':command_id,'generation':generation,'status':status,
         'error':'execution_indeterminate' if status=='quarantined' else None})
    return result.rowcount == 1


async def quarantine_superseded(db, command_id, generation):
    """End delivery only; retain the old command and execution ownership evidence."""
    command=await _command(db,command_id,lock=True)
    if not command or command['session_superseded_at'] is None:
        return False
    result=await db.execute(text("""UPDATE command_deliveries SET status='quarantined',
        error_code='session_superseded',lease_token=NULL,lease_until=NULL
        WHERE command_id=:id AND delivery_generation=:generation
          AND status NOT IN ('superseded','settled','quarantined')"""),
        {'id':command_id,'generation':generation})
    return result.rowcount == 1


async def quarantine_abandoned(db, command_id, generation):
    """Fence queue replay of execution whose owner no longer has authority.

    This does not prove tools stopped, clear execution gates or change command
    status. It records the reason the transport can stop retrying and requires
    existing execution reconciliation before any new execution is considered.
    """
    command=await _command(db,command_id,lock=True)
    if not command or command['status'] not in ('claimed','indeterminate'):
        return False
    if command['status']=='claimed' and (command['claimed_by']==command['session_owner_id']
            and command['claimed_epoch']==command['session_epoch'] and command['session_lease_live']):
        return False
    result=await db.execute(text("""UPDATE command_deliveries SET status='quarantined',
        error_code='execution_owner_lost',lease_token=NULL,lease_until=NULL
        WHERE command_id=:id AND delivery_generation=:generation
        AND status NOT IN ('superseded','settled','quarantined')"""),
        {'id':command_id,'generation':generation})
    return result.rowcount == 1


async def quarantine_message(db, stream, message_id, error, command_id=None):
    """Keep only bounded location/reason; untrusted payload may contain secrets."""
    if not stream or len(stream)>512 or not message_id or len(message_id)>128 or not error or len(error)>128:
        raise ValueError('Invalid quarantine metadata')
    if command_id is not None and len(command_id)>128:
        command_id=None
    await db.execute(text('''INSERT INTO command_delivery_quarantines(stream_key,message_id,command_id,error_code)
        VALUES (:stream,:mid,:id,:error) ON CONFLICT DO NOTHING'''),
        {'stream':stream,'mid':message_id,'id':command_id,'error':error})


async def classify(db, envelope, *, worker_id, node_id, max_generations=20):
    """Validate transport identity against PG before handing off to execution.

    Invalid envelope errors require the caller to persist message quarantine
    before ACK. A positive result is only a hint: the existing execution claim
    transaction still rechecks ownership, actor authorization and Task state.
    """
    command=await _command(db,envelope.command_id,lock=True)
    if not command:
        raise ValueError('unknown_command')
    row=await _row(db,'''SELECT * FROM command_deliveries WHERE command_id=:id
        AND delivery_generation=:generation FOR UPDATE''',id=envelope.command_id,
        generation=envelope.delivery_generation)
    if not row:
        raise ValueError('unknown_delivery_generation')
    if (row['storage_node_id'] != envelope.storage_node_id
            or row['target_incarnation'] != envelope.target_incarnation):
        raise ValueError('envelope_route_mismatch')
    current={**command,'delivery_channel':row['channel']}
    if row['status'] in ('superseded','settled','quarantined'):
        return Classification('ack',row['status'],current)
    if command['session_superseded_at'] is not None:
        await quarantine_superseded(db,envelope.command_id,envelope.delivery_generation)
        return Classification('ack','session_superseded',current)
    if command['status'] in ('completed','rejected','indeterminate'):
        await settle(db,envelope.command_id,envelope.delivery_generation)
        return Classification('ack',command['status'],current)
    if command['status']=='claimed':
        return Classification('wait','execution_claimed',current)
    if _residency_wait(command):
        return Classification('wait','workspace_release_pending',current)
    route=_route(command)
    # A released suspension may have cleared owner after publication. Persist
    # the replacement first; never ACK the sole surviving command notification.
    if row['channel']!=route['channel'] or row['storage_node_id']!=route['node'] or (route['worker'] is not None and row['target_incarnation']!=route['worker']) or (
            row['target_incarnation'] is not None and route['worker'] is None):
        generation=await reroute(db,envelope.command_id,expected_generation=envelope.delivery_generation,
            max_generations=max_generations)
        if generation is None:
            disposition=await _row(db,'SELECT status FROM command_deliveries WHERE command_id=:id '
                'AND delivery_generation=:generation',id=envelope.command_id,generation=envelope.delivery_generation)
            if disposition and disposition['status']=='quarantined':
                return Classification('ack','route_retry_exhausted',current)
        return Classification('ack' if generation else 'wait','route_changed',current)
    if node_id!=route['node'] or (row['target_incarnation'] is not None and worker_id!=row['target_incarnation']):
        raise ValueError('consumer_route_mismatch')
    return Classification('control' if route['channel']=='control' else 'execute','accepted',current)


async def quarantine_unclaimed(db, command_id, generation, error='execution_claim_rejected'):
    """Record an execution admission refusal, without changing Product intent."""
    if not error or len(error)>128:
        raise ValueError('Invalid quarantine reason')
    command=await _command(db,command_id,lock=True)
    if not command or command['status']!='accepted' or _residency_wait(command):
        return False
    route=_route(command)
    delivery=await _row(db,'SELECT * FROM command_deliveries WHERE command_id=:id '
        'AND delivery_generation=:generation FOR UPDATE',id=command_id,generation=generation)
    if delivery and (delivery['target_incarnation']!=route['worker'] or delivery['channel']!=route['channel']):
        await reroute(db,command_id,expected_generation=generation)
        return False
    result=await db.execute(text("""UPDATE command_deliveries SET status='quarantined',
        error_code=:error,lease_token=NULL,lease_until=NULL
        WHERE command_id=:id AND delivery_generation=:generation
        AND status NOT IN ('superseded','settled','quarantined')
        AND NOT EXISTS(SELECT 1 FROM command_deliveries newer WHERE newer.command_id=:id
            AND newer.delivery_generation>:generation)"""),
        {'id':command_id,'generation':generation,'error':error})
    return result.rowcount==1
