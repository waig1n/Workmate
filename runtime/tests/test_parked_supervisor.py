"""Parked owners remain live without occupying execution capacity."""
import asyncio

import pytest
from test_postgres_execution_store import database  # noqa: F401
from test_resume_controller import parked, submit, prepare_resume, allow
from test_parked_stop import stop_intent, quiescent
from test_core_engine import ConfirmTool
from workmate_runtime.execution.supervisor import ParkedSupervisor, ReconciliationRequired
from workmate_runtime.transport.postgres import LostOwnership


def serve(supervisor, first, slots, tool=None):
    return supervisor.serve(first.owner,'node',resume_slots=slots,
        prepare_resume=prepare_resume(tool or ConfirmTool()),prepare_stop=prepare_resume(ConfirmTool()),
        authorize_resume=allow,verify_cleanup=quiescent)


async def test_waiting_for_user_keeps_lease_and_stop_needs_no_execution_slot(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '1 second'")
    original=await db.fetchval('SELECT lease_until FROM runtime_sessions')
    supervisor=ParkedSupervisor(pool,lease_seconds=1)
    run=asyncio.create_task(serve(supervisor,first,asyncio.Semaphore(0)))
    try:
        async with asyncio.timeout(6):
            while not await db.fetchval("SELECT clock_timestamp()>$1::timestamptz+interval '1 second' AND lease_until>clock_timestamp() FROM runtime_sessions",original):
                await asyncio.sleep(0.03)
        await stop_intent(db)
        result=await asyncio.wait_for(run,6)
        assert result.status=='interrupted'
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    finally:
        run.cancel()
        await asyncio.gather(run,return_exceptions=True)


@pytest.mark.parametrize("notified", [False, True])
async def test_stop_wins_while_resume_waits_for_capacity(database, notified):
    pool,db=database
    first=await parked(pool,db)
    await submit(db,first.reply_id)
    tool=ConfirmTool()
    slots=asyncio.Semaphore(0)
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus("unused") if notified else None
    run=asyncio.create_task(serve(ParkedSupervisor(pool,lease_seconds=1,wakeup_bus=bus),first,slots,tool))
    try:
        await asyncio.sleep(0.2)
        await stop_intent(db)
        if bus is not None:
            bus.publish_nowait("commands")
        result=await asyncio.wait_for(run,6)
        assert result.status=='interrupted' and tool.calls==0
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='rejected'
        slots.release()
        await asyncio.wait_for(slots.acquire(),1)  # no orphaned capacity waiter
    finally:
        run.cancel()
        await asyncio.gather(run,return_exceptions=True)


@pytest.mark.parametrize('status',['claimed','indeterminate'])
async def test_unresolved_control_is_not_reexecuted_or_kept_alive_as_idle(database,status):
    pool,db=database
    first=await parked(pool,db)
    await submit(db,first.reply_id)
    await db.execute("UPDATE runtime_commands SET status=$1 WHERE id='resume'",status)
    with pytest.raises(ReconciliationRequired):
        await serve(ParkedSupervisor(pool),first,asyncio.Semaphore(1))
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")==status


async def test_duplicate_registration_and_shutdown_leave_no_capacity_or_lease_tasks(database):
    pool,db=database
    first=await parked(pool,db)
    supervisor=ParkedSupervisor(pool,lease_seconds=1)
    run=asyncio.create_task(serve(supervisor,first,asyncio.Semaphore(0)))
    try:
        await asyncio.sleep(0.15)
        for duplicate in (supervisor,ParkedSupervisor(pool)):
            with pytest.raises(ReconciliationRequired,match='already'):
                await serve(duplicate,first,asyncio.Semaphore(0))
    finally:
        run.cancel()
        await asyncio.gather(run,return_exceptions=True)
    lease=await db.fetchval('SELECT lease_until FROM runtime_sessions')
    await asyncio.sleep(0.5)
    assert await db.fetchval('SELECT lease_until FROM runtime_sessions')==lease
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'


async def test_lost_lease_never_revives_owner(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    with pytest.raises(LostOwnership):
        await serve(ParkedSupervisor(pool,lease_seconds=1),first,asyncio.Semaphore(1))
    assert await db.fetchval('SELECT lease_until<clock_timestamp() FROM runtime_sessions')


async def test_partial_resume_reparks_then_accepts_second_decision(database):
    from agentscope.message import TextBlock
    pool,db=database
    first=await parked(pool,db,partial=True)
    tool=ConfirmTool()
    tool.safety_ask=True
    tool.is_concurrency_safe=True
    async def prepare(owner,snapshot,guard):
        pending=snapshot.to_state().get_awaiting_tool_calls('test')
        responses=[] if len(pending)>1 else [[TextBlock(text='done')]]
        return await prepare_resume(tool,responses)(owner,snapshot,guard)
    supervisor=ParkedSupervisor(pool,lease_seconds=1)
    slots=asyncio.Semaphore(1)
    run=asyncio.create_task(supervisor.serve(first.owner,'node',resume_slots=slots,prepare_resume=prepare,
        prepare_stop=prepare_resume(ConfirmTool()),authorize_resume=allow,verify_cleanup=quiescent))
    try:
        await submit(db,first.reply_id)
        async with asyncio.timeout(6):
            while await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")!='completed':
                await asyncio.sleep(0.03)
        assert tool.calls==1
        await asyncio.sleep(1.5)
        await submit(db,first.reply_id,cid='second',call='b')
        result=await asyncio.wait_for(run,6)
        assert result.status=='completed' and result.reply_id==first.reply_id and tool.calls==2
        await asyncio.wait_for(slots.acquire(),1)
    finally:
        run.cancel()
        await asyncio.gather(run,return_exceptions=True)


async def test_shutdown_joins_active_preparation_and_returns_capacity(database):
    pool,db=database
    first=await parked(pool,db)
    await submit(db,first.reply_id)
    entered,cleaned=asyncio.Event(),asyncio.Event()
    async def prepare(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    slots=asyncio.Semaphore(1)
    supervisor=ParkedSupervisor(pool,lease_seconds=1)
    run=asyncio.create_task(supervisor.serve(first.owner,'node',resume_slots=slots,prepare_resume=prepare,
        prepare_stop=prepare_resume(ConfirmTool()),authorize_resume=allow,verify_cleanup=quiescent))
    try:
        await asyncio.wait_for(entered.wait(),6)
    finally:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert cleaned.is_set()
    await asyncio.wait_for(slots.acquire(),1)
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='claimed'
    with pytest.raises(ReconciliationRequired):
        await serve(supervisor,first,asyncio.Semaphore(1))


async def test_heartbeat_database_failure_stops_supervision(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute("""CREATE FUNCTION reject_renewal() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'renewal unavailable'; END $$;
        CREATE TRIGGER fail_renewal BEFORE UPDATE OF lease_until ON runtime_sessions
        FOR EACH ROW EXECUTE FUNCTION reject_renewal();""")
    with pytest.raises(Exception,match='renewal unavailable'):
        await asyncio.wait_for(serve(ParkedSupervisor(pool,lease_seconds=1),first,asyncio.Semaphore(1)),5)
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'


async def test_wrong_node_and_unparked_entry_are_not_healthy_idle(database):
    pool,db=database
    first=await parked(pool,db)
    await db.execute("UPDATE runtime_sessions SET storage_node_id='other'")
    with pytest.raises(LostOwnership):
        await serve(ParkedSupervisor(pool),first,asyncio.Semaphore(1))
    await db.execute("UPDATE runtime_sessions SET storage_node_id='node',status='running'")
    with pytest.raises(ReconciliationRequired):
        await serve(ParkedSupervisor(pool),first,asyncio.Semaphore(1))


async def test_revoked_resume_is_not_retried_forever(database):
    pool,db=database
    first=await parked(pool,db)
    await submit(db,first.reply_id)
    calls=[]
    async def deny(*args):
        calls.append('checked')
        return False
    slots=asyncio.Semaphore(1)
    with pytest.raises(ReconciliationRequired,match='not claimed'):
        await asyncio.wait_for(ParkedSupervisor(pool,lease_seconds=1).serve(first.owner,'node',resume_slots=slots,
            prepare_resume=prepare_resume(ConfirmTool()),prepare_stop=prepare_resume(ConfirmTool()),
            authorize_resume=deny,verify_cleanup=quiescent),6)
    assert calls==['checked']
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='accepted'
    await asyncio.wait_for(slots.acquire(),1)
