"""Schedule SQL only. Transactions belong to the application service."""
from sqlalchemy import select, func, or_, exists
from ..models import Schedule, ScheduleOccurrence, Task, Conversation, RuntimeSession


async def owned(db, user_id, schedule_id, *, lock=False):
    query = select(Schedule).where(Schedule.id == schedule_id,
        Schedule.owner_user_id == user_id, Schedule.deleted_at.is_(None))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(query)


async def due(db, now, excluded):
    return await db.scalar(select(Schedule).where(Schedule.status == 'active',
        Schedule.deleted_at.is_(None), Schedule.next_fire_at <= now,
        Schedule.id.not_in(excluded)).order_by(Schedule.next_fire_at, Schedule.id)
        .limit(1).with_for_update(skip_locked=True).execution_options(populate_existing=True))


async def failed_model_schedule(db, excluded):
    return await db.scalar(select(Schedule).join(ScheduleOccurrence,
        (ScheduleOccurrence.schedule_id == Schedule.id) &
        (ScheduleOccurrence.schedule_version == Schedule.version)).join(Task,
        Task.id == ScheduleOccurrence.task_id).where(
        Schedule.status.in_(['active','completed']), Schedule.deleted_at.is_(None),
        Schedule.id.not_in(excluded), Task.error['code'].as_string() == 'model_unavailable')
        .order_by(Schedule.updated_at, Schedule.id).limit(1)
        .with_for_update(of=Schedule, skip_locked=True).execution_options(populate_existing=True))


async def busy(db, schedule_id):
    return bool(await db.scalar(select(Task.id).join(ScheduleOccurrence,
        ScheduleOccurrence.task_id == Task.id).join(Conversation,
        Conversation.id == Task.conversation_id).where(
        ScheduleOccurrence.schedule_id == schedule_id,
        or_(Task.status.in_(['created','running','awaiting_permission','cancelling']),
            Conversation.active_product_task_id == Task.id,
            exists(select(RuntimeSession.id).where(RuntimeSession.active_task_id == Task.id)))).limit(1)))


async def model_failed(db, schedule_id, version):
    """Only trusted Worker failure of this definition can block future firings."""
    return bool(await db.scalar(select(Task.id).join(ScheduleOccurrence,
        ScheduleOccurrence.task_id == Task.id).where(
        ScheduleOccurrence.schedule_id == schedule_id,
        ScheduleOccurrence.schedule_version == version,
        Task.error['code'].as_string() == 'model_unavailable').limit(1)))


async def page(db, user_id, limit, offset):
    where = (Schedule.owner_user_id == user_id, Schedule.deleted_at.is_(None))
    total = await db.scalar(select(func.count()).select_from(Schedule).where(*where))
    rows = (await db.scalars(select(Schedule).where(*where)
        .order_by(Schedule.created_at.desc(), Schedule.id).limit(limit).offset(offset))).all()
    return rows, total


async def history(db, schedule_id, limit, offset=0):
    where = ScheduleOccurrence.schedule_id == schedule_id
    total = await db.scalar(select(func.count()).select_from(ScheduleOccurrence).where(where))
    rows = (await db.scalars(select(ScheduleOccurrence).where(where)
        .order_by(ScheduleOccurrence.scheduled_for.desc(), ScheduleOccurrence.id)
        .limit(limit).offset(offset))).all()
    return rows, total


async def occurrence(db, schedule_id, occurrence_id):
    return await db.scalar(select(ScheduleOccurrence).where(
        ScheduleOccurrence.id == occurrence_id, ScheduleOccurrence.schedule_id == schedule_id))
