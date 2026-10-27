"""Paused real S3 with production checkpoint/seal coordinators and real PG."""
import os
from types import SimpleNamespace

import boto3
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError
import pytest

from test_postgres_execution_store import database
from test_worker_delivery_store import delivery_db
from test_resume_controller import parked
from test_suspension_controller import controller
from test_execution_controller import prepare_reply
from agentscope.message import TextBlock
from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
from workmate_runtime.delivery.coordinator import DeliveryCoordinator
from workmate_runtime.execution.controller import ExecutionAborted
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_product.artifact_storage import S3ArtifactStore


def assert_s3_timeout(error):
    seen=set()
    while error is not None and id(error) not in seen:
        if isinstance(error, ReadTimeoutError): return
        seen.add(id(error)); error=error.__cause__ or error.__context__
    pytest.fail('Expected actual paused S3 read timeout in exception chain')


def real_store():
    endpoint=os.environ.get('FAULT_TEST_S3_ENDPOINT')
    if not endpoint: pytest.skip('dedicated paused S3 endpoint required')
    return S3ArtifactStore(boto3.client('s3',endpoint_url=endpoint,
        aws_access_key_id='fault-fixture',aws_secret_access_key='fault-fixture-secret',
        config=Config(connect_timeout=1,read_timeout=1,retries={'max_attempts':2})), 'fault-artifacts')


async def test_paused_s3_required_checkpoint_blocks_release_and_keeps_owner(database,tmp_path):
    pool,db=database
    first=await parked(pool,db)
    root=tmp_path/'workspace'; (root/'working').mkdir(parents=True)
    (root/'working/durable.txt').write_text('must checkpoint before release')
    coordinator=CheckpointCoordinator(pool,'node',real_store())
    control,assembly,delivery=controller(pool,first.owner)
    delivery.checkpoints=coordinator
    async def suspend(owner, snapshot, *, checkpoint, **kwargs):
        # The actual coordinator and PG publication path are exercised. The
        # workspace lifecycle is a fixture; no production provider is claimed.
        return await checkpoint(owner,root,boundary='approval')
    assembly.workspaces.suspend_task_workspace.side_effect=suspend
    with pytest.raises(Exception) as caught:
        await control.try_suspend(first.owner,'node')
    assert_s3_timeout(caught.value)
    assert await db.fetchval('SELECT count(*) FROM workspace_checkpoints')==0
    assert await db.fetchval('SELECT status FROM runtime_suspensions')=='blocked'
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions')==first.owner.worker_id
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')==first.owner.task_id
    assert first.owner in delivery._bindings and first.owner in delivery._checkpoint_workspaces


async def test_paused_s3_terminal_seal_has_no_success_metadata(delivery_db,tmp_path):
    pool,db=delivery_db
    root=tmp_path/'workspace'; (root/'output').mkdir(parents=True)
    registry=TaskResourceRegistry()
    delivery=DeliveryCoordinator(pool,'node',tmp_path/'objects',registry,
        max_file_bytes=1024,max_files=20,object_store=real_store())
    prepare,_=prepare_reply([[TextBlock(text='finished')]])
    owners=[]
    async def prepared(owner,snapshot,guard):
        owners.append(owner)
        registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
        await delivery.prepare(owner,SimpleNamespace(owner=owner,workspace=SimpleNamespace(host_workdir=root)))
        (root/'output/result.txt').write_text('must publish before terminal commit')
        return await prepare(owner,snapshot,guard)
    with pytest.raises(ExecutionAborted) as caught:
        await RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup,
            seal_terminal=delivery.seal_terminal).start('command','worker','node',prepared)
    assert_s3_timeout(caught.value)
    assert await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='REPLY_END'")==1
    for table in ('runtime_delivery_seals','runtime_delivery_captures','runtime_snapshots'):
        assert await db.fetchval('SELECT count(*) FROM '+table)==0
    assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'")=='claimed'
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions')==owners[0].worker_id
    assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')==owners[0].task_id
