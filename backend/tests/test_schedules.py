"""Schedule admission uses real PostgreSQL transactions and independent callers."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

from fastapi import HTTPException
import pytest
import pytest_asyncio

from app.services.execution_admission import TaskAdmission
from app.services.schedules import ScheduleService
from workmate_product.schedules.rules import ScheduleInput, ScheduleUpdate
from test_native_admission import admission_database
from test_durable_projection import projection_database


def definition(**changes):
    return ScheduleInput.model_validate({
        'name': 'Daily report', 'agent_id': 'a', 'prompt': 'Prepare a report',
        'model_profile': 'test', 'timezone': 'Asia/Shanghai',
        'rule': {'kind': 'daily', 'time': '09:00'}, **changes,
    })


@pytest_asyncio.fixture
async def schedule_database(admission_database):
    sessions, admin = admission_database
    service = ScheduleService(sessions, TaskAdmission(sessions, storage_node_id='node'))
    return sessions, admin, service


async def create_schedule(database, **changes):
    sessions, admin, service = database
    async with sessions() as db:
        schedule = await service.create(db, 'u', definition(**changes))
        schedule_id = schedule.id
    due = await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id)
    return schedule_id, due


async def outcomes(admin, schedule_id):
    return await admin.fetch('SELECT * FROM schedule_occurrences WHERE schedule_id=$1 ORDER BY scheduled_for', schedule_id)


async def test_two_gateways_create_one_occurrence_task_and_start(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    other = ScheduleService(sessions, TaskAdmission(sessions, storage_node_id='node'))
    await asyncio.gather(service.tick(now=due), other.tick(now=due))
    await service.tick(now=due)
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 1 and rows[0]['outcome'] == 'task_created'
    task_id, conversation_id = rows[0]['task_id'], rows[0]['conversation_id']
    assert await admin.fetchval('SELECT origin FROM tasks WHERE id=$1', task_id) == 'schedule'
    assert await admin.fetchval('SELECT approval_profile FROM tasks WHERE id=$1', task_id) == 'unattended'
    assert await admin.fetchval('SELECT user_id FROM conversations WHERE id=$1', conversation_id) == 'u'
    assert conversation_id not in {'new', 'c'}
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands WHERE task_id=$1', task_id) == 1
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries d JOIN runtime_commands c ON c.id=d.command_id WHERE c.task_id=$1', task_id) == 1
    assert await admin.fetchval('SELECT count(*) FROM task_messages WHERE task_id=$1', task_id) == 1
    payload = json.loads(await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1', task_id))
    assert payload['approval_profile'] == 'unattended' and payload['actor_user_id'] == 'u'


async def test_spec_failure_rolls_back_occurrence_conversation_task_and_command(schedule_database, monkeypatch):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    before = {table: await admin.fetchval(f'SELECT count(*) FROM {table}')
              for table in ('conversations', 'tasks', 'task_messages', 'runtime_commands', 'runtime_execution_specs', 'command_deliveries')}
    async def failed_spec(*args, **kwargs):
        raise RuntimeError('injected spec write failure')
    with monkeypatch.context() as patch:
        patch.setattr('app.services.execution_admission.record_execution_spec', failed_spec)
        try:
            await service.tick(now=due)
        except RuntimeError as error:
            assert 'injected' in str(error)
    assert await outcomes(admin, schedule_id) == []
    assert await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id) == due
    for table, count in before.items():
        assert await admin.fetchval(f'SELECT count(*) FROM {table}') == count
    await service.tick(now=due + timedelta(seconds=1))
    assert len(await outcomes(admin, schedule_id)) == 1


async def test_terminal_task_with_open_delivery_gate_still_skips_next_period(schedule_database):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await service.tick(now=due)
    first = (await outcomes(admin, schedule_id))[0]
    await admin.execute("UPDATE tasks SET status='completed' WHERE id=$1", first['task_id'])
    next_due = await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id)
    await service.tick(now=next_due)
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 2 and rows[1]['outcome'] == 'skipped_busy'
    assert rows[1]['task_id'] is None


async def test_later_period_creates_independent_conversation_after_gates_close(schedule_database):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await service.tick(now=due)
    first = (await outcomes(admin, schedule_id))[0]
    await admin.execute("UPDATE tasks SET status='completed' WHERE id=$1", first['task_id'])
    await admin.execute('UPDATE conversations SET active_product_task_id=NULL WHERE id=$1', first['conversation_id'])
    next_due = await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id)
    await service.tick(now=next_due)
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 2 and rows[1]['outcome'] == 'task_created'
    assert rows[0]['conversation_id'] != rows[1]['conversation_id']


async def test_runtime_execution_gate_blocks_even_when_product_gate_cleared(schedule_database):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await service.tick(now=due)
    first = (await outcomes(admin, schedule_id))[0]
    await admin.execute("UPDATE tasks SET status='completed' WHERE id=$1", first['task_id'])
    await admin.execute('UPDATE conversations SET active_product_task_id=NULL WHERE id=$1', first['conversation_id'])
    await admin.execute('UPDATE runtime_sessions SET active_task_id=$1 WHERE conversation_id=$2', first['task_id'], first['conversation_id'])
    next_due = await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id)
    await service.tick(now=next_due)
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 2 and rows[1]['outcome'] == 'skipped_busy'


async def test_revoked_owner_blocks_and_pauses_without_creating_task(schedule_database):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'")
    await service.tick(now=due)
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 1 and rows[0]['outcome'] == 'blocked' and rows[0]['task_id'] is None
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'paused'
    await service.tick(now=due + timedelta(days=1))
    assert len(await outcomes(admin, schedule_id)) == 1


async def test_missed_once_finishes_without_task(schedule_database):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database, rule={
        'kind': 'once', 'run_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    await service.tick(now=due + timedelta(minutes=6))
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 1 and rows[0]['outcome'] == 'missed' and rows[0]['task_id'] is None
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'completed'


async def test_capacity_retries_same_due_within_grace(schedule_database, monkeypatch):
    from app.config import get_settings
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    monkeypatch.setattr(get_settings(), 'task_max_outstanding', 1)
    await service.tick(now=due)
    assert await outcomes(admin, schedule_id) == []
    assert await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id) == due
    monkeypatch.setattr(get_settings(), 'task_max_outstanding', 64)
    await service.tick(now=due + timedelta(seconds=20))
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 1 and rows[0]['scheduled_for'] == due and rows[0]['outcome'] == 'task_created'


async def test_paused_plan_and_stale_update_do_not_create_old_definition(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    version = await admin.fetchval('SELECT version FROM schedules WHERE id=$1', schedule_id)
    async with sessions() as db:
        await service.set_active(db, 'u', schedule_id, version, False)
    await service.tick(now=due)
    assert await outcomes(admin, schedule_id) == []
    async with sessions() as db:
        with pytest.raises(HTTPException) as conflict:
            await service.update(db, 'u', schedule_id, ScheduleUpdate.model_validate({
                **definition().model_dump(mode='json'), 'version': version, 'prompt': 'Changed'}))
        assert conflict.value.status_code == 409


async def test_pause_and_trigger_race_has_consistent_committed_result(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    version = await admin.fetchval('SELECT version FROM schedules WHERE id=$1', schedule_id)
    async def pause():
        async with sessions() as db:
            await service.set_active(db, 'u', schedule_id, version, False)
    await asyncio.wait_for(asyncio.gather(pause(), service.tick(now=due)), timeout=10)
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'paused'
    rows = await outcomes(admin, schedule_id)
    assert len(rows) <= 1
    if rows:
        assert rows[0]['outcome'] == 'task_created'
        assert await admin.fetchval('SELECT count(*) FROM runtime_commands WHERE task_id=$1', rows[0]['task_id']) == 1


async def test_delete_retains_task_and_occurrence_history(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    await service.tick(now=due)
    first = (await outcomes(admin, schedule_id))[0]
    version = await admin.fetchval('SELECT version FROM schedules WHERE id=$1', schedule_id)
    async with sessions() as db:
        await service.delete(db, 'u', schedule_id, version)
    assert await admin.fetchval('SELECT deleted_at FROM schedules WHERE id=$1', schedule_id) is not None
    assert await admin.fetchval('SELECT count(*) FROM tasks WHERE id=$1', first['task_id']) == 1
    assert len(await outcomes(admin, schedule_id)) == 1
    await service.tick(now=due + timedelta(days=2))
    assert len(await outcomes(admin, schedule_id)) == 1


async def test_long_outage_records_one_missed_summary_and_advances_future(schedule_database):
    _, admin, service = schedule_database
    schedule_id, due = await create_schedule(schedule_database)
    now = due + timedelta(days=365, minutes=6)
    await service.tick(now=now)
    rows = await outcomes(admin, schedule_id)
    assert len(rows) == 1 and rows[0]['outcome'] == 'missed' and rows[0]['task_id'] is None
    assert await admin.fetchval('SELECT next_fire_at FROM schedules WHERE id=$1', schedule_id) > now
    await service.tick(now=now)
    assert len(await outcomes(admin, schedule_id)) == 1


async def test_other_user_cannot_read_mutate_or_list_history(schedule_database):
    sessions, admin, service = schedule_database
    schedule_id, _ = await create_schedule(schedule_database)
    version = await admin.fetchval('SELECT version FROM schedules WHERE id=$1', schedule_id)
    operations = [lambda db: service.get(db, 'other', schedule_id),
                  lambda db: service.occurrences(db, 'other', schedule_id),
                  lambda db: service.set_active(db, 'other', schedule_id, version, False),
                  lambda db: service.delete(db, 'other', schedule_id, version)]
    for operation in operations:
        async with sessions() as db:
            with pytest.raises(HTTPException) as denied:
                await operation(db)
            assert denied.value.status_code == 404
    assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'active'


@pytest.mark.parametrize('field,value', [('owner_user_id', 'other'), ('approval_profile', 'full_access'), ('origin', 'schedule')])
def test_schedule_input_rejects_identity_or_execution_policy(field, value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ScheduleInput.model_validate({**definition().model_dump(mode='json'), field: value})


def test_interactive_task_input_rejects_unattended_profile():
    from app.api.schemas import TaskCreate
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TaskCreate.model_validate({'message': 'hello', 'approval_profile': 'unattended'})

async def test_notification_freezes_at_fire_without_retargeting(schedule_database,monkeypatch):
    from unittest.mock import AsyncMock
    snapshot=AsyncMock(return_value={'binding_id':'binding','on_success':True,'on_failure':True,
        'channel_id':'ch','provider':'wecom','external_user_id':'external','user_id':'u'})
    monkeypatch.setattr('app.channels.scheduled_notifications.snapshot_target',snapshot)
    id,due=await create_schedule(schedule_database,notification={'binding_id':'binding'})
    sessions,admin,service=schedule_database
    snapshot.assert_awaited_once()
    assert snapshot.call_args.kwargs=={'strict':True}
    await service.tick(due)
    assert snapshot.call_args.kwargs=={'strict':False}
    value=json.loads(await admin.fetchval('SELECT snapshot FROM schedule_occurrences WHERE schedule_id=$1',id))
    assert value['notification']['external_user_id']=='external'
    async with sessions() as db:
        row=await service.get(db,'u',id)
        await service.update(db,'u',id,ScheduleUpdate(**definition(notification=None).model_dump(),version=row.version))
    original=json.loads(await admin.fetchval('SELECT snapshot FROM schedule_occurrences WHERE schedule_id=$1',id))
    assert original['notification']==value['notification']

async def test_notification_unavailable_at_fire_does_not_block_task(schedule_database,monkeypatch):
    from unittest.mock import AsyncMock
    snapshot=AsyncMock(return_value={'binding_id':'binding','on_success':True,'on_failure':True})
    monkeypatch.setattr('app.channels.scheduled_notifications.snapshot_target',snapshot)
    id,due=await create_schedule(schedule_database,notification={'binding_id':'binding'})
    snapshot.return_value={'binding_id':'binding','on_success':True,'on_failure':True,
        'user_id':'u','error':'Notification binding expired'}
    _,admin,service=schedule_database
    assert await service.tick(due)==1
    assert await admin.fetchval('SELECT task_id FROM schedule_occurrences WHERE schedule_id=$1',id)
