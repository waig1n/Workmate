"""Durable approval expiry and admission races, not a load test."""
import asyncio
import json

import pytest

from workmate_product.application.commands import CommandRejected
from workmate_product.application.control_commands import accept_resume
from workmate_product.application.task_capacity import TaskCapacityPolicy, TaskCapacityExceeded
from workmate_product.application.approval_expiry import expire_approval
from test_durable_projection import projection_database  # noqa: F401
from test_control_commands import parked, allow, admit, stop


async def expire(sessions, policy=TaskCapacityPolicy()):
    async with sessions.begin() as db:
        return await expire_approval(db, 'c', policy=policy)


async def age(admin):
    await admin.execute("UPDATE task_events SET created_at=now()-interval '25 hours' WHERE event_type='hitl_request'")


async def test_expiry_is_durable_idempotent_and_keeps_gates(projection_database):
    sessions, admin = projection_database
    command = await parked(sessions, admin)
    assert not await expire(sessions)
    await age(admin)
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected, match='expired'):
            await accept_resume(db, command, actor_user_id='u', authorize_resume=allow)
    results = await asyncio.gather(expire(sessions), expire(sessions))
    assert sum(bool(r) for r in results) == 1
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
    assert await admin.fetchval('SELECT status FROM runtime_task_executions') == 'awaiting_interaction'
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_request'") == 1
    record = await admin.fetchrow('SELECT * FROM runtime_commands')
    assert json.loads(record['payload'])['reason'] == 'approval_expired'
    from workmate_runtime.transport.stop_authority import admitted_stop_actor
    assert await admitted_stop_actor(admin, record, {'id':'c', 'user_id':'u'})
    await admin.execute("DELETE FROM task_events WHERE event_type='task_cancel_requested'")
    assert not await admitted_stop_actor(admin, record, {'id':'c', 'user_id':'u'})


async def test_resume_reserves_capacity_and_retries_remain_idempotent(projection_database):
    sessions, admin = projection_database
    command = await parked(sessions, admin)
    await admin.execute("INSERT INTO tasks(id,conversation_id,title,status) VALUES ('other','c','queued','created')")
    policy = TaskCapacityPolicy(task_max_outstanding=1)
    async with sessions.begin() as db:
        with pytest.raises(TaskCapacityExceeded):
            await accept_resume(db, command, actor_user_id='u', authorize_resume=allow, policy=policy)
    await admin.execute("UPDATE tasks SET status='completed' WHERE id='other'")
    async with sessions.begin() as db:
        await accept_resume(db, command, actor_user_id='u', authorize_resume=allow, policy=policy)
    async with sessions.begin() as db:
        assert (await accept_resume(db, command, actor_user_id='u', authorize_resume=allow, policy=policy)).duplicate
        with pytest.raises(TaskCapacityExceeded):
            await policy.check(db, 'u')
    await age(admin)
    assert not await expire(sessions)  # An accepted decision is not abandoned.
    await admit(sessions, stop())  # Stop cannot be blocked by capacity.


async def test_expiry_competes_with_stop_and_rejects_forged_reason(projection_database):
    sessions, admin = projection_database
    await parked(sessions, admin)
    await age(admin)
    await asyncio.gather(expire(sessions), admit(sessions, stop()), return_exceptions=True)
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StopTask'") == 1
    assert await admin.fetchval('SELECT status FROM tasks') == 'cancelling'
    with pytest.raises(CommandRejected):
        await admit(sessions, stop().model_copy(update={'payload': {'reason':'approval_expired'}}))


async def test_expired_resume_races_with_sweeper(projection_database):
    sessions, admin = projection_database
    command = await parked(sessions, admin)
    await age(admin)
    results = await asyncio.gather(admit(sessions, command), expire(sessions), return_exceptions=True)
    assert isinstance(results[0], CommandRejected)
    assert results[1] is True
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='ResumeInteraction'") == 0


async def test_background_scan_and_restart_use_persisted_request_time(projection_database):
    from app.services.durable_runtime import compose_product_runtime
    sessions, admin = projection_database
    await parked(sessions, admin)
    await age(admin)
    service = compose_product_runtime(sessions, capacity_policy=TaskCapacityPolicy())
    assert await service.tick('approval_expiry') == 1
    restarted = compose_product_runtime(sessions, capacity_policy=TaskCapacityPolicy())
    assert await restarted.tick('approval_expiry') == 0
    assert not service.failures and not restarted.failures


@pytest.mark.parametrize('mutation', [
    "UPDATE tasks SET status='completed'",
    "UPDATE runtime_task_executions SET status='running'",
    "UPDATE runtime_task_executions SET snapshot_id=NULL",
    "UPDATE conversations SET active_product_task_id=NULL",
    "DELETE FROM task_projection_states",
])
async def test_stale_expiry_candidate_does_not_stop_execution(projection_database, mutation):
    sessions, admin = projection_database
    await parked(sessions, admin)
    await age(admin)
    await admin.execute(mutation)
    assert not await expire(sessions)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0


async def test_expiry_audit_failure_rolls_back_intent(projection_database):
    from sqlalchemy.exc import IntegrityError
    sessions, admin = projection_database
    await parked(sessions, admin)
    await age(admin)
    await admin.execute("ALTER TABLE task_events ADD CHECK (event_type<>'task_cancel_requested')")
    with pytest.raises(IntegrityError):
        await expire(sessions)
    assert await admin.fetchval('SELECT status FROM tasks') == 'awaiting_permission'
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0


async def test_missing_timestamp_fails_closed(projection_database):
    sessions, _ = projection_database
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected, match='timestamp'):
            await TaskCapacityPolicy().expired(db, 't', 'missing')


async def test_new_task_and_resume_compete_for_same_active_slot(projection_database):
    from sqlalchemy import text
    from workmate_product.persistence.repositories.task_capacity import lock_capacity
    sessions, admin = projection_database
    command = await parked(sessions, admin)
    policy = TaskCapacityPolicy(task_max_outstanding=1)

    async def create():
        async with sessions.begin() as db:
            await lock_capacity(db)
            await policy.check(db, 'u')
            await db.execute(text("INSERT INTO tasks(id,conversation_id,title,status) VALUES ('new','c','queued','created')"))
        return True

    async def resume():
        async with sessions.begin() as db:
            await accept_resume(db, command, actor_user_id='u', authorize_resume=allow, policy=policy)
        return True

    results = await asyncio.gather(create(), resume(), return_exceptions=True)
    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, TaskCapacityExceeded) for result in results) == 1


async def test_http_runtime_maps_capacity_to_retryable_429(monkeypatch):
    from fastapi import HTTPException
    from app.services.product_runtime import ProductRuntime
    from workmate_product.application.task_controls import TaskControls

    async def full(*args, **kwargs):
        raise TaskCapacityExceeded('capacity full')

    monkeypatch.setattr(TaskControls, 'confirm', full)
    with pytest.raises(HTTPException) as error:
        await ProductRuntime(None, None).confirm()
    assert error.value.status_code == 429
    assert error.value.headers == {'Retry-After': '5'}
