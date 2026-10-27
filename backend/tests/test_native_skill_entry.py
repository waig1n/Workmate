"""Native composition owns Skill catalogs and never installs through App."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from app.auth import get_current_user


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_native_lifespan_owns_configured_catalog_and_router(monkeypatch, enabled):
    import app.main as entry
    from workmate_product.skills import lifecycle
    events = []
    from test_skill_blob_storage import MemoryBlobStore
    from workmate_product.skill_storage import PreparedSkillBlobStore
    active_contexts=0
    class Store(MemoryBlobStore):
        async def put_verified(self,digest,raw):
            assert active_contexts==0, 'Builtin objects must prepare outside bootstrap DB contexts'
            return await super().put_verified(digest,raw)
    store=Store()

    class Catalog:
        hub_id = 'skillhub-cn'
        display_name = 'Catalog'
        description = ''
        icon_url = None

        def __init__(self, **kwargs):
            assert kwargs == dict(base_url='https://catalog.example', timeout=7, max_retries=1)

        async def __aenter__(self):
            events.append('open')
            return self

        async def __aexit__(self, *args):
            events.append('close')

    class MCPCatalog:
        hub_id = 'github'
        display_name = 'GitHub MCP Registry'
        description = ''
        icon_url = None
        def __init__(self, *, api_token):
            assert api_token == 'fixture-catalog-token'
        async def __aenter__(self):
            events.append('mcp-open')
            return self
        async def __aexit__(self, *args):
            events.append('mcp-close')

    monkeypatch.setenv('GITHUB_TOKEN', 'fixture-catalog-token')
    monkeypatch.setattr(entry, 'GitHubMCPCatalog', MCPCatalog)

    class DB:
        def __init__(self): self.info={"skill_blob_store":store}
        async def run_sync(self, callback): pass
        async def commit(self): events.append('commit')

    @asynccontextmanager
    async def db_context():
        nonlocal active_contexts
        active_contexts+=1
        try:
            yield DB()
        finally:
            active_contexts-=1

    async def noop(*args): pass
    async def library(db):
        assert isinstance(db.info['skill_blob_store'],PreparedSkillBlobStore)
        assert store.objects
        events.append('library')
    async def assistant(db): events.append('assistant')
    async def serve(shutdown):
        await shutdown.wait()
        events.append('projection-stop')

    class ForbiddenClient:
        def __init__(self,*args,**kwargs): pass
        def __getattr__(self, name):
            raise AssertionError('Legacy Runtime call: ' + name)

    # Catalog ownership is tested independently of command/realtime I/O. The
    # production lifespan requires both transports, covered by role tests.
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.config.command_queue import CommandQueuePolicy
    from workmate_product.config.schedules import SchedulePolicy
    from workmate_product.config.channels import ChannelPolicy
    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object()
    async def background(shutdown):
        await shutdown.wait()
    db_context.kw = {}
    db_context.configure = lambda **kwargs: db_context.kw.update(kwargs)
    monkeypatch.setattr('redis.asyncio.Redis.from_url', transport)
    monkeypatch.setattr('workmate_product.coordination.WakeupBus', transport)
    monkeypatch.setattr('workmate_product.realtime.composition.create_realtime_log', lambda *args, **kwargs: object())
    monkeypatch.setattr('workmate_product.coordination.command_publisher.CommandPublisher',
        lambda *args, **kwargs: SimpleNamespace(serve=background))
    monkeypatch.setattr('workmate_product.realtime.business_publisher.BusinessPublisher',
        lambda *args, **kwargs: SimpleNamespace(serve=background))
    stream=SimpleNamespace(reader=SimpleNamespace(close=noop),manager=object(),close=noop)

    monkeypatch.setattr(entry, '_warn_insecure_settings', lambda: None)
    monkeypatch.setattr(entry, 'get_sessionmaker', lambda: db_context)
    monkeypatch.setattr(entry, 'dispose_engine', noop)
    monkeypatch.setattr(entry, 'ensure_bootstrap', noop)
    monkeypatch.setattr(entry, 'ensure_workspace_assistant', assistant)
    monkeypatch.setattr(entry, 'ensure_builtin_skill_library', library)
    monkeypatch.setattr(entry, 'KnowledgeServiceClient', ForbiddenClient)
    monkeypatch.setattr(entry, 'get_settings', lambda: SimpleNamespace(
        **{'schedule_' + key: value for key, value in SchedulePolicy(enabled=False).model_dump().items()},
        **{'channel_' + key: value for key, value in ChannelPolicy(enabled=False).model_dump().items()},
        workmate_public_url='',
        runtime_base_url='http://worker',runtime_internal_token='fixture-token',runtime_timeout_secs=10,worker_nodes=[],
        coordination_redis_url='redis://fixture.invalid',coordination_namespace='catalog-test',
        command_queue=CommandQueuePolicy(),realtime=RealtimePolicy(),
        workspace_assistant_enabled=True, skillhub_cn_enabled=enabled, github_mcp_hub_enabled=enabled,
        skillhub_cn_base_url='https://catalog.example', skillhub_cn_request_timeout_secs=7,
        skillhub_cn_max_retries=1))
    monkeypatch.setattr(lifecycle, 'SkillHubCN', Catalog)
    monkeypatch.setattr('app.services.durable_runtime.compose_product_services',
        lambda *args, **kwargs: (SimpleNamespace(admission=object()), SimpleNamespace(serve=serve), stream))
    web = entry.build_app()
    web.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='u')
    async with web.router.lifespan_context(web):
        async with AsyncClient(transport=ASGITransport(web), base_url='http://test') as client:
            response = await client.get('/api/skills/hubs')
            assert response.status_code == 200
            assert {hub['hub_id'] for hub in response.json()} == (
                {'workmate-builtin', 'uploaded', 'skillhub-cn'} if enabled else {'workmate-builtin', 'uploaded'})
    assert events == (['library', 'assistant', 'commit', 'open', 'mcp-open', 'projection-stop', 'mcp-close', 'close'] if enabled
                      else ['library', 'assistant', 'commit', 'projection-stop'])
