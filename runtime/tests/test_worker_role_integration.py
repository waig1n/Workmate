"""Actual role lifespan through PostgreSQL claims/Core reply/delivery/shutdown.

Only the model and OS sandbox are controlled fixtures; Product schema, Worker
composition, durable transport, controller and role readiness run unchanged.
"""
import asyncio
import json
import os
import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient,ASGITransport
from agentscope.message import TextBlock
from test_execution_context import database,record
from test_worker_workspaces import StubWorkspace
from test_core_engine import ScriptModel
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.roles.settings import WorkerRoleSettings



@pytest_asyncio.fixture
async def stream_runtime(database):
    from redis.asyncio import Redis
    from workmate_product.config.command_queue import CommandQueuePolicy
    from workmate_product.coordination.command_queue import RedisCommandQueue
    from workmate_product.coordination.command_publisher import CommandPublisher
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires isolated local Redis for the production Worker role')
    namespace='role-'+uuid.uuid4().hex
    client=Redis.from_url(url)
    queue=RedisCommandQueue(client,namespace=namespace)
    sessions,_=database
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy(poll_interval_seconds=.05))
    shutdown=asyncio.Event()
    running=asyncio.create_task(publisher.serve(shutdown))
    try:
        yield RuntimeSettings(reme_enabled=False,coordination_redis_url=url,
            coordination_namespace=namespace)
    finally:
        shutdown.set()
        await asyncio.wait_for(running,5)
        keys=[key async for key in client.scan_iter(match=f'workmate:{namespace}:*')]
        if keys: await client.delete(*keys)
        await client.aclose()


async def _stage_record(sessions):
    from workmate_product.persistence.repositories.command_deliveries import stage
    await record(sessions)
    async with sessions.begin() as db:
        await stage(db,'cmd')

def _stub_os_sandbox(monkeypatch, worker):
    from workmate_runtime.sandbox.factory import create_sandbox_provider
    from workmate_runtime.sandbox.bubblewrap import BubblewrapSandboxProvider

    def provider(settings):
        assert settings.provider == 'bubblewrap'
        return create_sandbox_provider(settings, workspace_factory=StubWorkspace)

    async def probe(self):
        assert type(self) is BubblewrapSandboxProvider

    async def verify(self, backend):
        # Only OS namespace execution is stubbed; production ownership checks,
        # preparing-state/lease fencing and persisted proof remain exercised.
        assert backend._execution_owner is not None
        assert backend._task_resources is not None
        return None

    monkeypatch.setattr(worker, 'create_sandbox_provider', provider)
    monkeypatch.setattr(BubblewrapSandboxProvider, 'probe', probe)
    monkeypatch.setattr(BubblewrapSandboxProvider, 'verify_task_isolation', verify)


async def test_real_worker_role_claims_core_reply_and_joins_shutdown(database,tmp_path,monkeypatch,stream_runtime):
    from workmate_runtime.roles import worker
    from workmate_runtime.execution.composition import compose_worker
    sessions,admin=database
    engine=sessions.kw['bind']
    await _stage_record(sessions)
    settings=WorkerRoleSettings(runtime=stream_runtime,
        database_url=engine.url.render_as_string(hide_password=False),storage_node_id='node',
        workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts')
    assert settings.runtime.knowledge.enabled is False
    monkeypatch.setattr(worker,'create_async_engine',lambda *args,**kwargs:engine)
    _stub_os_sandbox(monkeypatch, worker)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',
        lambda *_:ScriptModel([[TextBlock(text='role-owned reply')]]))
    web=worker.create_worker_app(settings)
    async with AsyncClient(transport=ASGITransport(app=web),base_url='http://worker') as client:
        assert (await client.get('/health')).status_code==503

        async with web.router.lifespan_context(web):
            async with asyncio.timeout(15):
                while not await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures'):
                    assert not web.state.worker.service.failures,web.state.worker.service.failures
                    await asyncio.sleep(.02)
            assert (await client.get('/health')).status_code==200
            assert await admin.fetchval('SELECT count(*) FROM runtime_sandbox_attestations') == 1
            assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='cmd'")=='completed'
            snapshot=json.loads(await admin.fetchval('SELECT payload FROM runtime_snapshots'))
            assert 'role-owned reply' in json.dumps(snapshot)
            # Product consumer/delivery release is a separate role concern.
            assert await admin.fetchval("SELECT active_product_task_id FROM conversations WHERE id='c'")=='t'
            assert await admin.fetchval("SELECT active_task_id FROM runtime_sessions WHERE id='s'")=='t'
        assert web.state.running.done()
        assert not web.state.worker.service.ready.is_set()
        assert not web.state.worker.service.failures
        assert (await client.get('/health')).status_code==503


async def test_new_worker_role_recovers_terminal_delivery_without_replaying_core(database,tmp_path,monkeypatch,stream_runtime):
    from workmate_runtime.roles import worker
    from workmate_runtime.execution.composition import compose_worker
    sessions,admin=database
    engine=sessions.kw['bind']
    await _stage_record(sessions)
    settings=WorkerRoleSettings(runtime=stream_runtime,
        database_url=engine.url.render_as_string(hide_password=False),storage_node_id='node',
        workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts')
    monkeypatch.setattr(worker,'create_async_engine',lambda *args,**kwargs:engine)
    _stub_os_sandbox(monkeypatch, worker)
    models=[]
    def model(*args):
        models.append(True)
        return ScriptModel([[TextBlock(text='must execute only once')]])
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',model)
    applications=[]
    def compose(*args,**kwargs):
        from dataclasses import replace
        app=compose_worker(*args,**kwargs)
        if not applications:
            async def crash_after_terminal(result):
                raise RuntimeError('simulated exit before capture')
            app.service.bindings=replace(app.service.bindings,finalize_result=crash_after_terminal)
        applications.append(app)
        return app
    monkeypatch.setattr(worker,'compose_worker',compose)
    first=worker.create_worker_app(settings)
    async with first.router.lifespan_context(first):
        async with asyncio.timeout(15):
            while not first.state.worker.service.failures:
                await asyncio.sleep(.02)
        assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_seals')==1
        assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures')==0
    owner=await admin.fetchval('SELECT owner_id FROM runtime_sessions')
    await admin.execute("UPDATE runtime_sessions SET lease_until=now()-interval '1 minute'")
    second=worker.create_worker_app(settings)
    async with second.router.lifespan_context(second):
        assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures')==1
        assert await admin.fetchval('SELECT owner_id FROM runtime_sessions')==owner
        assert second.state.worker.service.worker_id!=owner
        assert await admin.fetchval('SELECT execution_epoch FROM runtime_sessions')==1
        assert not second.state.worker.service.failures
        assert models==[True]


async def test_role_requires_redis_before_any_command_intake(database,tmp_path,monkeypatch):
    from workmate_runtime.roles import worker
    sessions,_=database;engine=sessions.kw['bind']
    settings=WorkerRoleSettings(runtime=RuntimeSettings(reme_enabled=False),
        database_url=engine.url.render_as_string(hide_password=False),storage_node_id='node',
        workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts')
    monkeypatch.setattr(worker,'create_async_engine',lambda *args,**kwargs:engine)
    _stub_os_sandbox(monkeypatch,worker)
    def forbidden(*args,**kwargs): raise AssertionError('must not compose execution without Redis')
    monkeypatch.setattr(worker,'compose_worker',forbidden)
    with pytest.raises(RuntimeError,match='WORKMATE_COORDINATION_REDIS_URL is required'):
        await worker.serve_worker(settings,asyncio.Event())
