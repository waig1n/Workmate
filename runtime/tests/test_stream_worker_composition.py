"""Real Redis + PG + composed Runtime/Product approval paths, locally isolated."""
import asyncio
import os
import uuid
import pytest
from test_execution_context import database


def fixture_file_backend(monkeypatch):
    """All Write I/O is local fixture work; never accidentally launch bwrap."""
    from pathlib import Path
    from types import SimpleNamespace
    from workmate_runtime.workspaces import WorkmateClearedEnvBubblewrapBackend
    def target(backend,path):
        relative=Path(path).relative_to('/workspace')
        if '..' in relative.parts:
            raise AssertionError('Fixture path escapes workspace')
        return Path(backend._host_workdir)/relative
    async def exists(backend,path):
        return target(backend,path).exists()
    async def write(backend,path,data):
        destination=target(backend,path)
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(data)
    async def execute(backend,command,**kwargs):
        if not isinstance(command,list) or len(command)!=3 or command[:2]!=['mkdir','-p']:
            raise AssertionError('Unexpected shell execution in file fixture')
        target(backend,command[2]).mkdir(parents=True,exist_ok=True)
        return SimpleNamespace(exit_code=0,stdout=b'',stderr=b'')
    monkeypatch.setattr(WorkmateClearedEnvBubblewrapBackend,'file_exists',exists)
    monkeypatch.setattr(WorkmateClearedEnvBubblewrapBackend,'write_file',write)
    monkeypatch.setattr(WorkmateClearedEnvBubblewrapBackend,'exec_shell',execute)


@pytest.mark.parametrize('action',['resume','revoked_stop','revoked_resume'])
async def test_real_streams_publisher_composition_approval_and_stop(database,tmp_path,monkeypatch,action):
    from redis.asyncio import Redis
    from workmate_product.coordination.command_queue import RedisCommandQueue
    from workmate_product.coordination.command_publisher import CommandPublisher
    from workmate_product.config.command_queue import CommandQueuePolicy
    import workmate_runtime.execution.composition as composition
    from test_worker_composition import test_composed_permission_control_and_product_background_delivery as scenario
    # This suite verifies queue/lifecycle composition, not OS namespace support.
    # File mutation is an explicit fixture; all PG/Redis/Core paths stay real.
    from workmate_runtime.sandbox.bubblewrap import BubblewrapSandboxProvider
    verify_isolation=BubblewrapSandboxProvider.verify_task_isolation
    real_sandbox=os.getenv('WORKMATE_TEST_REAL_SANDBOX')=='1'
    if not real_sandbox:
        fixture_file_backend(monkeypatch)
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url: pytest.skip('requires isolated local Redis')
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    client=Redis.from_url(url,decode_responses=True,retry=Retry(NoBackoff(),0))
    queue=RedisCommandQueue(client,namespace='test-'+uuid.uuid4().hex)
    sessions,admin=database
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.composition import create_realtime_log
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_product.realtime.business_publisher import BusinessPublisher
    realtime_log=create_realtime_log(client,namespace=queue.namespace,policy=RealtimePolicy())
    manager=EventLogManager(sessions,realtime_log)
    # Emit real Core display events; final-only fixtures do not exercise SSE.
    import test_worker_composition as scenario_module
    from agentscope.model import ChatResponse
    original_model=scenario_module.ScriptModel
    class StreamingModel(original_model):
        async def _call_api(self,*args,**kwargs):
            source=await super()._call_api(*args,**kwargs)
            async def chunks():
                async for chunk in source:
                    yield ChatResponse(content=chunk.content,is_last=False)
                    yield chunk
            return chunks()
    monkeypatch.setattr(scenario_module,'ScriptModel',StreamingModel)
    policy=CommandQueuePolicy(poll_interval_seconds=.05)
    publisher=CommandPublisher(sessions,queue,policy)
    original=composition.compose_worker
    def compose(*args,**kwargs):
        if real_sandbox:
            from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
            kwargs['workspace_factory']=WorkerMCPWorkspace
            monkeypatch.setattr(BubblewrapSandboxProvider,'verify_task_isolation',verify_isolation)
        app=original(*args,**kwargs,command_queue=queue,realtime_log=realtime_log)
        if action=='revoked_resume':
            # Exercise the live owner's queue path; cold recovery has separate
            # authority tests and may safely decline without a live supervisor.
            from dataclasses import replace
            app.service.bindings=replace(app.service.bindings,suspend=None)
        return app
    monkeypatch.setattr(composition,'compose_worker',compose)
    shutdown=asyncio.Event()
    publishing=asyncio.create_task(publisher.serve(shutdown))
    business=asyncio.create_task(BusinessPublisher(sessions,manager,realtime_log,policy).serve(shutdown))
    try:
        await scenario(database,tmp_path,monkeypatch,action,None)
        async with asyncio.timeout(10):
            while await admin.fetchval("SELECT count(*) FROM realtime_business_outbox WHERE status<>'published'"):
                assert not business.done(), business.exception() if business.done() else None
                await asyncio.sleep(.02)
        from workmate_contracts.realtime import RealtimeCursor
        ref=await manager.ensure('c')
        page=await realtime_log.read(ref,RealtimeCursor(conversation_id='c',generation=ref.generation,stream_id='0-0'))
        payloads=[entry.envelope.payload for entry in page.entries]
        assert any(p.get('name')=='HITL_APPROVAL_REQUEST' for p in payloads)
        assert any(p.get('type')=='TOOL_CALL_ARGS' for p in payloads)
        assert any(p.get('type')=='STEP_STARTED' for p in payloads)
        assert any(p.get('type')=='STEP_FINISHED' for p in payloads)
        if action=='resume':
            assert any(p.get('type')=='TOOL_CALL_RESULT' for p in payloads)
            assert any(p.get('type')=='RUN_FINISHED' for p in payloads)
            assert any(p.get('name')=='ARTIFACT_CREATED' for p in payloads)
            assert any(entry.envelope.text is not None for entry in page.entries)
            assert await admin.fetchval("SELECT snapshot_phase FROM task_messages WHERE snapshot_version>0")=='final'
        runtime_types=await admin.fetch("SELECT payload->>'type' AS type FROM runtime_events")
        assert not any(row['type'].startswith(('TEXT_BLOCK_','THINKING_BLOCK_')) for row in runtime_types)
        product_types=await admin.fetch("SELECT payload->>'type' AS type FROM product_events")
        assert not any(row['type'].startswith(('TEXT_MESSAGE_','REASONING_MESSAGE_')) for row in product_types)
        if action=='revoked_resume':
            _,admin=database
            assert await admin.fetchval("SELECT status FROM command_deliveries WHERE command_id='resume'")=='quarantined'
            assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='accepted'
            rows=await admin.fetch("SELECT target_incarnation FROM command_deliveries WHERE command_id='resume'")
            from workmate_product.coordination.command_queue import QueueRoute
            route=QueueRoute('node',rows[0]['target_incarnation'],True)
            assert await queue.pending(route)==[]
    finally:
        shutdown.set()
        await asyncio.wait_for(asyncio.gather(publishing,business),5)
        keys=[key async for key in client.scan_iter(match=f'workmate:{queue.namespace}:*')]
        if keys: await client.delete(*keys)
        await client.aclose()


