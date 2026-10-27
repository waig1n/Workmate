"""Durable suspension releases ownership only with exact cleanup evidence."""
import json

import pytest
import pytest_asyncio

from test_postgres_execution_store import database
from test_resume_controller import parked, submit
from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership
from workmate_runtime.transport.suspension import CleanupReceipt, SuspensionStore


@pytest_asyncio.fixture
async def suspended_database(database):
    pool, db = database
    result = await parked(pool, db)
    yield pool, db, result


async def begin(db, result):
    snapshot_id = await db.fetchval('SELECT snapshot_id FROM runtime_sessions')
    return await SuspensionStore(db).begin(result.owner, 'node', snapshot_id, 'generation-1')


def receipt(claim, **changes):
    values = dict(schema_version=1, session_id=claim.owner.session_id, task_id=claim.owner.task_id,
        worker_id=claim.owner.worker_id, execution_epoch=claim.owner.execution_epoch,
        snapshot_id=claim.snapshot_id, snapshot_digest=claim.snapshot_digest, node_id=claim.node_id, resource_generation=claim.resource_generation,
        resources_quiescent=True, workspace_closed=True)
    return CleanupReceipt.model_validate({**values, **changes})


async def test_release_preserves_task_reply_and_snapshot(suspended_database):
    _, db, result = suspended_database
    claim = await begin(db, result)
    assert claim is not None
    assert await db.fetchval('SELECT status FROM runtime_suspensions') == 'releasing'
    await SuspensionStore(db).release(claim, receipt(claim))
    session = await db.fetchrow('SELECT * FROM runtime_sessions')
    assert session['owner_id'] is None and session['lease_until'] is None
    assert session['active_task_id'] == 'task'
    assert session['snapshot_id'] == claim.snapshot_id
    assert session['execution_epoch'] == claim.owner.execution_epoch
    assert session['reply_id'] == result.reply_id
    assert session['status'] == 'awaiting_interaction'
    assert await db.fetchval('SELECT status FROM runtime_suspensions') == 'released'
    assert await db.fetchval('SELECT active_product_task_id FROM conversations') == 'task'


@pytest.mark.parametrize('case', ['lease','node','snapshot','task','external','accepted','claimed','indeterminate'])
async def test_begin_rejects_stale_or_unsafe_boundary(suspended_database, case):
    _, db, result = suspended_database
    snapshot_id = await db.fetchval('SELECT snapshot_id FROM runtime_sessions')
    node = 'node'
    if case == 'lease':
        await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    elif case == 'node':
        node = 'other'
    elif case == 'snapshot':
        snapshot_id = 'other'
    elif case == 'task':
        await db.execute("UPDATE conversations SET active_product_task_id='other-task'")
    elif case == 'external':
        session = await db.fetchrow('SELECT * FROM runtime_sessions')
        payload = json.loads(await db.fetchval('SELECT payload FROM runtime_snapshots WHERE id=$1',snapshot_id))
        for block in payload['state']['context'][-1]['content']:
            if block['type'] == 'tool_call':
                block['state'] = 'submitted'
        await ExecutionStore(db).save_boundary(result.owner,expected_state_version=session['state_version'],
            status='awaiting_interaction',reply_id=result.reply_id,snapshot=payload,events=[{'type':'fixture'}])
        snapshot_id = await db.fetchval('SELECT snapshot_id FROM runtime_sessions')
    else:
        await submit(db, result.reply_id)
        await db.execute("UPDATE runtime_commands SET status=$1 WHERE id='resume'",case)
    assert await SuspensionStore(db).begin(result.owner,node,snapshot_id,'generation-1') is None
    assert await db.fetchval('SELECT count(*) FROM runtime_suspensions') == 0


@pytest.mark.parametrize('case', ['receipt','lease','control','boundary'])
async def test_release_rechecks_authority_and_never_partially_releases(suspended_database, case):
    _, db, result = suspended_database
    claim = await begin(db, result)
    proof = receipt(claim)
    if case == 'receipt':
        proof = receipt(claim,resource_generation='other')
    elif case == 'lease':
        await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    elif case == 'control':
        await submit(db,result.reply_id)
        await db.execute("UPDATE runtime_commands SET status='claimed' WHERE id='resume'")
    else:
        await db.execute('UPDATE runtime_sessions SET state_version=state_version+1')
    with pytest.raises((LostOwnership, ValueError)):
        await SuspensionStore(db).release(claim,proof)
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') == result.owner.worker_id
    assert await db.fetchval('SELECT status FROM runtime_suspensions') == 'releasing'


async def test_blocked_cleanup_cannot_release_or_restart(suspended_database):
    _, db, result = suspended_database
    claim = await begin(db,result)
    await SuspensionStore(db).block(claim)
    assert await db.fetchval('SELECT status FROM runtime_suspensions') == 'blocked'
    assert await begin(db,result) is None
    with pytest.raises(LostOwnership):
        await SuspensionStore(db).release(claim,receipt(claim))
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') == result.owner.worker_id


