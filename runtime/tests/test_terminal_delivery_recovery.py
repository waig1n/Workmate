"""Actual PostgreSQL crash windows; immutable terminal bytes survive output loss."""
import asyncio
import json
from types import SimpleNamespace
import pytest

from test_worker_delivery_store import delivery_db
from test_postgres_execution_store import database
from test_execution_controller import prepare_reply
from agentscope.message import TextBlock
from workmate_runtime.delivery.coordinator import DeliveryCoordinator
from workmate_runtime.delivery.recovery import TerminalDeliveryRecovery
from workmate_runtime.execution.controller import ExecutionAborted
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.transport.delivery import DeliveryStore,DeliveryConflict


async def sealed_terminal(pool,tmp_path,*,controller_class=RunningExecutionController):
    workspace=tmp_path/'workspace'
    (workspace/'output').mkdir(parents=True)
    objects=tmp_path/'artifacts'
    registry=TaskResourceRegistry()
    delivery=DeliveryCoordinator(pool,'node',objects,registry,max_file_bytes=1024,max_files=20)
    prepare,_=prepare_reply([[TextBlock(text='finished')]])
    async def wrapped(owner,snapshot,guard):
        registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
        await delivery.prepare(owner,SimpleNamespace(owner=owner,workspace=SimpleNamespace(host_workdir=workspace)))
        (workspace/'output/result.txt').write_text('sealed original')
        return await prepare(owner,snapshot,guard)
    result=await controller_class(pool,verify_cleanup=registry.verify_cleanup,
        seal_terminal=delivery.seal_terminal).start('command','worker','node',wrapped)
    return result,workspace,objects


