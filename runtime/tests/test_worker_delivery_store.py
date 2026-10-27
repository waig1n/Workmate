"""Real PostgreSQL baseline/capture fencing and immutable delivery boundaries."""

import pytest
import pytest_asyncio

from test_postgres_execution_store import database
from test_execution_controller import prepare_reply
from test_parked_stop import quiescent
from agentscope.message import TextBlock
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.transport.delivery import DeliveryStore, DeliveryConflict
from workmate_runtime.transport.postgres import ExecutionOwner, ExecutionStore


BASELINE={'version':1,'files':{},'presentation_id':None}
CAPTURE={'version':1,'source':'snapshot','declared_files':[],'warnings':[],'files':[]}


@pytest_asyncio.fixture
async def delivery_db(database):
    pool,db=database
    yield pool,db


async def complete(pool,db,*,sealed=True):
    prepare,_=prepare_reply([[TextBlock(text='done')]])
    async def wrapped(owner,snapshot,guard):
        async with pool.acquire() as conn:
            await DeliveryStore(conn).record_baseline(owner,'node',BASELINE)
        return await prepare(owner,snapshot,guard)
    async def seal(owner,reply_id,status,snapshot):
        async with pool.acquire() as conn:
            return await DeliveryStore(conn).record_seal(owner,'node',reply_id=reply_id,
                status=status,snapshot=snapshot,payload=CAPTURE)
    return await RunningExecutionController(pool,verify_cleanup=quiescent,
        seal_terminal=seal if sealed else None).start('command','worker','node',wrapped)


async def test_capture_requires_baseline_and_terminal_resource_evidence(delivery_db):
    pool,db=delivery_db
    result=await complete(pool,db)
    store=DeliveryStore(db)
    first=await store.record_capture(result.owner,'node',CAPTURE)
    assert first==await store.record_capture(result.owner,'node',CAPTURE)
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==1
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'


@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_sessions SET lease_until=now()-interval '1 second'",
    "UPDATE runtime_sessions SET owner_id='other'",
    "UPDATE runtime_sessions SET storage_node_id='other'",
    "UPDATE runtime_task_executions SET execution_epoch=2",
    "UPDATE conversations SET active_product_task_id=NULL",
    "UPDATE runtime_events SET payload=payload-'resource_cleanup' WHERE payload->>'type'='EXECUTION_BOUNDARY'",
])
async def test_capture_rejects_stale_or_unproven_execution(delivery_db,mutation):
    pool,db=delivery_db
    result=await complete(pool,db)
    await db.execute(mutation)
    with pytest.raises(DeliveryConflict):
        await DeliveryStore(db).record_capture(result.owner,'node',CAPTURE)
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0


async def test_baseline_cannot_be_recorded_after_reply_started(delivery_db):
    pool,db=delivery_db
    owner=await ExecutionStore(db).claim_start('command','worker','node',lease_seconds=30)
    await ExecutionStore(db).begin_reply(owner,'reply',{'type':'REPLY_START','reply_id':'reply'})
    with pytest.raises(DeliveryConflict):
        await DeliveryStore(db).record_baseline(owner,'node',BASELINE)


async def test_conflicting_retry_does_not_replace_capture(delivery_db):
    pool,db=delivery_db
    result=await complete(pool,db)
    store=DeliveryStore(db)
    await store.record_capture(result.owner,'node',CAPTURE)
    with pytest.raises(DeliveryConflict):
        await store.record_capture(result.owner,'node',{**CAPTURE,'warnings':['different']})


@pytest.mark.parametrize('table',['runtime_delivery_baselines','runtime_delivery_captures'])
async def test_database_rejects_delivery_record_mutation(delivery_db,table):
    pool,db=delivery_db
    result=await complete(pool,db)
    await DeliveryStore(db).record_capture(result.owner,'node',CAPTURE)
    import asyncpg
    with pytest.raises(asyncpg.RaiseError):
        await db.execute(f"UPDATE {table} SET digest=repeat('0',64)")
    with pytest.raises(asyncpg.RaiseError):
        await db.execute(f'DELETE FROM {table}')


async def test_real_core_delivery_coordinator_captures_immutable_bytes(delivery_db,tmp_path):
    from types import SimpleNamespace
    from workmate_runtime.delivery.coordinator import DeliveryCoordinator
    from workmate_runtime.execution.resources import TaskResourceRegistry
    pool,db=delivery_db
    workspace=tmp_path/'workspace'
    (workspace/'output').mkdir(parents=True)
    objects=tmp_path/'artifacts'
    registry=TaskResourceRegistry()
    coordinator=DeliveryCoordinator(pool,'node',objects,registry,max_file_bytes=100,max_files=20)
    prepare,_=prepare_reply([[TextBlock(text='done')]])
    async def wrapped(owner,snapshot,guard):
        registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
        binding=SimpleNamespace(owner=owner,workspace=SimpleNamespace(host_workdir=workspace))
        await coordinator.prepare(owner,binding)
        (workspace/'output/result.txt').write_bytes(b'first Task')
        return await prepare(owner,snapshot,guard)
    result=await RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup,
        seal_terminal=coordinator.seal_terminal).start('command','worker','node',wrapped)
    digest=await coordinator.capture(result)
    import json
    payload=json.loads(await db.fetchval('SELECT payload FROM runtime_delivery_captures'))
    assert digest==await db.fetchval('SELECT digest FROM runtime_delivery_captures')
    (workspace/'output/result.txt').write_bytes(b'later Task')
    assert (objects/payload['files'][0]['storage_key']).read_bytes()==b'first Task'
    assert await coordinator.capture(result)==digest
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'


async def test_seal_cancellation_joins_file_io_and_never_publishes_terminal_record(delivery_db,tmp_path,monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    from workmate_runtime.delivery.coordinator import DeliveryCoordinator
    from workmate_runtime.delivery.files import DeliveryFiles
    from workmate_runtime.execution.resources import TaskResourceRegistry
    pool,db=delivery_db
    workspace=tmp_path/'workspace'
    (workspace/'output').mkdir(parents=True)
    registry=TaskResourceRegistry()
    coordinator=DeliveryCoordinator(pool,'node',tmp_path/'artifacts',registry,max_file_bytes=100,max_files=20)
    prepare,_=prepare_reply([[TextBlock(text='done')]])
    async def wrapped(owner,snapshot,guard):
        registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
        await coordinator.prepare(owner,SimpleNamespace(owner=owner,workspace=SimpleNamespace(host_workdir=workspace)))
        return await prepare(owner,snapshot,guard)
    entered,release=asyncio.Event(),threading.Event()
    loop=asyncio.get_running_loop()
    original=DeliveryFiles.capture
    def delayed(self,*args,**kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(DeliveryFiles,'capture',delayed)
    operation=asyncio.create_task(RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup,
        seal_terminal=coordinator.seal_terminal).start('command','worker','node',wrapped))
    try:
        await asyncio.wait_for(entered.wait(),3)
        operation.cancel()
        done,_=await asyncio.wait([operation],timeout=.05)
        assert not done
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0

    assert await db.fetchval('SELECT count(*) FROM runtime_delivery_seals')==0
    assert await db.fetchval('SELECT count(*) FROM runtime_snapshots')==0
