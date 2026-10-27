"""Shared PostgreSQL resource residency prevents wrong-worker warm claims."""
import asyncio
import pytest
from sqlalchemy import text
from test_execution_context import database, claim as claim_execution
from workmate_product.persistence.repositories import workspace_residencies as store


async def held(sessions,admin):
    await claim_execution(admin)
    async with sessions.begin() as db:
        return await store.record_held(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
            node_id='node',generation='generation')


async def idle(admin):
    await admin.execute("""UPDATE runtime_sessions SET owner_id=NULL,lease_until=NULL,active_task_id=NULL;
        UPDATE conversations SET active_product_task_id=NULL;
        UPDATE runtime_commands SET status='completed'; UPDATE tasks SET status='completed'""")


def proof(claim):
    return dict(schema_version=1,session_id=claim.session_id,worker_id=claim.worker_id,node_id=claim.node_id,
        workspace_generation=claim.generation,hold_version=claim.hold_version,
        resources_quiescent=True,workspace_closed=True,ownership_released=True)


async def request(sessions):
    async with sessions.begin() as db:
        return await store.request_release(db,session_id='s',expected_worker='worker',expected_hold_version=1,
            generation='generation',node_id='node')


async def test_registration_requires_current_execution_and_warm_routing_is_exclusive(database):
    sessions,admin=database
    with pytest.raises(store.ResidencyConflict):
        async with sessions.begin() as db:
            await store.record_held(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
                node_id='node',generation='generation')
    first=await held(sessions,admin)
    async with sessions.begin() as db:
        second=await store.record_held(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
            node_id='node',generation='generation')
        assert first['hold_version']==second['hold_version']==1
        assert await store.start_allowed(db,'s','worker','node')
        assert not await store.start_allowed(db,'s','other','node')
        assert not await store.start_allowed(db,'s','worker','foreign-node')


async def test_idle_release_waits_for_delivery_then_commits_exact_receipt(database):
    sessions,admin=database
    await held(sessions,admin)
    pending=await request(sessions)
    async with sessions.begin() as db:
        assert await store.begin_release(db,pending['id'],'worker','node') is None
    await idle(admin)
    async with sessions.begin() as db:
        released=await store.begin_release(db,pending['id'],'worker','node')
        assert released is not None
        assert not await store.start_allowed(db,'s','worker','node')
    async with sessions.begin() as db:
        await store.finish_release(db,released,proof(released))
    async with sessions.begin() as db:
        await store.finish_release(db,released,proof(released))
        assert await store.start_allowed(db,'s','peer','node')
    assert await admin.fetchval("SELECT status FROM workspace_residencies")=='released'


async def test_old_release_cannot_close_new_hold_and_versions_increase(database):
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    pending=await request(sessions)
    async with sessions.begin() as db:
        old=await store.begin_release(db,pending['id'],'worker','node')
        await store.finish_release(db,old,proof(old))
    await claim_execution(admin)
    async with sessions.begin() as db:
        newer=await store.record_held(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
            node_id='node',generation='generation')
        assert newer['hold_version']==2
        assert await store.begin_release(db,pending['id'],'worker','node') is None
        await store.finish_release(db,old,proof(old))
    assert await admin.fetchval('SELECT hold_version FROM workspace_residencies')==2
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'


async def test_failed_close_latches_blocked_and_bad_proof_cannot_release(database):
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    pending=await request(sessions)
    async with sessions.begin() as db:
        release=await store.begin_release(db,pending['id'],'worker','node')
    with pytest.raises(ValueError):
        async with sessions.begin() as db:
            await store.finish_release(db,release,{**proof(release),'ownership_released':False})
    async with sessions.begin() as db:
        await store.block_release(db,release,'close_failed')
        assert not await store.start_allowed(db,'s','peer','node')
    with pytest.raises(store.ResidencyConflict):
        async with sessions.begin() as db:
            await store.finish_release(db,release,proof(release))


