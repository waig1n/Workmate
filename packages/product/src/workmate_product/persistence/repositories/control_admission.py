"""SQL for control intent admission.

Uses the caller transaction; Application owns validation and lock ordering.
Statement timing, raw SQL, and PostgreSQL lock modes are deliberately retained.
"""
import json

from sqlalchemy import text
from .command_deliveries import stage

async def row(db, sql, **params):
    return (await db.execute(text(sql), params)).mappings().first()


async def conversation_agent(db, *, id):
    return await row(db, 'SELECT agent_id FROM conversations WHERE id=:id', id=id)


async def lock_agent(db, *, id):
    return await row(db, 'SELECT id,deleted_at FROM agents WHERE id=:id FOR SHARE', id=id)


async def lock_conversation(db, *, id):
    return await row(db, 'SELECT * FROM conversations WHERE id=:id FOR UPDATE', id=id)


async def actor_status(db, actor):
    return await db.scalar(text('SELECT status FROM users WHERE id=:id'), {'id': actor})


async def lock_task(db, *, id):
    return await row(db, 'SELECT * FROM tasks WHERE id=:id FOR UPDATE', id=id)


async def lock_session(db, *, id):
    return await row(db, 'SELECT * FROM runtime_sessions WHERE id=:id FOR UPDATE', id=id)


async def lock_execution(db, *, id):
    return await row(db, 'SELECT * FROM runtime_task_executions WHERE task_id=:id FOR UPDATE', id=id)


async def task_execution(db, *, id):
    return await row(db, 'SELECT session_id FROM runtime_task_executions WHERE task_id=:id', id=id)


async def insert_command(db, command, actor, digest):
    result = await db.execute(text('''INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
            kind,expected_epoch,status,payload,protocol_version,content_digest,reply_id,interaction_id,request_version)
            VALUES (:id,:sid,:cid,:tid,:actor,:kind,:epoch,'accepted',CAST(:payload AS jsonb),
            :protocol,:digest,:reply,:interaction,:request)'''),
            {'id': command.command_id, 'sid': command.runtime_session_id, 'cid': command.conversation_id,
             'tid': command.product_task_id, 'actor': actor, 'kind': command.kind, 'epoch': command.expected_epoch,
             'payload': command.canonical_payload().decode(), 'protocol': command.protocol_version,
             'digest': digest, 'reply': command.reply_id, 'interaction': command.interaction_id,
             'request': command.request_version})
    await stage(db, command.command_id)
    return result


async def append_audit(db, command, event_type, payload):
    return await db.execute(text('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
            SELECT CAST(:tid AS varchar),CAST(:cid AS varchar),coalesce(max(seq),0)+1,CAST(:type AS varchar),CAST(:payload AS json)
            FROM task_events WHERE task_id=:tid'''),
            {'tid': command.product_task_id, 'cid': command.conversation_id, 'type': event_type,
             'payload': json.dumps(payload)})


async def snapshot(db, *, id):
    return await row(db, 'SELECT * FROM runtime_snapshots WHERE id=:id', id=id)


async def lock_projection(db, *, id):
    return await row(db, 'SELECT * FROM task_projection_states WHERE task_id=:id FOR UPDATE', id=id)


async def projection_cursor(db, *, id):
    return await row(db, 'SELECT * FROM runtime_projection_cursors WHERE conversation_id=:id', id=id)


async def task_history(db, command):
    return (await db.execute(text('SELECT event_type,payload FROM task_events WHERE task_id=:id'),
                                    {'id': command.product_task_id})).mappings().all()


async def command(db, *, id):
    return await row(db, 'SELECT * FROM runtime_commands WHERE id=:id', id=id)


async def resume_decision(db, *, id, version):
    return await row(db, '''SELECT * FROM runtime_commands WHERE task_id=:id
                AND kind='ResumeInteraction' AND payload->>'decision_version'=:version''', id=id, version=version)


async def outstanding_resume(db, *, id, snapshot=None):
    if snapshot is None:
        return await row(db, """SELECT id FROM runtime_commands WHERE task_id=:id
            AND kind='ResumeInteraction' AND status IN ('accepted','claimed','indeterminate')""", id=id)
    return await row(db, """SELECT id FROM runtime_commands WHERE task_id=:id
                AND kind='ResumeInteraction' AND (status IN ('accepted','claimed','indeterminate')
                OR payload->>'snapshot_id'=:snapshot)""", id=id, snapshot=snapshot)


async def set_decision_version(db, payload, task):
    return await db.execute(text('UPDATE tasks SET last_decision_version=:version WHERE id=:id'),
                             {'version': payload.decision_version, 'id': task['id']})


async def save_interaction_state(db, task, updated):
    return await db.execute(text('UPDATE task_projection_states SET state=CAST(:state AS jsonb) WHERE task_id=:id'),
                             {'state': json.dumps(updated), 'id': task['id']})


async def lock_organization_scopes(db):
    return await db.execute(text("SELECT id FROM scopes WHERE kind='organization' ORDER BY id FOR UPDATE"))


async def lock_disabled_user(db, *, id):
    return await row(db, 'SELECT * FROM users WHERE id=:id FOR UPDATE', id=id)


async def disable_receipt(db, *, id):
    return await row(db, 'SELECT * FROM security_audit_events WHERE id=:id', id=id)


async def lock_agent_identity(db, candidate):
    return await db.execute(text('SELECT id FROM agents WHERE id=:id FOR SHARE'), {'id': candidate['agent_id']})


async def stop_command(db, *, id):
    return await row(db, "SELECT * FROM runtime_commands WHERE task_id=:id AND kind='StopTask'", id=id)


async def reject_pending_commands(db, task):
    return await db.execute(text("""UPDATE runtime_commands SET status='rejected' WHERE task_id=:id
            AND kind IN ('StartTask','ResumeInteraction') AND status='accepted'"""), {'id': task['id']})


async def mark_cancelling(db, task):
    return await db.execute(text("UPDATE tasks SET status='cancelling' WHERE id=:id"), {'id': task['id']})
