"""A Stop arriving during joined seal I/O cannot invalidate terminal delivery."""
import asyncio
import json
import threading

from workmate_runtime.delivery.files import DeliveryFiles
from workmate_runtime.delivery.recovery import TerminalDeliveryRecovery
from test_postgres_execution_store import database
from test_worker_delivery_store import delivery_db
from test_terminal_delivery_recovery import sealed_terminal
from test_running_stop import stop_intent


async def test_running_stop_during_capture_preserves_exact_final_core_seal(delivery_db,tmp_path,monkeypatch):
    pool,db=delivery_db
    loop=asyncio.get_running_loop()
    entered=asyncio.Event()
    release=threading.Event()
    capture=DeliveryFiles.capture
    def paused(self,*args,**kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(10):
            raise RuntimeError('test capture synchronization timed out')
        return capture(self,*args,**kwargs)
    monkeypatch.setattr(DeliveryFiles,'capture',paused)
    execution=asyncio.create_task(sealed_terminal(pool,tmp_path))
    try:
        await asyncio.wait_for(entered.wait(),5)
        await stop_intent(db)
        async with asyncio.timeout(5):
            while await db.fetchval('SELECT status FROM runtime_sessions')!='stopping':
                assert not execution.done()
                await asyncio.sleep(.02)
        release.set()
        result,workspace,objects=await asyncio.wait_for(execution,10)
        assert result.status=='completed'  # Core finished before the late Stop.
        seal=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_seals'))
        boundary=json.loads(await db.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='EXECUTION_BOUNDARY'"))
        assert seal['status']==boundary['status']=='completed'
        assert boundary['stop_cleanup']['command_id']=='stop'
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
        assert await TerminalDeliveryRecovery(pool,'node',objects).recover_pending()=={}
        assert await db.fetchval('SELECT owner_id FROM runtime_sessions')=='worker'
        assert await db.fetchval('SELECT execution_epoch FROM runtime_sessions')==1
        payload=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_captures'))
        assert (objects/payload['files'][0]['storage_key']).read_text()=='sealed original'
    finally:
        release.set()
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution,return_exceptions=True)


async def test_parked_supervisor_forwards_seal_through_real_stop_and_recovery(delivery_db,tmp_path):
    from types import SimpleNamespace
    from agentscope.message import ToolCallBlock
    from workmate_runtime.delivery.coordinator import DeliveryCoordinator
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.execution.running import RunningExecutionController
    from workmate_runtime.execution.supervisor import ParkedSupervisor
    from test_core_engine import ConfirmTool
    from test_execution_controller import prepare_reply
    from test_resume_controller import prepare_resume
    pool,db=delivery_db
    workspace=tmp_path/'workspace'
    (workspace/'output').mkdir(parents=True)
    objects=tmp_path/'objects'
    registry=TaskResourceRegistry()
    coordinator=DeliveryCoordinator(pool,'node',objects,registry,max_file_bytes=1024,max_files=20)
    tool=ConfirmTool()
    prepare,_=prepare_reply([[ToolCallBlock(id='call',name=tool.name,input='{}')]],tool=tool)
    async def wrapped(owner,snapshot,guard):
        registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
        await coordinator.prepare(owner,SimpleNamespace(owner=owner,workspace=SimpleNamespace(host_workdir=workspace)))
        (workspace/'output/partial.txt').write_text('partial result')
        return await prepare(owner,snapshot,guard)
    controller=RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup,
        seal_terminal=coordinator.seal_terminal)
    parked=await controller.start('command','worker','node',wrapped)
    assert parked.status=='awaiting_interaction'
    await stop_intent(db)
    async def allowed(*args): return True
    result=await asyncio.wait_for(ParkedSupervisor(pool,execution_controller=controller).serve(
        parked.owner,'node',resume_slots=asyncio.Semaphore(1),prepare_resume=prepare_resume(tool),
        prepare_stop=prepare_resume(tool),authorize_resume=allowed,verify_cleanup=registry.verify_cleanup),10)
    assert result.status=='interrupted' and tool.calls==0
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_seals')==1
    assert await TerminalDeliveryRecovery(pool,'node',objects).recover_pending()=={}
    payload=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_captures'))
    assert (objects/payload['files'][0]['storage_key']).read_text()=='partial result'
