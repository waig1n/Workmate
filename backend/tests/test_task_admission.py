import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.config import get_settings
from app.db import get_sessionmaker
from workmate_product.persistence.models import Conversation, Task
from app.services.task_admission import task_admission


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_field", ["task_max_outstanding", "task_max_outstanding_per_user"])
async def test_admission_competition_is_bounded_and_capacity_released(
    seeded_ids, db_session, monkeypatch, limit_field,
):
    _, cid = seeded_ids
    user_id = (await db_session.get(Conversation, cid)).user_id
    monkeypatch.setattr(get_settings(), limit_field, 2)

    async def submit():
        async with get_sessionmaker()() as db:
            try:
                async with task_admission(db, user_id):
                    db.add(Task(id=str(uuid.uuid4()), conversation_id=cid,
                                title="queued", status="created"))
                    await db.commit()
                return 201
            except HTTPException as exc:
                assert exc.headers["Retry-After"] == "5"
                return exc.status_code

    results = await asyncio.gather(*(submit() for _ in range(10)))
    assert results.count(201) == 2
    assert results.count(429) == 8
    assert await db_session.scalar(select(func.count(Task.id))) == 2
    task = await db_session.scalar(select(Task).limit(1))
    task.status = "cancelled"
    await db_session.commit()
    assert await submit() == 201


@pytest.mark.asyncio
@pytest.mark.parametrize('limit_field', ['task_max_pending_approval', 'task_max_pending_approval_per_user'])
async def test_pending_approval_has_separate_admission_budget(seeded_ids, db_session, monkeypatch, limit_field):
    _, cid = seeded_ids
    uid = (await db_session.get(Conversation, cid)).user_id
    settings = get_settings()
    monkeypatch.setattr(settings, 'task_max_outstanding', 1)
    monkeypatch.setattr(settings, limit_field, 2)
    db_session.add(Task(id='parked', conversation_id=cid, title='parked', status='awaiting_permission'))
    await db_session.commit()
    async with task_admission(db_session, uid):
        await db_session.commit()
    db_session.add(Task(id='parked2', conversation_id=cid, title='parked', status='awaiting_permission'))
    await db_session.commit()
    with pytest.raises(HTTPException, match='429'):
        async with task_admission(db_session, uid):
            pass
