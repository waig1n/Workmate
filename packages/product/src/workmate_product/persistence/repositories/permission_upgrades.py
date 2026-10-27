"""SQL for task permission upgrade evidence.

Uses the caller transaction; Application owns validation and lock ordering.
Statement timing, raw SQL, and PostgreSQL lock modes are deliberately retained.
"""
import json
import uuid

from sqlalchemy import text

from .control_admission import row


async def sandbox_attestation(db, *, id, epoch):
    return await row(db, 'SELECT * FROM runtime_sandbox_attestations WHERE task_id=:id AND execution_epoch=:epoch', id=id, epoch=epoch)


async def recovery_boundary(db, session):
    return await row(db, "SELECT p.*,b.payload AS snapshot_payload FROM runtime_suspensions p JOIN runtime_snapshots b ON b.id=p.snapshot_id WHERE p.snapshot_id=:id AND p.status='released'",
                     id=session['snapshot_id'])


async def recovery_lineage(db, *, task_id, session_id, node_id, source_worker, source_epoch, worker_id, execution_epoch, allow_pending_last=False):
    from ..recovery_provenance import LINEAGE_SELECT, valid_lineage
    records = (await db.execute(text(LINEAGE_SELECT + ' WHERE p.task_id=:task AND p.session_id=:session'),
                               {'task':task_id,'session':session_id})).mappings().all()
    return valid_lineage(records,task_id=task_id,session_id=session_id,node_id=node_id,
        source_worker=source_worker,source_epoch=source_epoch,worker_id=worker_id,execution_epoch=execution_epoch,
        allow_pending_last=allow_pending_last)


async def execution_spec(db, *, id):
    return await row(db, 'SELECT * FROM runtime_execution_specs WHERE task_id=:id', id=id)


async def lease_is_current(db, session):
    return await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id'),{'id':session['id']})


async def previous_upgrade(db, *, id):
    return await row(db, 'SELECT command_id FROM runtime_permission_upgrades WHERE task_id=:id LIMIT 1', id=id)


async def conversation_actor(db, command):
    return await db.scalar(text('SELECT user_id FROM conversations WHERE id=:id'),{'id':command.conversation_id})


async def insert_upgrade(db, actor, command, session, task, version):
    return await db.execute(text('''INSERT INTO runtime_permission_upgrades(task_id,session_id,command_id,
            permission_version,state_version,actor_user_id,worker_id,execution_epoch,initial_profile)
            VALUES (:task,:session,:command,:version,:state,:actor,:worker,:epoch,:profile)'''),
            {'task':task['id'],'session':session['id'],'command':command.command_id,'version':version,'state':command.payload['state_version'],
             'actor':actor,
             'worker':session['owner_id'],'epoch':session['execution_epoch'],'profile':task['approval_profile']})


async def append_security_audit(db, actor, command, proof, task, version):
    return await db.execute(text('''INSERT INTO security_audit_events(id,event_type,actor_user_id,metadata)
            VALUES (:id,'task.full_access_enabled',:actor,CAST(:metadata AS json))'''),
            {'id':str(uuid.uuid4()),'actor':actor,'metadata':json.dumps({'task_id':task['id'],
                'conversation_id':command.conversation_id,'decision_version':version,'workspace_isolation':proof['isolation']})})


async def latest_upgrade(db, *, id):
    return await row(db, 'SELECT * FROM runtime_permission_upgrades WHERE task_id=:id ORDER BY state_version DESC LIMIT 1', id=id)


async def command(db, *, id):
    return await row(db, 'SELECT * FROM runtime_commands WHERE id=:id', id=id)
