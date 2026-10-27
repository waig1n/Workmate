"""Process replacement discovers released permission work without a hot registry."""
import asyncio
from dataclasses import replace
import pytest
from agentscope.message import TextBlock
from test_postgres_execution_store import database
from test_resume_controller import parked,submit,prepare_resume,allow
from test_suspension_store import begin,receipt
from test_parked_stop import stop_intent,quiescent
from test_core_engine import ConfirmTool
from test_worker_service import wait_until
from workmate_runtime.transport.suspension import SuspensionStore
from workmate_runtime.execution.controller import ExecutionController
from polling_worker import WorkerService,WorkerBindings
from workmate_runtime.execution.supervisor import ParkedSupervisor


async def noop_pause(owner,node):
    return False


async def test_two_replacement_workers_resume_once(database):
    pool,db=database
    first=await parked(pool,db)
    claim=await begin(db,first)
    await SuspensionStore(db).release(claim,receipt(claim))
    await submit(db,first.reply_id)
    tool=ConfirmTool()
    async def no_start(*args):
        raise AssertionError('cold resume cannot restart task')
    callbacks=WorkerBindings(no_start,prepare_resume(tool,[[TextBlock(text='done')]]),
        prepare_resume(tool),allow,quiescent,suspend=noop_pause)
    workers=[WorkerService(pool,'node',callbacks,controller=ExecutionController(pool),drain_seconds=0) for _ in range(2)]
    stop=asyncio.Event()
    loops=[asyncio.create_task(w.serve(stop)) for w in workers]
    try:
        async def finished():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='completed'
        await wait_until(finished)
        assert tool.calls==1
        assert await db.fetchval('SELECT execution_epoch FROM runtime_sessions')==2
        assert await db.fetchval('SELECT reply_id FROM runtime_sessions')==first.reply_id
        assert not any(w.failures for w in workers)
    finally:
        stop.set()
        await asyncio.gather(*loops)


async def test_cold_stop_runs_without_inference_capacity(database):
    pool,db=database
    first=await parked(pool,db)
    claim=await begin(db,first)
    await SuspensionStore(db).release(claim,receipt(claim))
    await stop_intent(db)
    tool=ConfirmTool()
    callbacks=WorkerBindings(None,None,prepare_resume(tool),allow,quiescent,suspend=noop_pause)
    worker=WorkerService(pool,'node',callbacks,controller=ExecutionController(pool),capacity=1,drain_seconds=0)
    await worker._slots.acquire()
    stop=asyncio.Event()
    loop=asyncio.create_task(worker.serve(stop))
    try:
        async def finished():
            return await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
        await wait_until(finished)
        assert tool.calls==0 and worker.available_capacity==0
        assert await db.fetchval('SELECT reply_id FROM runtime_sessions')==first.reply_id
        assert not worker.failures
    finally:
        stop.set()
        await loop


async def test_supervisor_exits_after_committed_pause(database):
    pool,db=database
    first=await parked(pool,db)
    async def pause(owner,node):
        async with pool.acquire() as connection:
            claim=await begin(connection,first)
            await SuspensionStore(connection).release(claim,receipt(claim))
        return True
    result=await ParkedSupervisor(pool,suspend=pause).serve(first.owner,'node',resume_slots=asyncio.Semaphore(0),
        prepare_resume=None,prepare_stop=None,authorize_resume=allow,verify_cleanup=quiescent)
    assert result is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None


async def test_pause_commit_beats_heartbeat_before_callback_returns(database,monkeypatch):
    from workmate_runtime.transport.postgres import ExecutionStore
    pool,db=database
    first=await parked(pool,db)
    released=asyncio.Event()
    renewed=asyncio.Event()
    renew=ExecutionStore.renew
    async def late_renew(store,*args,**kwargs):
        await released.wait()
        try:
            return await renew(store,*args,**kwargs)
        finally:
            renewed.set()
    monkeypatch.setattr(ExecutionStore,'renew',late_renew)
    async def pause(owner,node):
        async with pool.acquire() as connection:
            claim=await begin(connection,first)
            await SuspensionStore(connection).release(claim,receipt(claim))
        released.set()
        await renewed.wait()
        await asyncio.sleep(.05)  # Caller has not yet observed the successful commit.
        return True
    result=await asyncio.wait_for(ParkedSupervisor(pool,suspend=pause).serve(first.owner,'node',
        resume_slots=asyncio.Semaphore(0),prepare_resume=None,prepare_stop=None,
        authorize_resume=allow,verify_cleanup=quiescent),5)
    assert result is None
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None
