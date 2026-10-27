"""Worker role checks configuration without importing old role or executing work."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from test_execution_context import database


@pytest.fixture
def command_transport_fixture(monkeypatch):
    """These lifecycle unit tests stub I/O; role integration uses real Redis."""
    from contextlib import asynccontextmanager
    from redis.asyncio import Redis
    original = Redis.from_url
    @asynccontextmanager
    async def client(*args,**kwargs):
        # Redis construction is lazy: preserve retry/pool protocol without I/O.
        async with original(*args,**kwargs) as transport:
            yield transport
    @asynccontextmanager
    async def notifications(*args,**kwargs):
        yield None
    monkeypatch.setattr('redis.asyncio.Redis.from_url',client)
    monkeypatch.setattr('workmate_product.coordination.WakeupBus',notifications)


@pytest.mark.parametrize('valid',[True,False])
def test_worker_check_config_is_read_only_and_redacts_secrets(tmp_path,valid):
    root=Path(__file__).resolve().parents[2]
    config=tmp_path/'workmate.yaml'
    config.write_text('version: 1\nworkmate_runtime:\n  memory:\n    enabled: false\n')
    env={'PATH':os.environ.get('PATH',''),'PYTHONPATH':os.pathsep.join(str(root/p) for p in
        ('runtime','packages/contracts/src','packages/product/src')),
        'WORKMATE_CONFIG_FILE':str(config),'DATABASE_URL':'postgresql+asyncpg://test:do-not-print@localhost/test',
        'WORKMATE_WORKER_STORAGE_NODE_ID':'node-a' if valid else 'invalid/node',
        'WORKMATE_WORKSPACE_BASEDIR':str(tmp_path/'workspaces'),'ARTIFACT_STORAGE_DIR':str(tmp_path/'artifacts')}
    result=subprocess.run([sys.executable,'-m','workmate_runtime.roles.worker','--check-config'],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==(0 if valid else 1),result.stderr
    assert 'do-not-print' not in result.stdout+result.stderr
    assert not (tmp_path/'workspaces').exists() and not (tmp_path/'artifacts').exists()


@pytest.mark.parametrize("missing_table", ["runtime_delivery_captures", "runtime_suspensions", "command_deliveries", "command_delivery_quarantines", "workspace_residencies", "workspace_release_requests", "conversation_event_logs", "realtime_business_outbox"])
async def test_worker_schema_check_requires_every_feature_table(database, missing_table):
    from workmate_runtime.roles.worker import verify_product_schema
    sessions,admin=database
    engine=sessions.kw['bind']
    await verify_product_schema(engine)
    await admin.execute(f'ALTER TABLE {missing_table} RENAME TO unavailable_feature')
    with pytest.raises(RuntimeError,match='schema is not ready'):
        await verify_product_schema(engine)


async def test_worker_schema_check_requires_display_snapshot_fields(database):
    from workmate_runtime.roles.worker import verify_product_schema
    from sqlalchemy.exc import DBAPIError
    sessions,admin=database
    await admin.execute('ALTER TABLE task_messages DROP COLUMN covered_cursor')
    with pytest.raises(DBAPIError):
        await verify_product_schema(sessions.kw['bind'])


async def test_worker_health_tracks_lifecycle_and_joins_shutdown(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from httpx import AsyncClient,ASGITransport
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles.worker import create_worker_app
    exited=asyncio.Event()
    service=SimpleNamespace(ready=asyncio.Event(),active_sessions={'one'},failures={'blocked':'reason'})
    async def run(settings,shutdown,*,on_ready):
        service.ready.set()
        on_ready(SimpleNamespace(service=service))
        try:
            await shutdown.wait()
        finally:
            service.ready.clear()
            exited.set()
    monkeypatch.setattr('workmate_runtime.roles.worker.serve_worker',run)
    web=create_worker_app(SimpleNamespace(runtime=RuntimeSettings()))
    async with AsyncClient(transport=ASGITransport(app=web),base_url='http://worker') as client:
        assert (await client.get('/health')).status_code==503
        async with web.router.lifespan_context(web):
            response=await client.get('/health')
            assert response.status_code==200
            assert response.json()=={'role':'worker','ready':True,'active_sessions':1,'blocked_sessions':1}
        assert exited.is_set()
        assert (await client.get('/health')).status_code==503


async def test_worker_health_fails_when_background_worker_dies(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from httpx import AsyncClient,ASGITransport
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles.worker import create_worker_app
    fail=asyncio.Event()
    service=SimpleNamespace(ready=asyncio.Event(),active_sessions=set(),failures={})
    async def run(settings,shutdown,*,on_ready):
        service.ready.set()
        on_ready(SimpleNamespace(service=service))
        await fail.wait()
        raise RuntimeError('test-only secret never in health')
    monkeypatch.setattr('workmate_runtime.roles.worker.serve_worker',run)
    web=create_worker_app(SimpleNamespace(runtime=RuntimeSettings()))
    async with web.router.lifespan_context(web):
        fail.set()
        await asyncio.sleep(0)
        async with AsyncClient(transport=ASGITransport(app=web),base_url='http://worker') as client:
            response=await client.get('/health')
            assert response.status_code==503 and 'secret' not in response.text


@pytest.mark.parametrize('after_ready',[False,True])
async def test_indexing_failure_cancels_worker_before_closing_auxiliary_resources(monkeypatch,after_ready,command_transport_fixture):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles import worker
    failure=asyncio.get_running_loop().create_future()
    manager=SimpleNamespace(workmate_indexing_failure=failure)
    stopped,initialized=asyncio.Event(),asyncio.Event()
    order=[]
    async def dispose(): order.append('engine')
    async def verify(*args): pass
    @asynccontextmanager
    async def knowledge(*args,**kwargs):
        try:
            yield manager
        finally:
            assert stopped.is_set()
            order.append('knowledge')
    service=SimpleNamespace(ready=asyncio.Event())
    async def serve(shutdown):
        try:
            if after_ready:
                service.ready.set()
            else:
                failure.set_result(RuntimeError('index consumer exited'))
            await shutdown.wait()
        finally:
            service.ready.clear()
            stopped.set()
            order.append('worker')
    application=SimpleNamespace(service=service,serve=serve)
    monkeypatch.setattr(worker,'create_async_engine',lambda *a,**k:SimpleNamespace(dispose=dispose))
    monkeypatch.setattr(worker,'verify_product_schema',verify)
    monkeypatch.setattr(worker,'create_sandbox_provider',lambda _:SimpleNamespace(probe=verify))
    monkeypatch.setattr(worker,'configured_knowledge_resources',knowledge)
    monkeypatch.setattr(worker,'async_sessionmaker',lambda *a,**k:None)
    monkeypatch.setattr(worker,'WorkerKnowledgeResolver',lambda *a,**k:None)
    monkeypatch.setattr(worker,'compose_worker',lambda *a,**k:application)
    settings=SimpleNamespace(runtime=RuntimeSettings(reme_enabled=False,coordination_redis_url='redis://fixture:6379/0'),database_url='unused',
        storage_node_id='node',workspace_dir='workspace',artifact_dir='artifact')
    running=asyncio.create_task(worker.serve_worker(settings,asyncio.Event(),
        on_ready=lambda app:initialized.set()))
    if after_ready:
        await asyncio.wait_for(initialized.wait(),2)
        failure.set_result(RuntimeError('index consumer exited'))
    with pytest.raises(RuntimeError,match='index consumer exited'):
        await asyncio.wait_for(running,2)
    assert initialized.is_set()==after_ready
    assert order==['worker','knowledge','engine']


async def test_management_rejects_during_index_failure_resource_close(monkeypatch,command_transport_fixture):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from httpx import AsyncClient,ASGITransport
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles import worker
    failure=asyncio.get_running_loop().create_future()
    closing,release=asyncio.Event(),asyncio.Event()
    management=object()
    manager=SimpleNamespace(workmate_indexing_failure=failure,workmate_knowledge_management=management)
    async def noop(*args): pass
    @asynccontextmanager
    async def resources(*args,**kwargs):
        try: yield manager
        finally:
            closing.set()
            await release.wait()
    service=SimpleNamespace(ready=asyncio.Event(),active_sessions=set(),failures={})
    async def execute(shutdown):
        service.ready.set()
        await shutdown.wait()
    application=SimpleNamespace(service=service,serve=execute)
    monkeypatch.setattr(worker,'create_async_engine',lambda *a,**k:SimpleNamespace(dispose=noop))
    monkeypatch.setattr(worker,'verify_product_schema',noop)
    monkeypatch.setattr(worker,'create_sandbox_provider',lambda _:SimpleNamespace(probe=noop))
    monkeypatch.setattr(worker,'configured_knowledge_resources',resources)
    monkeypatch.setattr(worker,'async_sessionmaker',lambda *a,**k:None)
    monkeypatch.setattr(worker,'WorkerKnowledgeResolver',lambda *a,**k:None)
    monkeypatch.setattr(worker,'compose_worker',lambda *a,**k:application)
    settings=SimpleNamespace(runtime=RuntimeSettings(reme_enabled=False,internal_token='fixture',coordination_redis_url='redis://fixture:6379/0'),
        database_url='unused',storage_node_id='node',workspace_dir='workspace',artifact_dir='artifact')
    web=worker.create_worker_app(settings)
    async with web.router.lifespan_context(web):
        assert application.knowledge_management is management
        failure.set_result(RuntimeError('fixture indexing stopped'))
        try:
            await asyncio.wait_for(closing.wait(),3)
            assert not web.state.running.done()
            async with AsyncClient(transport=ASGITransport(app=web,raise_app_exceptions=False),base_url='http://worker') as client:
                response=await client.get('/workmate/knowledge/kb/documents',
                    headers={'X-Workmate-Internal-Token':'fixture','X-User-ID':'owner'})
                assert response.status_code==503,response.text
        finally:
            release.set()


@pytest.mark.parametrize('column',['storage_key','size_bytes'])
async def test_worker_schema_check_rejects_missing_skill_storage_column(database,column):
    from workmate_runtime.roles.worker import verify_product_schema
    sessions,admin=database
    await admin.execute(f'ALTER TABLE skill_artifacts RENAME COLUMN {column} TO unavailable_column')
    from sqlalchemy.exc import ProgrammingError
    with pytest.raises(ProgrammingError) as missing:
        await verify_product_schema(sessions.kw['bind'])
    assert missing.value.orig.sqlstate=='42703'


async def test_same_storage_replicas_use_distinct_index_lease_owners(monkeypatch,command_transport_fixture):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles import worker

    index_owners, storage_nodes = [], []
    async def noop(*args): pass
    @asynccontextmanager
    async def knowledge(settings, *, node_id):
        index_owners.append(node_id)
        yield None
    def compose(*args, storage_node_id, **kwargs):
        storage_nodes.append(storage_node_id)
        service = SimpleNamespace(ready=asyncio.Event())
        async def serve(shutdown):
            service.ready.set()
            await shutdown.wait()
        return SimpleNamespace(service=service, serve=serve)
    monkeypatch.setattr(worker, 'create_async_engine', lambda *a, **k: SimpleNamespace(dispose=noop))
    monkeypatch.setattr(worker, 'verify_product_schema', noop)
    monkeypatch.setattr(worker, 'create_sandbox_provider', lambda _: SimpleNamespace(probe=noop))
    monkeypatch.setattr(worker, 'configured_knowledge_resources', knowledge)
    monkeypatch.setattr(worker, 'compose_worker', compose)
    settings = SimpleNamespace(runtime=RuntimeSettings(reme_enabled=False,coordination_redis_url='redis://fixture:6379/0'), database_url='unused',
        storage_node_id='shared-storage', workspace_dir='workspace', artifact_dir='artifact')
    async def lifetime():
        shutdown = asyncio.Event()
        await worker.serve_worker(settings, shutdown, on_ready=lambda app: shutdown.set())
    await asyncio.wait_for(asyncio.gather(lifetime(), lifetime()), 3)
    assert storage_nodes == ['shared-storage', 'shared-storage']
    assert len(set(index_owners)) == 2
    assert all(owner != settings.storage_node_id for owner in index_owners)


async def test_node_identity_is_protected_and_reports_readiness(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from httpx import AsyncClient, ASGITransport
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.roles.worker import create_worker_app
    service = SimpleNamespace(ready=asyncio.Event(),active_sessions=set(),failures={})
    async def run(settings, shutdown, *, on_ready):
        service.ready.set()
        on_ready(SimpleNamespace(service=service))
        await shutdown.wait()
        service.ready.clear()
    monkeypatch.setattr('workmate_runtime.roles.worker.serve_worker',run)
    web = create_worker_app(SimpleNamespace(runtime=RuntimeSettings(internal_token='secret'),storage_node_id='node-a'))
    async with AsyncClient(transport=ASGITransport(app=web),base_url='http://worker') as client:
        assert (await client.get('/workmate/node')).status_code == 403
        headers = {'X-Workmate-Internal-Token':'secret'}
        assert (await client.get('/workmate/node',headers=headers)).status_code == 503
        async with web.router.lifespan_context(web):
            response = await client.get('/workmate/node',headers=headers)
            assert response.status_code == 200
            assert response.json() == {'role':'worker','ready':True,'storage_node_id':'node-a','artifact_storage_backend':'local'}
        assert (await client.get('/workmate/node',headers=headers)).status_code == 503
