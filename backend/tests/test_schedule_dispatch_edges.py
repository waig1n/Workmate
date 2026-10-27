"""Boundary regressions for scheduling without a model or running Worker."""
from datetime import timedelta

import pytest
from fastapi import HTTPException

from test_schedules import schedule_database, create_schedule, outcomes, definition
from test_native_admission import admission_database
from test_durable_projection import projection_database
from workmate_product.schedules.rules import ScheduleUpdate
from workmate_product.config.schedules import SchedulePolicy


async def test_edit_completed_definition_requires_explicit_resume(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await admin.execute("UPDATE schedules SET status='completed',next_fire_at=NULL WHERE id=$1", schedule_id)
    async with sessions() as db:
        row = await service.update(db, 'u', schedule_id,
            ScheduleUpdate(**definition().model_dump(), version=1))
        assert row.status == 'paused' and row.next_fire_at is None
        resumed = await service.set_active(db, 'u', row.id, row.version, True)
        assert resumed.status == 'active' and resumed.next_fire_at is not None


async def test_unavailable_node_retries_then_records_missed(schedule_database, monkeypatch):
    from workmate_product.application.node_placement import NodePlacementUnavailable
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    service.admission.node_directory = object()
    async def unavailable(*args, **kwargs):
        raise NodePlacementUnavailable('offline')
    monkeypatch.setattr(service.admission, '_storage_node', unavailable)
    await service.tick(now=due)
    assert await outcomes(admin, schedule_id) == []
    assert await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id) == due
    await service.tick(now=due + timedelta(minutes=6))
    assert (await outcomes(admin, schedule_id))[0]['outcome'] == 'missed'


async def test_removed_model_blocks_and_pauses(schedule_database, monkeypatch):
    from app.services.runtime_errors import ModelConfigError
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    def removed(*args):
        raise ModelConfigError('private deployment detail')
    monkeypatch.setattr('app.services.schedules.selected_model', removed)
    await service.tick(now=due)
    row = (await outcomes(admin, schedule_id))[0]
    assert row['outcome'] == 'blocked' and 'private' not in row['reason']
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'paused'


async def test_disabled_service_allows_read_pause_delete_only(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    service.policy = SchedulePolicy(enabled=False)
    assert await service.tick(now=due) == 0
    async with sessions() as db:
        with pytest.raises(HTTPException) as denied:
            await service.create(db, 'u', definition())
        assert denied.value.status_code == 503
        await db.rollback()
        paused = await service.set_active(db, 'u', schedule_id, 1, False)
        with pytest.raises(HTTPException) as denied:
            await service.set_active(db, 'u', schedule_id, paused.version, True)
        assert denied.value.status_code == 503
        await db.rollback()
        await service.delete(db, 'u', schedule_id, 2)
    assert await outcomes(admin, schedule_id) == []


async def test_waiting_node_does_not_starve_other_due_schedule(schedule_database, monkeypatch):
    from workmate_product.application.node_placement import NodePlacementUnavailable
    _, admin, service = schedule_database
    first, due = await create_schedule(schedule_database)
    second, _ = await create_schedule(schedule_database)
    await admin.execute('UPDATE schedules SET next_fire_at=$1 WHERE id=$2', due + timedelta(seconds=1), second)
    service.admission.node_directory = object()
    calls = 0
    async def select_node(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NodePlacementUnavailable('offline')
        return 'node'
    monkeypatch.setattr(service.admission, '_storage_node', select_node)
    await service.tick(now=due + timedelta(seconds=2))
    assert await outcomes(admin, first) == []
    assert (await outcomes(admin, second))[0]['outcome'] == 'task_created'


async def test_worker_model_failure_pauses_definition_until_explicit_resume(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await service.tick(now=due)
    first = (await outcomes(admin, schedule_id))[0]
    await admin.execute("UPDATE tasks SET status='failed',error=$1::json WHERE id=$2",
        '{"code":"model_unavailable","message":"Selected model is unavailable"}', first['task_id'])
    next_due = await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id)
    await service.tick(now=due + timedelta(seconds=10))
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 1 and rows[0]['outcome'] == 'task_created'
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'paused'
    # Resume changes the definition version; a repaired deployment may retry.
    await admin.execute('UPDATE conversations SET active_product_task_id=NULL WHERE id=$1', first['conversation_id'])
    await admin.execute('UPDATE runtime_sessions SET active_task_id=NULL WHERE conversation_id=$1', first['conversation_id'])
    async with sessions() as db:
        await service.set_active(db, 'u', schedule_id, 1, True)
    # The fixture moves virtual time forward independently of DB wall time.
    await admin.execute('UPDATE schedules SET next_fire_at=$1 WHERE id=$2', next_due + timedelta(days=1), schedule_id)
    await service.tick(now=next_due + timedelta(days=1))
    assert (await outcomes(admin, schedule_id))[-1]['outcome'] == 'task_created'


async def test_once_worker_failure_is_reconciled_without_another_due_time(schedule_database):
    from datetime import datetime, timezone
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database, rule={
        'kind': 'once', 'run_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    await service.tick(now=due)
    first = (await outcomes(admin, schedule_id))[0]
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'completed'
    await admin.execute("UPDATE tasks SET status='failed',error=$1::json WHERE id=$2",
        '{"code":"model_unavailable"}', first['task_id'])
    await service.tick(now=due + timedelta(seconds=10))
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'paused'
    assert len(await outcomes(admin, schedule_id)) == 1
