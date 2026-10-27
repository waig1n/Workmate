"""Actual Core Start/HITL/Resume/Stop through Nginx and two Gateway processes.

The model and workspace attestation are fixtures, not OS isolation/provider tests.
No terminal events, ownership release or approval snapshots are manufactured.
"""
import asyncio
import json
from functools import wraps
import os
import shutil
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from agentscope.message import TextBlock, ToolCallBlock
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from test_durable_projection import projection_database  # noqa: F401
from test_native_admission import admission_database  # noqa: F401
from test_native_agents import native_agents  # noqa: F401
from test_dual_gateway_http import Gateway
from gateway_lb import Nginx
from test_core_engine import ScriptModel
from test_worker_workspaces import StubWorkspace
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from workmate_product.coordination.command_queue import RedisCommandQueue
from workmate_product.realtime.composition import create_realtime_log


async def test_actual_worker_approval_resume_and_stop_through_lb(admission_database, native_agents, tmp_path, monkeypatch):
    if not os.getenv('WORKMATE_TEST_REDIS_URL'):
        pytest.skip('requires isolated Redis')
    if not (os.getenv('WORKMATE_TEST_NGINX_IMAGE') or os.getenv('WORKMATE_TEST_NGINX') or shutil.which('nginx')):
        pytest.skip('requires real Nginx')
    sessions, admin = admission_database
    # projection_database seeds an unfinished scripted task for projection tests.
    # Remove that unrelated fixture before any role starts; all scenario tasks
    # below are admitted only by HTTP and retain their real ownership lifecycle.
    await admin.execute("DELETE FROM runtime_task_executions WHERE task_id='t'; "
        "DELETE FROM runtime_sessions WHERE id='s'; "
        "UPDATE conversations SET active_product_task_id=NULL WHERE id='c'; "
        "UPDATE tasks SET status='cancelled' WHERE id='t'")
    schema = await admin.fetchval('SELECT current_schema()')
    namespace = 'lb-business-' + uuid4().hex
    redis = Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'], decode_responses=True,
        retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        '  models:\n    default: test\n    profiles:\n      test:\n        type: openai\n'
        '        model: fixture-model\n        api_key_env: FIXTURE_MODEL_KEY\n'
        'command_queue:\n  poll_interval_seconds: 0.05\n')
    monkeypatch.setenv('FIXTURE_MODEL_KEY', 'local-fixture-only')
    responses = iter([[ToolCallBlock(id='lb-write', name='Write', input=json.dumps({
        'file_path':'/workspace/output/result.txt', 'content':'approved exactly once'}))],
        [TextBlock(text='Approved file saved')]])
    from workmate_runtime.workspaces import WorkmateSandboxWrite
    original_write = WorkmateSandboxWrite.call
    writes = []
    @wraps(original_write)
    async def observed_write(self, **kwargs):
        writes.append(kwargs)
        return await original_write(self, **kwargs)
    monkeypatch.setattr(WorkmateSandboxWrite, 'call', observed_write)
    blocked = asyncio.Event()
    stopping = False

    class BusinessModel(ScriptModel):
        async def _call_api(self, *args, **kwargs):
            if stopping:
                blocked.set()
                await asyncio.Event().wait()
            self.responses = iter([next(responses)])
            return await super()._call_api(*args, **kwargs)

    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model', lambda *_: BusinessModel([]))
    async def no_attestation(*_):
        return None  # Strict profile; does not grant Full Access.
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox', no_attestation)
    settings = RuntimeSettings(reme_enabled=False, coordination_redis_url=os.environ['WORKMATE_TEST_REDIS_URL'],
        coordination_namespace=namespace)
    worker = compose_worker(sessions.kw['bind'], settings, storage_node_id='node',
        workspace_dir=tmp_path/'workspaces', artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024, max_files=3, workspace_factory=StubWorkspace, drain_seconds=0,
        command_queue=RedisCommandQueue(redis, namespace=namespace),
        realtime_log=create_realtime_log(redis, namespace=namespace, policy=settings.realtime))
    gateways = [Gateway(tmp_path, schema, namespace, config, index) for index in range(2)]
    shutdown = asyncio.Event()
    running = None
    proxy = None
    headers = {'Authorization':'Bearer '+jwt.encode({'sub':'u','ver':0,'iat':int(time.time()),
        'exp':int(time.time())+600}, 'local-dual-gateway-signing-key-0123456789', algorithm='HS256')}

    async def wait_status(task_id, wanted):
        async with asyncio.timeout(25):
            while True:
                assert not worker.service.failures, worker.service.failures
                assert running is None or not running.done(), 'Worker exited'
                status = await admin.fetchval('SELECT status FROM tasks WHERE id=$1', task_id)
                if status == wanted:
                    return
                assert status not in {'failed','quarantined'}, status
                await asyncio.sleep(.05)

    try:
        for gateway in gateways:
            await gateway.start()
        proxy = Nginx(tmp_path, [urlsplit(gateway.url).port for gateway in gateways])
        await proxy.start()
        running = asyncio.create_task(worker.serve(shutdown))
        async with httpx.AsyncClient(base_url=proxy.url, headers=headers, timeout=10) as client:
            created = await client.post('/api/tasks/conversations/new/tasks',
                json={'message':'Write approved fixture file', 'approval_profile':'strict'})
            assert created.status_code == 202, created.text
            task_id = created.json()['task']['id']
            start_upstream = created.headers['x-acceptance-upstream']
            await wait_status(task_id, 'awaiting_permission')
            original_reply = await admin.fetchval('SELECT run_id FROM tasks WHERE id=$1', task_id)
            assert not list((tmp_path/'workspaces').rglob('result.txt'))
            pending = await client.get(f'/api/tasks/{task_id}/pending-confirm')
            assert pending.status_code == 200, pending.text
            card = pending.json()
            # Advance the real round-robin peer so Resume is served by the
            # other process; no routing override or direct upstream request.
            assert (await client.get('/health')).status_code == 200
            confirmed = await client.post(f'/api/tasks/{task_id}/confirm', json={
                'decision_version':card['decisionVersion'],
                'decisions':[{'tool_call_id':'lb-write', 'approved':True}]})
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.headers['x-acceptance-upstream'] != start_upstream
            await wait_status(task_id, 'completed')
            assert await admin.fetchval('SELECT run_id FROM tasks WHERE id=$1', task_id) == original_reply
            files = list((tmp_path/'workspaces').rglob('result.txt'))
            assert len(writes) == 1
            assert len(files) == 1 and files[0].read_text() == 'approved exactly once'
            kinds = dict(await admin.fetch('SELECT kind,count(*) FROM runtime_commands WHERE task_id=$1 GROUP BY kind', task_id))
            assert kinds == {'StartTask':1, 'ResumeInteraction':1}
            stopping = True
            created = await client.post('/api/tasks/conversations/new/tasks',
                json={'message':'Wait until stopped', 'approval_profile':'standard'})
            assert created.status_code == 202, created.text
            stop_id = created.json()['task']['id']
            await asyncio.wait_for(blocked.wait(), 20)
            stopped = await client.post(f'/api/tasks/{stop_id}/stop')
            assert stopped.status_code == 200, stopped.text
            await wait_status(stop_id, 'cancelled')
            assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id=$1 AND kind='StopTask'", stop_id) == 1
            identities = {start_upstream, pending.headers['x-acceptance-upstream'],
                confirmed.headers['x-acceptance-upstream'], stopped.headers['x-acceptance-upstream']}
            assert len(identities) == 2
            assert files[0].read_text() == 'approved exactly once'
    finally:
        shutdown.set()
        try:
            if running:
                await asyncio.wait_for(running, 15)
        finally:
            if proxy:
                await proxy.close()
            await asyncio.gather(*(gateway.close() for gateway in gateways))
            keys = [key async for key in redis.scan_iter(match=f'workmate:{namespace}:*')]
            if keys:
                await redis.delete(*keys)
            await redis.aclose()
