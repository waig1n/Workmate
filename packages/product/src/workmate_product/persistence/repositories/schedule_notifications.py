"""Notification lookups and locks; transaction ownership remains in applications."""
from sqlalchemy import select, exists, or_, and_
from ..models import (ChannelIdentity, ChannelConnection, ChannelInbox, ChannelOutbox,
                      Schedule, ScheduleOccurrence, Task, Conversation)


async def binding_channel(db, binding_id, user_id):
    return (await db.execute(select(ChannelIdentity, ChannelConnection)
        .join(ChannelConnection, ChannelConnection.id == ChannelIdentity.channel_id)
        .where(ChannelIdentity.id == binding_id, ChannelIdentity.user_id == user_id)
        .execution_options(populate_existing=True))).first()


async def targets(db, user_id):
    return (await db.execute(select(ChannelIdentity, ChannelConnection)
        .join(ChannelConnection, ChannelConnection.id == ChannelIdentity.channel_id)
        .where(ChannelIdentity.user_id == user_id, ChannelConnection.deleted_at.is_(None))
        .order_by(ChannelConnection.name, ChannelIdentity.created_at))).all()


async def latest_context(db, binding):
    return await db.scalar(select(ChannelInbox).where(ChannelInbox.channel_id == binding.channel_id,
        ChannelInbox.sender_id == binding.external_user_id, ChannelInbox.user_id == binding.user_id,
        ChannelInbox.chat_type == 'private', ChannelInbox.created_at >= binding.created_at)
        .order_by(ChannelInbox.created_at.desc(), ChannelInbox.id.desc()).limit(1))


async def delivery(db, occurrence_id, *, lock=False):
    stmt = select(ChannelOutbox).where(ChannelOutbox.schedule_occurrence_id == occurrence_id)
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)
    return await db.scalar(stmt.execution_options(populate_existing=True))


async def occurrence(db, occurrence_id, *, lock=False):
    stmt = select(ScheduleOccurrence).where(ScheduleOccurrence.id == occurrence_id)
    if lock:
        stmt = stmt.with_for_update(skip_locked=True)
    return await db.scalar(stmt.execution_options(populate_existing=True))


async def candidates(db, limit):
    notification = ScheduleOccurrence.snapshot['notification']
    terminal = Task.status.in_(['completed','failed','cancelled','interrupted'])
    released = or_(Conversation.active_product_task_id.is_(None), Conversation.active_product_task_id != Task.id)
    wanted = or_(and_(Task.status == 'completed', notification['on_success'].as_boolean().is_(True)),
                 and_(or_(ScheduleOccurrence.task_id.is_(None), Task.status != 'completed'), notification['on_failure'].as_boolean().is_(True)))
    stmt = (select(ScheduleOccurrence.id).outerjoin(Task, Task.id == ScheduleOccurrence.task_id)
        .outerjoin(Conversation, Conversation.id == Task.conversation_id)
        .where(notification['channel_id'].as_string().is_not(None), wanted,
               or_(ScheduleOccurrence.task_id.is_(None), and_(terminal,released)),
               ~exists(select(ChannelOutbox.id).where(ChannelOutbox.schedule_occurrence_id == ScheduleOccurrence.id)))
        .order_by(ScheduleOccurrence.triggered_at, ScheduleOccurrence.id).limit(limit))
    return list((await db.scalars(stmt)).all())


async def pending(db, limit, *, after=None):
    stmt = select(ChannelOutbox).where(ChannelOutbox.kind == 'schedule_result', ChannelOutbox.status == 'pending')
    if after:
        stamp, row_id = after
        stmt = stmt.where(or_(ChannelOutbox.created_at > stamp,
            and_(ChannelOutbox.created_at == stamp, ChannelOutbox.id > row_id)))
    return list((await db.scalars(stmt.order_by(ChannelOutbox.created_at, ChannelOutbox.id)
        .limit(limit).with_for_update(skip_locked=True))).all())
