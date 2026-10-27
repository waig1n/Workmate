"""Trusted Schedule provenance survives edits and is rechecked by Worker reads."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import update
from test_execution_context import database, record, claim, read
from workmate_product.application.execution_context import ExecutionContextRejected
from workmate_product.persistence.models import Schedule, ScheduleOccurrence, Task, TaskMessage


async def schedule(sessions, *, snapshot_owner='u'):
    async with sessions.begin() as db:
        now = datetime.now(timezone.utc)
        db.add(Schedule(id='schedule',owner_user_id='u',agent_id='a',name='Daily',prompt='hello',
            model_profile='chat',timezone='Asia/Shanghai',rule={'kind':'daily','time':'09:00'}))
        await db.flush()
        db.add(ScheduleOccurrence(id='occ',schedule_id='schedule',schedule_version=1,
            scheduled_for=now,triggered_at=now,outcome='task_created',task_id='t',conversation_id='c',
            snapshot={'owner_user_id':snapshot_owner,'agent_id':'a','version':1,'prompt':'hello',
                'model_profile':'chat','approval_profile':'unattended'}))
        await db.flush()
        await db.execute(update(Task).where(Task.id=='t').values(origin='schedule',
            schedule_occurrence_id='occ',approval_profile='unattended'))


@pytest.mark.asyncio
async def test_schedule_record_and_worker_read_ignore_later_plan_edits(database):
    sessions,admin = database
    await schedule(sessions)
    spec = await record(sessions)
    assert spec.origin == 'schedule' and spec.schedule_occurrence_id == 'occ'
    async with sessions.begin() as db:
        await db.execute(update(Schedule).values(version=2,status='paused',prompt='changed',
            deleted_at=datetime.now(timezone.utc)))
    await claim(admin)
    context = await read(sessions)
    assert context.initial_spec == spec
    assert context.input_messages[0].content['text'] == 'hello'
    assert context.effective_approval_profile == 'unattended'


@pytest.mark.asyncio
async def test_schedule_worker_rejects_input_tampering_after_record(database):
    sessions,admin = database
    await schedule(sessions)
    await record(sessions)
    async with sessions.begin() as db:
        await db.execute(update(TaskMessage).where(TaskMessage.id=='message').values(content={'text':'changed'}))
    await claim(admin)
    with pytest.raises(ExecutionContextRejected, match='Schedule input'):
        await read(sessions)


@pytest.mark.asyncio
async def test_schedule_record_rejects_snapshot_owner_mismatch(database):
    sessions,admin = database
    await schedule(sessions, snapshot_owner='other')
    with pytest.raises(ExecutionContextRejected, match='Schedule occurrence'):
        await record(sessions)
