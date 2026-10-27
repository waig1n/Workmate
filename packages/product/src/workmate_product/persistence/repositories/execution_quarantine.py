"""Quarantine persistence; caller owns transaction and Conversation lock.

Failure reporting preserves both execution gates and all Runtime ownership facts.
"""
import json
from sqlalchemy import select, text
from workmate_product.persistence.models import Task


async def read_task_error(db, *, task_id, conversation_id):
    return await db.scalar(select(Task.error).where(
        Task.id == task_id, Task.conversation_id == conversation_id))


async def lock_execution_context(db, *, task_id, session_id):
    task = (await db.execute(text('SELECT * FROM tasks WHERE id=:id FOR UPDATE'), {'id': task_id})).mappings().first()
    session = (await db.execute(text('SELECT * FROM runtime_sessions WHERE id=:id FOR UPDATE'), {'id': session_id})).mappings().first()
    execution = (await db.execute(text('SELECT * FROM runtime_task_executions WHERE task_id=:id FOR UPDATE'), {'id': task_id})).mappings().first()
    return task, session, execution


async def record_failure(db, *, task_id, error):
    await db.execute(text("UPDATE tasks SET status='failed',error=CAST(:error AS json),updated_at=clock_timestamp() WHERE id=:id"),
        {'id': task_id, 'error': json.dumps(error)})


async def read_failure_binding(db, *, command_id, session_id, task_id, worker_id, storage_node_id):
    """Discover this owner's binding; quarantine rechecks it under its row locks.

    Reuse the caller's transaction and never substitute a new owner or epoch.
    """
    return (await db.execute(text('''SELECT s.conversation_id,s.execution_epoch
        FROM runtime_sessions s
        JOIN runtime_task_executions e ON e.session_id=s.id AND e.task_id=s.active_task_id
        JOIN runtime_commands c ON c.session_id=s.id AND c.task_id=e.task_id
        WHERE c.id=:command AND s.id=:session AND e.task_id=:task
            AND s.owner_id=:worker AND s.storage_node_id=:node
            AND e.execution_epoch=s.execution_epoch'''),
        {'command': command_id, 'session': session_id, 'task': task_id,
         'worker': worker_id, 'node': storage_node_id})).mappings().first()


async def set_report_lock_timeout(db):
    """Bound row-lock waits inside the caller's current report transaction."""
    await db.execute(text("SET LOCAL lock_timeout = '1s'"))


async def expired_candidates(db, *, limit=100):
    """Bounded failure reconciliation only; never execution candidates."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Invalid expired execution report limit')
    return list((await db.execute(text('''
        SELECT s.conversation_id, t.id AS task_id, s.id AS session_id,
            s.owner_id AS worker_id, s.execution_epoch, s.storage_node_id
        FROM runtime_sessions s JOIN conversations c ON c.id=s.conversation_id
        JOIN tasks t ON t.id=s.active_task_id AND t.id=c.active_product_task_id
        JOIN runtime_task_executions e ON e.task_id=t.id AND e.session_id=s.id
        WHERE c.deleted_at IS NULL AND t.status IN ('created','running','awaiting_permission','cancelling')
          AND s.status IN ('preparing','running','awaiting_interaction','stopping')
          AND s.owner_id IS NOT NULL AND s.lease_until<=clock_timestamp()
          AND s.execution_epoch>0 AND e.execution_epoch=s.execution_epoch
          AND e.status=s.status AND e.state_version=s.state_version
          AND e.reply_id IS NOT DISTINCT FROM s.reply_id
          AND (t.run_id IS NULL OR t.run_id=s.reply_id)
        ORDER BY s.lease_until,s.id LIMIT :limit
    '''), {'limit':limit})).mappings())


async def lease_expired(db, *, session_id):
    return await db.scalar(text('SELECT lease_until<=clock_timestamp() FROM runtime_sessions WHERE id=:id'),
                           {'id':session_id}) is True
