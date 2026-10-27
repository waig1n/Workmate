"""Task control reads and queued cancellation writes on the caller Session.

Application coordinates Agent -> Conversation -> Task locks, exact approval
identity and commits. Queries preserve their original timing, including boolean
short-circuit reads. No Session creation or transaction ownership lives here.
"""
import json
from sqlalchemy import text
from ..models import Agent


async def conversation_agent(db, conversation_id):
    return await db.scalar(text('SELECT agent_id FROM conversations WHERE id=:id'),{'id':conversation_id})


async def lock_agent(db, agent_id):
    return await db.execute(text('SELECT id FROM agents WHERE id=:id FOR SHARE'),{'id':agent_id})


async def cancel_queued_task(db, task_id):
    return await db.execute(text("UPDATE tasks SET status='cancelled' WHERE id=:id"),{'id':task_id})


async def audit_user_cancellation(db, task_id, conversation_id, actor_user_id):
    return await db.execute(text('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
                    SELECT CAST(:tid AS varchar),CAST(:cid AS varchar),coalesce(max(seq),0)+1,'task_cancelled',CAST(:payload AS json)
                    FROM task_events WHERE task_id=:tid'''),{'tid':task_id,'cid':conversation_id,'payload':json.dumps({'actorUserId':actor_user_id,'reason':'user_requested','queued':True})})


async def audit_disabled_cancellation(db, task_id, conversation_id, actor_user_id, target_user_id, disable_event_id):
    return await db.execute(text("""INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
                        SELECT CAST(:tid AS varchar),CAST(:cid AS varchar),coalesce(max(seq),0)+1,
                        'task_cancelled',CAST(:payload AS json) FROM task_events WHERE task_id=:tid"""),
                        {'tid': task_id, 'cid': conversation_id, 'payload': json.dumps({
                            'actorUserId': actor_user_id, 'targetUserId': target_user_id,
                            'reason': 'account_disabled', 'queued': True, 'disableEventId': disable_event_id})})


async def conversation_deleted_at(db, conversation_id):
    return await db.scalar(text('SELECT deleted_at FROM conversations WHERE id=:id'),{'id':conversation_id})


async def actor_status(db, actor_user_id):
    return await db.scalar(text('SELECT status FROM users WHERE id=:id'),{'id':actor_user_id})


async def task_command(db, task_id):
    return await db.scalar(text('SELECT id FROM runtime_commands WHERE task_id=:id LIMIT 1'),{'id':task_id})


async def tasks_for_disabled_user(db, target_user_id):
    return (await db.execute(text("""SELECT t.id,c.id AS conversation_id FROM tasks t
            JOIN conversations c ON c.id=t.conversation_id WHERE c.user_id=:id
            AND t.status IN ('created','running','awaiting_permission','cancelling')
            ORDER BY c.id,t.created_at,t.id"""), {'id': target_user_id})).mappings().all()


async def active_session(db, task_id):
    return await db.scalar(text('SELECT id FROM runtime_sessions WHERE active_task_id=:id LIMIT 1'),
                                               {'id': task_id})


async def existing_command(db, command_id):
    return (await db.execute(text('SELECT * FROM runtime_commands WHERE id=:id'),{'id':command_id})).mappings().first()


async def lock_task(db, task_id):
    return (await db.execute(text('SELECT * FROM tasks WHERE id=:id FOR UPDATE'),{'id':task_id})).mappings().first()


async def task_execution(db, task_id):
    return (await db.execute(text('SELECT * FROM runtime_task_executions WHERE task_id=:id'),{'id':task_id})).mappings().first()


async def approval_boundary(db, task_id):
    return (await db.execute(text('''SELECT e.*,p.state FROM runtime_task_executions e
                    JOIN task_projection_states p ON p.task_id=e.task_id WHERE e.task_id=:id'''),{'id':task_id})).mappings().first()



async def agent_for_access(db, agent_id):
    return await db.get(Agent, agent_id)
