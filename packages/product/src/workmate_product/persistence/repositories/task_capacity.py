"""Capacity observations; callers own transactions and admission lock ordering."""
from sqlalchemy import exists, func, or_, select, text

from ..models import Conversation, Task, RuntimeCommand, RuntimeTaskExecution, TaskEvent


async def lock_capacity(db):
    if db.bind.dialect.name == 'postgresql':
        await db.execute(text('SELECT pg_advisory_xact_lock(173542921, 1)'))


async def counts(db, user_id, *, exclude_task_id=None):
    # A persisted Resume reserves capacity before the Product running projection.
    resuming = exists(select(RuntimeCommand.id).where(
        RuntimeCommand.task_id == Task.id, RuntimeCommand.kind == 'ResumeInteraction',
        RuntimeCommand.status.in_(['accepted', 'claimed', 'indeterminate'])))
    executing = exists(select(RuntimeTaskExecution.task_id).where(
        RuntimeTaskExecution.task_id == Task.id,
        RuntimeTaskExecution.status.in_(['preparing', 'running', 'stopping'])))
    active = or_(Task.status.in_(['created', 'running', 'cancelling']),
        (Task.status == 'awaiting_permission') & or_(resuming, executing))
    pending = (Task.status == 'awaiting_permission') & ~or_(resuming, executing)
    values = []
    for condition in (active, pending):
        query = select(func.count(Task.id)).where(condition)
        if exclude_task_id is not None:
            query = query.where(Task.id != exclude_task_id)
        values.extend([await db.scalar(query), await db.scalar(query.join(
            Conversation, Conversation.id == Task.conversation_id).where(Conversation.user_id == user_id))])
    return tuple(values)


async def request_time(db, task_id, version):
    return await db.scalar(select(func.min(TaskEvent.created_at)).where(
        TaskEvent.task_id == task_id, TaskEvent.event_type == 'hitl_request',
        TaskEvent.payload['decisionVersion'].as_string() == version))


async def database_time(db):
    return await db.scalar(select(func.clock_timestamp()))
