"""Startup observations never grant ownership or replay a claimed command."""
import pytest

from test_postgres_execution_store import database, claim  # noqa: F401
from test_resume_controller import parked
from workmate_runtime.execution.reconciliation import inspect_sessions


async def observe(db, worker='worker', node='node'):
    rows = await inspect_sessions(db, worker_id=worker, storage_node_id=node)
    assert len(rows) == 1
    return rows[0]


async def dump(db):
    return [await db.fetchval(f'SELECT coalesce(jsonb_agg(to_jsonb(t)),\'[]\'::jsonb) FROM {table} t')
            for table in ('conversations','tasks','runtime_sessions','runtime_task_executions',
                          'runtime_commands','runtime_events','runtime_snapshots')]


async def test_unclaimed_admitted_start_is_only_candidate_and_read_only(database):
    _, db = database
    before = await dump(db)
    item = await observe(db)
    assert item.category == 'ready_unclaimed'
    assert item.command_ids == ('command',)
    assert item.task_id == 'task'
    assert await dump(db) == before


async def test_claimed_start_needs_local_registry_even_for_same_owner(database):
    pool, db = database
    await claim(pool)
    before = await dump(db)
    item = await observe(db)
    assert item.category == 'local_execution_unverified'
    assert await dump(db) == before
    assert (await observe(db, worker='another-process')).category == 'foreign_live'


async def test_complete_parked_owner_is_observation_not_attach_authority(database):
    pool, db = database
    result = await parked(pool, db)
    before = await dump(db)
    item = await observe(db, worker=result.owner.worker_id)
    assert item.category == 'local_parked_unverified'
    assert item.snapshot_id is not None
    assert await dump(db) == before


@pytest.mark.parametrize('park_first', [False, True])
async def test_expired_owner_never_reclaims_or_retries(database, park_first):
    pool, db = database
    if park_first:
        await parked(pool, db)
    else:
        await claim(pool)
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    before = await dump(db)
    assert (await observe(db)).category == 'blocked_expired'
    assert await dump(db) == before


async def test_foreign_live_and_foreign_storage_do_not_hide_corrupt_binding(database):
    pool, db = database
    await claim(pool)
    await db.execute("UPDATE conversations SET active_product_task_id='other-task'")
    assert (await observe(db, worker='other', node='other-node')).category == 'blocked_inconsistent'


async def test_accepted_start_with_wrong_epoch_is_not_ready(database):
    _, db = database
    await db.execute('UPDATE runtime_commands SET expected_epoch=9')
    assert (await observe(db)).category == 'blocked_inconsistent'


async def test_parked_unresolved_control_requires_reconciliation(database):
    pool, db = database
    await parked(pool, db)
    await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
        kind,expected_epoch,status,payload,content_digest)
        VALUES ('resume','session','conversation','task','user','ResumeInteraction',1,'indeterminate','{}',repeat('a',64))""")
    assert (await observe(db)).category == 'blocked_claimed'


async def test_terminal_snapshot_keeps_delivery_gate_visible(database):
    pool, db = database
    first = await parked(pool, db)
    from test_parked_stop import stop_intent, quiescent
    from test_resume_controller import prepare_resume
    from test_core_engine import ConfirmTool
    from workmate_runtime.execution.stopping import ParkedStopController
    await stop_intent(db)
    await ParkedStopController(pool).stop('stop', first.owner.worker_id, 'node',
                                         prepare_resume(ConfirmTool()), verify_cleanup=quiescent)
    before = await dump(db)
    assert (await observe(db)).category == 'terminal_delivery_pending'
    assert await dump(db) == before


async def test_storage_affinity_does_not_authorize_claim(database):
    _, db = database
    assert (await observe(db, node='another-node')).category == 'foreign_storage'


async def test_reports_multiple_sessions_without_blocking_healthy_candidates(database):
    _, db = database
    await db.execute("""INSERT INTO conversations(id,user_id,agent_id) VALUES ('empty','user','agent');
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('empty-session','empty','node')""")
    rows = await inspect_sessions(db, worker_id='worker', storage_node_id='node')
    assert {item.category for item in rows} == {'idle', 'ready_unclaimed'}
    assert [item.session_id for item in rows] == sorted(item.session_id for item in rows)


async def test_orphan_active_execution_is_not_reported_idle(database):
    _, db = database
    await db.execute("""UPDATE conversations SET active_product_task_id=NULL;
        UPDATE runtime_commands SET status='rejected';
        UPDATE runtime_task_executions SET status='preparing',execution_epoch=1""")
    assert (await observe(db)).category == 'blocked_inconsistent'