@pytest.mark.parametrize('action',['resume','full_access','partial_resume','revoked_stop'])
async def test_real_streams_cold_restart_preserves_task_reply_files(database,tmp_path,monkeypatch,action):
    from redis.asyncio import Redis
    from workmate_product.coordination.command_queue import RedisCommandQueue
    from workmate_product.coordination.command_publisher import CommandPublisher
    from workmate_product.config.command_queue import CommandQueuePolicy
    import test_cold_worker_composition as cold
    from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
    from workmate_runtime.sandbox.bubblewrap import BubblewrapSandboxProvider
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url: pytest.skip('requires isolated local Redis')
    real_sandbox=os.getenv('WORKMATE_TEST_REAL_SANDBOX')=='1'
    if not real_sandbox:
        fixture_file_backend(monkeypatch)
    verify=BubblewrapSandboxProvider.verify_task_isolation
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    client=Redis.from_url(url,decode_responses=True,retry=Retry(NoBackoff(),0))
    queue=RedisCommandQueue(client,namespace='test-'+uuid.uuid4().hex)
    sessions,_=database
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy(poll_interval_seconds=.05))
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.composition import create_realtime_log
    realtime_log=create_realtime_log(client,namespace=queue.namespace,policy=RealtimePolicy())
    from agentscope.model import ChatResponse
    original_model=cold.ScriptModel
    class StreamingModel(original_model):
        async def _call_api(self,*args,**kwargs):
            source=await super()._call_api(*args,**kwargs)
            async def chunks():
                async for chunk in source:
                    yield ChatResponse(content=chunk.content,is_last=False)
                    yield chunk
            return chunks()
    monkeypatch.setattr(cold,'ScriptModel',StreamingModel)
    original=cold.compose_worker
    def compose(*args,**kwargs):
        if real_sandbox:
            kwargs['workspace_factory']=WorkerMCPWorkspace
            monkeypatch.setattr(BubblewrapSandboxProvider,'verify_task_isolation',verify)
        kwargs['realtime_log'] = realtime_log
        return original(*args,**kwargs,command_queue=queue)
    monkeypatch.setattr(cold,'compose_worker',compose)
    shutdown=asyncio.Event()
    publishing=asyncio.create_task(publisher.serve(shutdown))
    try:
        await cold.test_composed_cold_control_keeps_original_reply_and_delivery_baseline(
            database,tmp_path,monkeypatch,action,None)
    finally:
        shutdown.set()
        await asyncio.wait_for(publishing,5)
        keys=[key async for key in client.scan_iter(match=f'workmate:{queue.namespace}:*')]
        if keys: await client.delete(*keys)
        await client.aclose()
