"""Operator command inspection/redrive persistence; joins the caller transaction."""
import json
from uuid import uuid4
from sqlalchemy import text
from . import control_admission as controls
from .command_deliveries import _command, _route


async def inspect_deliveries(db, *, command_id=None, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError('invalid_inspection_limit')
    rows=await db.execute(text('''SELECT d.command_id,d.delivery_generation,d.retry_origin_generation,
        d.storage_node_id,d.target_incarnation,d.channel,d.status AS delivery_status,d.attempt_count,
        d.error_code,d.published_at,d.next_attempt_at,c.kind,c.status AS command_status,
        c.task_id,c.session_id,c.claimed_by,c.claimed_epoch,s.owner_id AS session_owner_id,
        s.lease_until,t.status AS task_status
        FROM command_deliveries d JOIN runtime_commands c ON c.id=d.command_id
        JOIN runtime_sessions s ON s.id=c.session_id LEFT JOIN tasks t ON t.id=c.task_id
        WHERE (CAST(:id AS varchar) IS NULL OR c.id=CAST(:id AS varchar)) ORDER BY d.created_at DESC,d.delivery_generation DESC LIMIT :limit'''),
        {'id':command_id,'limit':limit})
    return [dict(row) for row in rows.mappings()]


async def lock_context(db, command_id):
    hint=await _command(db,command_id)
    if not hint:
        return None
    candidate=await controls.conversation_agent(db,id=hint['conversation_id'])
    if not candidate:
        return None
    agent=await controls.lock_agent(db,id=candidate['agent_id'])
    conversation=await controls.lock_conversation(db,id=hint['conversation_id'])
    task=await controls.lock_task(db,id=hint['task_id'])
    session=await controls.lock_session(db,id=hint['session_id'])
    execution=await controls.lock_execution(db,id=hint['task_id'])
    command=await _command(db,command_id,lock=True)
    delivery=(await db.execute(text('''SELECT * FROM command_deliveries WHERE command_id=:id
        ORDER BY delivery_generation DESC LIMIT 1 FOR UPDATE'''),{'id':command_id})).mappings().first()
    return command,delivery,agent,conversation,task,session,execution


async def suspension(db, session):
    return (await db.execute(text('''SELECT p.*,b.payload AS snapshot_payload FROM runtime_suspensions p
        JOIN runtime_snapshots b ON b.id=p.snapshot_id WHERE p.snapshot_id=:id'''),
        {'id':session['snapshot_id']})).mappings().first()


async def audit_rows(db, task_id):
    return (await db.execute(text('SELECT event_type,payload FROM task_events WHERE task_id=:id'),
        {'id':task_id})).mappings().all()


async def redrive(db, command, delivery, *, operator_id, reason):
    """Application validated all authority under ordered row locks first."""
    generation=delivery['delivery_generation']+1
    route=_route(command)
    await db.execute(text('''INSERT INTO command_deliveries(command_id,delivery_generation,retry_origin_generation,
        storage_node_id,target_incarnation,channel) VALUES (:id,:generation,:generation,:node,:worker,:channel)'''),
        {'id':command['id'],'generation':generation,**route})
    await db.execute(text('''INSERT INTO security_audit_events(id,event_type,actor_user_id,target_user_id,metadata)
        VALUES (:id,'command_delivery.redrive',:actor,:target,CAST(:metadata AS json))'''),
        {'id':str(uuid4()),'actor':operator_id,'target':command['actor_user_id'],
         'metadata':json.dumps({'command_id':command['id'],'previous_generation':delivery['delivery_generation'],
             'delivery_generation':generation,'storage_node_id':route['node'],'reason':reason})})
    return generation


async def user(db, user_id):
    return await controls.row(db,'SELECT * FROM users WHERE id=:id',id=user_id)


async def inspect_quarantined_messages(db, *, command_id=None, limit=100):
    """Malformed/unknown queue messages have no delivery row or even command ID."""
    if not 1 <= limit <= 1000:
        raise ValueError('invalid_inspection_limit')
    rows=await db.execute(text('''SELECT stream_key,message_id,command_id,error_code,created_at
        FROM command_delivery_quarantines
        WHERE (CAST(:id AS varchar) IS NULL OR command_id=CAST(:id AS varchar))
        ORDER BY created_at DESC,stream_key,message_id LIMIT :limit'''),{'id':command_id,'limit':limit})
    return [dict(row) for row in rows.mappings()]
