"""Production Gateway scheduler lifespans compete over the same real PG row.

No Worker/model runs: the assertion is exactly one durable admission and Start,
not exactly-once external side effects. Both independent processes are observed
waiting in the same PostgreSQL admission lock chain before the due row is released.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from test_durable_projection import projection_database  # noqa: F401
from test_native_admission import admission_database  # noqa: F401
from test_schedules import schedule_database, create_schedule, outcomes  # noqa: F401
from test_dual_gateway_http import Gateway


async def test_real_gateway_scheduler_processes_admit_one_occurrence(schedule_database, tmp_path):
    if not os.getenv('WORKMATE_TEST_REDIS_URL'):
        pytest.skip('requires isolated Redis')
    _, admin, _ = schedule_database
    schema = await admin.fetchval('SELECT current_schema()')
    namespace = 'gateway-schedule-' + uuid4().hex
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        '  models:\n    default: test\n    profiles:\n      test:\n        type: openai\n'
        '        model: fixture-model\n        api_key_env: FIXTURE_MODEL_KEY\n'
        '  schedules:\n    enabled: true\n    poll_interval_secs: 0.1\n'
        'command_queue:\n  poll_interval_seconds: 0.05\n')
    schedule_id, _ = await create_schedule(schedule_database, rule={
        'kind': 'once', 'run_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    gateways = [Gateway(tmp_path, schema, namespace, config, index) for index in range(2)]
    barrier = None
    try:
        for gateway in gateways:
            await gateway.start()
        barrier = admin.transaction()
        await barrier.start()
        await admin.execute('LOCK TABLE schedules IN ACCESS EXCLUSIVE MODE')
        # Move the one-shot definition together with its derived next_fire_at.
        # Leaving run_at in the future would correctly schedule another tick.
        due = await admin.fetchval("""UPDATE schedules SET next_fire_at=now(),
            rule=jsonb_build_object('kind','once','run_at',now())
            WHERE id=$1 RETURNING next_fire_at""", schedule_id)
        application_names = [f'q4_gateway_{gateway.process.pid}' for gateway in gateways]
        async with asyncio.timeout(15):
            while True:
                await admin.execute('SELECT pg_stat_clear_snapshot()')
                waiting = await admin.fetch("""SELECT application_name, pid, wait_event_type, query
                    FROM pg_stat_activity WHERE application_name=ANY($1::text[])
                    AND wait_event_type='Lock'""", application_names)
                if {row['application_name'] for row in waiting} == set(application_names):
                    assert any('schedules' in row['query'] for row in waiting)
                    # The production capacity advisory lock precedes Schedule
                    # row locking, so the second poller waits on that lock.
                    assert any('pg_advisory_xact_lock' in row['query'] for row in waiting)
                    break
                await asyncio.sleep(.05)
        (tmp_path / 'schedule-barrier.json').write_text(json.dumps({
            'gateway_processes': [gateway.process.pid for gateway in gateways],
            'waiting_database_sessions': [dict(row) for row in waiting],
            'schedule_id': schedule_id, 'scheduled_for': due.isoformat(),
        }, indent=2) + '\n')
        await barrier.commit()
        barrier = None
        async with asyncio.timeout(15):
            while not await outcomes(admin, schedule_id):
                await asyncio.sleep(.05)
        # Multiple production polling cycles after the race must remain idempotent.
        await asyncio.sleep(.5)
        rows = await outcomes(admin, schedule_id)
        assert len(rows) == 1 and rows[0]['outcome'] == 'task_created', [dict(row) for row in rows]
        assert rows[0]['scheduled_for'] == due
        task_id = rows[0]['task_id']
        assert await admin.fetchval("SELECT count(*) FROM tasks t JOIN schedule_occurrences o ON o.task_id=t.id WHERE o.schedule_id=$1", schedule_id) == 1
        assert await admin.fetchval('SELECT count(*) FROM runtime_commands WHERE task_id=$1', task_id) == 1
        assert await admin.fetchval('SELECT count(*) FROM command_deliveries d JOIN runtime_commands c ON c.id=d.command_id WHERE c.task_id=$1', task_id) == 1
        assert await admin.fetchval('SELECT count(*) FROM task_messages WHERE task_id=$1', task_id) == 1
        async with asyncio.timeout(5):
            while await admin.fetchval('SELECT d.status FROM command_deliveries d JOIN runtime_commands c ON c.id=d.command_id WHERE c.task_id=$1', task_id) != 'published':
                await asyncio.sleep(.05)
        payload = json.loads(await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1', task_id))
        assert payload['approval_profile'] == 'unattended' and payload['actor_user_id'] == 'u'
        assert await admin.fetchval('SELECT status FROM schedules WHERE id=$1', schedule_id) == 'completed'
    finally:
        if barrier is not None:
            await barrier.rollback()
        await asyncio.gather(*(gateway.close() for gateway in gateways))
        redis = Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'])
        try:
            keys = [key async for key in redis.scan_iter(match=f'workmate:{namespace}:*')]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()