@pytest.mark.parametrize('proof',[True,{}, {'resources_quiescent':1}])
async def test_unstructured_cleanup_proof_is_rejected(suspended_database,proof):
    _, db, result = suspended_database
    claim = await begin(db,result)
    with pytest.raises(ValueError):
        await SuspensionStore(db).release(claim,proof)
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') == result.owner.worker_id


async def test_competing_suspensions_have_single_winner(suspended_database):
    import asyncio
    pool, db, result = suspended_database
    snapshot_id = await db.fetchval('SELECT snapshot_id FROM runtime_sessions')
    async def attempt(generation):
        async with pool.acquire() as connection:
            return await SuspensionStore(connection).begin(result.owner,'node',snapshot_id,generation)
    claims = await asyncio.gather(*(attempt(f'g-{i}') for i in range(5)))
    assert sum(c is not None for c in claims) == 1
    assert await db.fetchval('SELECT count(*) FROM runtime_suspensions') == 1


@pytest.mark.parametrize('field,value', [('resources_quiescent',1),('workspace_closed','true'),
    ('execution_epoch',True),('schema_version',True),('extra_field','unexpected')])
async def test_receipt_does_not_coerce_untrusted_values(suspended_database,field,value):
    _, db, result = suspended_database
    claim = await begin(db,result)
    raw = receipt(claim).model_dump()
    raw[field] = value
    with pytest.raises(ValueError):
        await SuspensionStore(db).release(claim,raw)


async def test_failed_release_transaction_rolls_back_receipt(suspended_database):
    _, db, result = suspended_database
    claim = await begin(db,result)
    await db.execute("ALTER TABLE runtime_sessions ADD CONSTRAINT fixture_owner CHECK(owner_id IS NOT NULL)")
    import asyncpg
    with pytest.raises(asyncpg.CheckViolationError):
        await SuspensionStore(db).release(claim,receipt(claim))
    row = await db.fetchrow('SELECT * FROM runtime_suspensions')
    assert row['status'] == 'releasing' and row['cleanup_receipt'] is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') == result.owner.worker_id


@pytest.mark.parametrize('control', ['StopTask','ResumeInteraction'])
async def test_accepted_control_after_begin_survives_release(suspended_database,control):
    _, db, result = suspended_database
    claim = await begin(db,result)
    await submit(db,result.reply_id)
    if control == 'StopTask':
        await db.execute("UPDATE runtime_commands SET kind='StopTask' WHERE id='resume'")
        await db.execute("UPDATE tasks SET status='cancelling' WHERE id='task'")
    await SuspensionStore(db).release(claim,receipt(claim))
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'") == 'accepted'
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None
    assert await db.fetchval('SELECT status FROM runtime_suspensions') == 'released'
    assert await db.fetchval("SELECT status FROM tasks WHERE id='task'") == (
        'cancelling' if control == 'StopTask' else 'awaiting_permission')


@pytest.mark.parametrize('new_owner',[False,True])
async def test_release_retry_is_idempotent_without_touching_future_owner(suspended_database,new_owner):
    _, db, result = suspended_database
    claim = await begin(db,result)
    proof = receipt(claim)
    await SuspensionStore(db).release(claim,proof)
    before = await db.fetchrow('SELECT * FROM runtime_suspensions')
    if new_owner:
        await db.execute("UPDATE runtime_sessions SET owner_id='replacement',execution_epoch=execution_epoch+1,lease_until=clock_timestamp()+interval '30 seconds'")
    await SuspensionStore(db).release(claim,proof)
    assert await db.fetchrow('SELECT * FROM runtime_suspensions') == before
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') == ('replacement' if new_owner else None)


async def test_release_retry_rejects_different_stored_receipt(suspended_database):
    _, db, result = suspended_database
    claim = await begin(db,result)
    proof = receipt(claim)
    await SuspensionStore(db).release(claim,proof)
    await db.execute("UPDATE runtime_suspensions SET cleanup_receipt=cleanup_receipt || '{\"workspace_closed\":false}'::jsonb")
    with pytest.raises(LostOwnership):
        await SuspensionStore(db).release(claim,proof)


async def test_begin_lease_expiring_after_validation_rolls_back_insert(suspended_database):
    _, db, result = suspended_database
    snapshot_id = await db.fetchval('SELECT snapshot_id FROM runtime_sessions')
    class ExpiringStore(SuspensionStore):
        async def _validate(self,*args,**kwargs):
            claim = await super()._validate(*args,**kwargs)
            assert claim is not None
            await self.db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
            return claim
    with pytest.raises(LostOwnership):
        await ExpiringStore(db).begin(result.owner,'node',snapshot_id,'generation-1')
    assert await db.fetchval('SELECT count(*) FROM runtime_suspensions') == 0