async def test_owned_close_retains_execution_gates_and_old_receipt_is_idempotent(database):
    sessions,admin=database
    await held(sessions,admin)
    claim=store.ReleaseClaim(store._request_id('s',1),'s','worker','node','generation',1)
    receipt={**proof(claim),'task_id':'t','execution_epoch':1}
    args=dict(session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
        node_id='node',generation='generation',hold_version=1,receipt=receipt)
    async with sessions.begin() as db:
        await store.finish_owned_release(db,**args)
    assert await admin.fetchval("SELECT owner_id FROM runtime_sessions")=='worker'
    assert await admin.fetchval("SELECT active_task_id FROM runtime_sessions")=='t'
    assert await admin.fetchval("SELECT active_product_task_id FROM conversations")=='t'
    async with sessions.begin() as db:
        current=await store.record_held(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
            node_id='node',generation='generation')
        assert current['hold_version']==2
        await store.finish_owned_release(db,**args)
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'
    assert await admin.fetchval('SELECT hold_version FROM workspace_residencies')==2


@pytest.mark.parametrize('mutation',[
    {'task_id':'old-task'}, {'execution_epoch':2}, {'workspace_closed':1},
    {'hold_version':True}, {'resources_quiescent':False}, {'unknown':True},
])
async def test_owned_release_rejects_inexact_or_malformed_receipts(database,mutation):
    sessions,admin=database
    await held(sessions,admin)
    claim=store.ReleaseClaim(store._request_id('s',1),'s','worker','node','generation',1)
    with pytest.raises(ValueError):
        async with sessions.begin() as db:
            await store.finish_owned_release(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
                node_id='node',generation='generation',hold_version=1,
                receipt={**proof(claim),'task_id':'t','execution_epoch':1,**mutation})
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'


async def test_two_release_consumers_have_one_cleanup_winner(database):
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    pending=await request(sessions)
    async def begin():
        async with sessions.begin() as db:
            return await store.begin_release(db,pending['id'],'worker','node')
    outcomes=await asyncio.gather(begin(),begin())
    assert sum(x is not None for x in outcomes)==1
    async with sessions.begin() as db:
        assert not await store.start_allowed(db,'s','worker','node')


async def test_pending_start_can_wait_for_close_but_claimed_command_cannot(database):
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    await admin.execute("UPDATE conversations SET active_product_task_id='t'; UPDATE tasks SET status='created'; "
        "UPDATE runtime_task_executions SET status='accepted',execution_epoch=0; UPDATE runtime_commands SET status='claimed'")
    pending=await request(sessions)
    async with sessions.begin() as db:
        assert await store.begin_release(db,pending['id'],'worker','node') is None
    await admin.execute("UPDATE runtime_commands SET status='accepted'")
    async with sessions.begin() as db:
        assert await store.begin_release(db,pending['id'],'worker','node') is not None


async def test_foreign_release_and_changed_directory_are_rejected(database):
    sessions,admin=database
    await held(sessions,admin)
    pending=await request(sessions)
    async with sessions.begin() as db:
        assert await store.begin_release(db,pending['id'],'peer','node') is None
        assert await store.begin_release(db,pending['id'],'worker','foreign') is None
        assert await store.request_release(db,session_id='s',expected_worker='worker',expected_hold_version=2,
            generation='generation',node_id='node') is None
    with pytest.raises(store.ResidencyConflict):
        async with sessions.begin() as db:
            await store.record_held(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
                node_id='node',generation='replacement-directory')


