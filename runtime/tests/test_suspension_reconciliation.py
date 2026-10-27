"""Restart only discovers proven released pauses; expiry never proves cleanup."""
import pytest
from test_postgres_execution_store import database
from test_resume_controller import parked
from test_suspension_store import begin, receipt
from workmate_runtime.transport.suspension import SuspensionStore
from workmate_runtime.execution.reconciliation import inspect_sessions


@pytest.mark.parametrize('phase',['released','releasing','blocked'])
async def test_restart_classifies_pause_evidence(database,phase):
    pool,db=database
    first=await parked(pool,db)
    claim=await begin(db,first)
    if phase=='released':
        await SuspensionStore(db).release(claim,receipt(claim))
    elif phase=='blocked':
        await SuspensionStore(db).block(claim)
    await db.execute("UPDATE runtime_sessions SET lease_until=CASE WHEN owner_id IS NULL THEN NULL ELSE clock_timestamp()-interval '1 second' END")
    rows=await inspect_sessions(db,worker_id='replacement',storage_node_id='node')
    result=next(r for r in rows if r.session_id=='session')
    assert result.category==('released_permission' if phase=='released' else 'blocked_suspension')


async def test_released_pause_on_other_node_is_not_runnable(database):
    pool,db=database
    first=await parked(pool,db)
    claim=await begin(db,first)
    await SuspensionStore(db).release(claim,receipt(claim))
    rows=await inspect_sessions(db,worker_id='replacement',storage_node_id='other')
    assert next(r for r in rows if r.session_id=='session').category=='foreign_storage'


async def test_peer_startup_observes_live_cold_preparation_without_quarantine(database):
    from test_resume_controller import submit, allow
    from workmate_runtime.transport.controls import ResumeStore
    pool,db=database
    first=await parked(pool,db)
    claim=await begin(db,first)
    await SuspensionStore(db).release(claim,receipt(claim))
    await submit(db,first.reply_id)
    assert await ResumeStore(db).claim_resume('resume','replacement','node',authorize_resume=allow)
    rows=await inspect_sessions(db,worker_id='peer',storage_node_id='node')
    assert next(r for r in rows if r.session_id=='session').category=='foreign_live'
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    rows=await inspect_sessions(db,worker_id='peer',storage_node_id='node')
    assert next(r for r in rows if r.session_id=='session').category.startswith('blocked_')


async def test_peer_startup_during_live_cleanup_does_not_quarantine(database):
    pool,db=database
    first=await parked(pool,db)
    await begin(db,first)
    rows=await inspect_sessions(db,worker_id='peer',storage_node_id='node')
    assert next(r for r in rows if r.session_id=='session').category=='foreign_live'