async def test_restart_recovers_only_sealed_bytes_after_output_overwrite_and_expired_owner(delivery_db,tmp_path):
    pool,db=delivery_db
    result,workspace,objects=await sealed_terminal(pool,tmp_path)
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0
    seal=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_seals'))
    boundary=json.loads(await db.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='EXECUTION_BOUNDARY'"))
    assert boundary['delivery_seal_sha256']==await db.fetchval('SELECT digest FROM runtime_delivery_seals')
    (workspace/'output/result.txt').write_text('polluted mutable output')
    await db.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 minute'")
    recovered=TerminalDeliveryRecovery(pool,'node',objects)
    assert await recovered.recover_pending()=={}
    capture=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_captures'))
    assert capture==seal['capture']
    assert (objects/capture['files'][0]['storage_key']).read_text()=='sealed original'
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions')=='worker'
    assert await db.fetchval('SELECT execution_epoch FROM runtime_sessions')==1
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    assert await db.fetchval('SELECT lease_until<clock_timestamp() FROM runtime_sessions')
    assert await recovered.recover(result.owner)==await db.fetchval('SELECT digest FROM runtime_delivery_captures')


async def test_seal_without_terminal_commit_cannot_authorize_capture(delivery_db,tmp_path):
    pool,db=delivery_db
    class CrashBeforeBoundary(RunningExecutionController):
        async def _save_normal(self,*args): raise RuntimeError('simulated crash after seal')
    with pytest.raises(ExecutionAborted):
        await sealed_terminal(pool,tmp_path,controller_class=CrashBeforeBoundary)
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_seals')==1
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots')==0
    assert await TerminalDeliveryRecovery(pool,'node',tmp_path/'artifacts').recover_pending()=={}
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0


@pytest.mark.parametrize('mutation',[
    "UPDATE conversations SET active_product_task_id=NULL",
    "UPDATE runtime_sessions SET execution_epoch=2",
    "UPDATE runtime_sessions SET owner_id='new-worker'",
    "UPDATE runtime_sessions SET storage_node_id='other-node'",
    "UPDATE runtime_events SET payload=payload-'delivery_seal_sha256' WHERE payload->>'type'='EXECUTION_BOUNDARY'",
    "UPDATE runtime_events SET payload=payload-'resource_cleanup' WHERE payload->>'type'='EXECUTION_BOUNDARY'",
    "UPDATE runtime_commands SET status='indeterminate' WHERE id='command'",
])
async def test_recovery_rejects_changed_terminal_authority_or_missing_proof(delivery_db,tmp_path,mutation):
    pool,db=delivery_db
    result,workspace,objects=await sealed_terminal(pool,tmp_path)
    await db.execute(mutation)
    with pytest.raises(DeliveryConflict):
        await TerminalDeliveryRecovery(pool,'node',objects).recover(result.owner)
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0


async def test_concurrent_recovery_is_idempotent_and_does_not_reopen_workspace(delivery_db,tmp_path,monkeypatch):
    from workmate_runtime.delivery.files import DeliveryFiles
    pool,db=delivery_db
    result,workspace,objects=await sealed_terminal(pool,tmp_path)
    def forbidden(*args,**kwargs): raise AssertionError('Recovery may not scan mutable output')
    monkeypatch.setattr(DeliveryFiles,'capture',forbidden)
    monkeypatch.setattr(DeliveryFiles,'baseline',forbidden)
    recovery=TerminalDeliveryRecovery(pool,'node',objects)
    a,b=await asyncio.gather(recovery.recover(result.owner),recovery.recover(result.owner))
    assert a==b and await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==1


async def test_missing_seal_or_corrupt_object_remains_blocked(delivery_db,tmp_path):
    from workmate_runtime.delivery.files import DeliveryFileError
    pool,db=delivery_db
    result,workspace,objects=await sealed_terminal(pool,tmp_path)
    seal=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_seals'))
    (objects/seal['capture']['files'][0]['storage_key']).write_text('corrupt')
    recovery=TerminalDeliveryRecovery(pool,'node',objects)
    assert await recovery.recover_pending()=={'session':'DeliveryFileError'}
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0
    import asyncpg
    with pytest.raises(asyncpg.RaiseError):
        await db.execute("UPDATE runtime_delivery_seals SET digest=repeat('0',64)")
    with pytest.raises(asyncpg.RaiseError): await db.execute('DELETE FROM runtime_delivery_seals')

async def test_historical_terminal_without_seal_is_not_reconstructed_from_output(delivery_db,tmp_path):
    from test_worker_delivery_store import complete
    pool,db=delivery_db
    await complete(pool,db,sealed=False)
    assert await TerminalDeliveryRecovery(pool,'node',tmp_path/'objects').recover_pending()=={'session':'DeliveryConflict'}
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0


async def test_existing_capture_remains_idempotent_after_product_releases_gate(delivery_db,tmp_path):
    pool,db=delivery_db
    result,workspace,objects=await sealed_terminal(pool,tmp_path)
    recovery=TerminalDeliveryRecovery(pool,'node',objects)
    digest=await recovery.recover(result.owner)
    await db.execute("UPDATE conversations SET active_product_task_id=NULL")
    await db.execute("UPDATE runtime_sessions SET active_task_id=NULL,owner_id=NULL,lease_until=NULL")
    assert await recovery.recover(result.owner)==digest


async def test_sealing_failure_does_not_replace_end_event_with_false_terminal(delivery_db,tmp_path,monkeypatch):
    from workmate_runtime.delivery.files import DeliveryFiles,DeliveryFileError
    pool,db=delivery_db
    def unavailable(*args,**kwargs): raise DeliveryFileError('artifact volume unavailable')
    monkeypatch.setattr(DeliveryFiles,'capture',unavailable)
    with pytest.raises(ExecutionAborted): await sealed_terminal(pool,tmp_path)
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'")==1
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_seals')==0
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots')==0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'")=='claimed'


async def test_authority_is_rechecked_after_object_verification(delivery_db,tmp_path,monkeypatch):
    import workmate_runtime.delivery.recovery as module
    pool,db=delivery_db
    result,workspace,objects=await sealed_terminal(pool,tmp_path)
    verify=module._joined_io
    async def changed_after_verify(*args,**kwargs):
        result=await verify(*args,**kwargs)
        await db.execute("UPDATE conversations SET active_product_task_id=NULL")
        return result
    monkeypatch.setattr(module,'_joined_io',changed_after_verify)
    with pytest.raises(DeliveryConflict):
        await TerminalDeliveryRecovery(pool,'node',objects).recover(result.owner)
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0


async def test_parked_stop_seals_drained_reply_and_recovery_uses_its_bound_bytes(delivery_db,tmp_path):
    from agentscope.message import ToolCallBlock
    from test_core_engine import ConfirmTool
    from test_resume_controller import prepare_resume
    from test_parked_stop import stop_intent
    from workmate_runtime.execution.stopping import ParkedStopController
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
        (workspace/'output/partial.txt').write_text('partial artifact')
        return await prepare(owner,snapshot,guard)
    parked=await RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup,
        seal_terminal=coordinator.seal_terminal).start('command','worker','node',wrapped)
    assert parked.status=='awaiting_interaction'
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_seals')==0
    await stop_intent(db)
    stopped=await ParkedStopController(pool,seal_terminal=coordinator.seal_terminal).stop(
        'stop','worker','node',prepare_resume(tool),verify_cleanup=registry.verify_cleanup)
    assert stopped.status=='interrupted' and tool.calls==0
    assert await TerminalDeliveryRecovery(pool,'node',objects).recover_pending()=={}
    payload=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_captures'))
    assert (objects/payload['files'][0]['storage_key']).read_text()=='partial artifact'