async def test_expired_execution_cannot_register_or_release(database):
    sessions,admin=database
    await held(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    claim=store.ReleaseClaim(store._request_id('s',1),'s','worker','node','generation',1)
    with pytest.raises(store.ResidencyConflict):
        async with sessions.begin() as db:
            await store.finish_owned_release(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
                node_id='node',generation='generation',hold_version=1,
                receipt={**proof(claim),'task_id':'t','execution_epoch':1})
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'


async def test_start_admission_and_idle_close_serialize_on_session_gate(database):
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    pending=await request(sessions)
    entered=asyncio.Event()
    async def release():
        entered.set()
        async with sessions.begin() as db:
            return await store.begin_release(db,pending['id'],'worker','node')
    async with sessions.begin() as db:
        assert await store.start_allowed(db,'s','worker','node')
        contender=asyncio.create_task(release())
        await entered.wait()
        # The execution claim shares this transaction and session lock. An idle
        # release cannot cross between routing validation and installing owner.
        await db.execute(text("UPDATE runtime_sessions SET owner_id='worker',active_task_id='t',"
            "lease_until=clock_timestamp()+interval '30 seconds' WHERE id='s'"))
    assert await asyncio.wait_for(contender,5) is None
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'


@pytest.mark.parametrize('operation',['register','close'])
async def test_final_lease_expiry_rolls_back_holding_mutation(database,monkeypatch,operation):
    sessions,admin=database
    await held(sessions,admin)
    owner=store._owner
    calls=0
    async def expiring(db,session,**kwargs):
        nonlocal calls
        calls+=1
        await owner(db,session,**kwargs)
        if calls==1:
            await db.execute(text("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'"))
    monkeypatch.setattr(store,'_owner',expiring)
    claim=store.ReleaseClaim(store._request_id('s',1),'s','worker','node','generation',1)
    with pytest.raises(store.ResidencyConflict):
        async with sessions.begin() as db:
            args=dict(session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
                node_id='node',generation='generation')
            if operation=='register':
                await store.record_held(db,**args)
            else:
                await store.finish_owned_release(db,**args,hold_version=1,
                    receipt={**proof(claim),'task_id':'t','execution_epoch':1})
    assert calls==2
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'
    assert await admin.fetchval('SELECT count(*) FROM workspace_release_requests')==0


async def test_confirm_committed_release_claim_does_not_manufacture_cleanup_winner(database):
    from dataclasses import replace
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    pending=await request(sessions)
    expected=store.ReleaseClaim(pending['id'],'s','worker','node','generation',1)
    async with sessions.begin() as db:
        assert not await store.confirm_release_claim(db,expected)
    async with sessions.begin() as db:
        won=await store.begin_release(db,pending['id'],'worker','node')
    async with sessions.begin() as db:
        assert await store.confirm_release_claim(db,won)
        assert not await store.confirm_release_claim(db,replace(won,hold_version=2))
        assert not await store.confirm_release_claim(db,replace(won,worker_id='peer'))
        assert not await store.confirm_release_claim(db,replace(won,generation='different'))
        assert await store.begin_release(db,pending['id'],'worker','node') is None
    await admin.execute("UPDATE runtime_sessions SET active_task_id='t'")
    async with sessions.begin() as db:
        assert not await store.confirm_release_claim(db,won)
    await admin.execute("UPDATE runtime_sessions SET active_task_id=NULL")
    async with sessions.begin() as db:
        await store.finish_release(db,won,proof(won))
        assert not await store.confirm_release_claim(db,won)


async def test_confirm_release_rejects_rolled_back_or_missing_claim(database):
    sessions,admin=database
    await held(sessions,admin)
    await idle(admin)
    pending=await request(sessions)
    with pytest.raises(RuntimeError,match='rollback'):
        async with sessions.begin() as db:
            claim=await store.begin_release(db,pending['id'],'worker','node')
            raise RuntimeError('rollback')
    async with sessions.begin() as db:
        assert not await store.confirm_release_claim(db,claim)
        missing=store.ReleaseClaim('missing','s','worker','node','generation',1)
        assert not await store.confirm_release_claim(db,missing)
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'


async def test_superseded_session_cannot_register_or_reuse_workspace(database):
    sessions, admin = database
    await held(sessions, admin)
    await admin.execute("UPDATE runtime_sessions SET superseded_at=clock_timestamp()")
    async with sessions.begin() as db:
        assert not await store.start_allowed(db, 's', 'worker', 'node')
    with pytest.raises(store.ResidencyConflict):
        async with sessions.begin() as db:
            await store.record_held(db, session_id='s', task_id='t', worker_id='worker',
                execution_epoch=1, node_id='node', generation='generation')
    assert await admin.fetchval('SELECT hold_version FROM workspace_residencies') == 1
    assert await admin.fetchval('SELECT holder_worker_id FROM workspace_residencies') == 'worker'
