"""Default gateway owns realtime transport and supervises business publishing."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.config import Settings
from app.main import build_app


@pytest.mark.parametrize("task_name", [
    "projection_task", "command_publisher_task", "business_publisher_task",
    "schedule_task", "channel_task",
])
@pytest.mark.parametrize("state", ["missing", "done", "running"])
async def test_health_requires_enabled_background_services(monkeypatch, task_name, state):
    import app.main as entry

    settings = Settings(_env_file=None, channel_enabled=True, schedule_enabled=True)
    monkeypatch.setattr(entry, "get_settings", lambda: settings)
    web = build_app()
    web.state.product_ready = True
    for name in ("projection_task", "command_publisher_task", "business_publisher_task",
                 "schedule_task", "channel_task"):
        if name != task_name:
            setattr(web.state, name, SimpleNamespace(done=lambda: False))
    if state != "missing":
        setattr(web.state, task_name, SimpleNamespace(done=lambda: state == "done"))
    async with AsyncClient(
        transport=ASGITransport(app=web), base_url="http://gateway"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == (200 if state == "running" else 503)
        assert response.json() == {"role": "product-api", "ready": state == "running"}


@pytest.mark.parametrize("failure", [None, "startup", "publisher"])
async def test_gateway_activates_realtime_and_closes_responses_before_database(
    monkeypatch, failure
):
    import app.main as entry
    from app.services.realtime_product_stream import RealtimeProductStream
    from workmate_product.realtime.reader import ConversationReaderHub
    from workmate_product.realtime.business_publisher import BusinessPublisher
    from workmate_product.coordination.command_publisher import CommandPublisher
    from workmate_product.projections.service import ProductProjectionService
    from app.services.schedules import ScheduleService

    order, clients = [], []
    background_closing, release_background = asyncio.Event(), asyncio.Event()
    original = Redis.from_url

    def client_factory(*args, **kwargs):
        client = original(*args, **kwargs)
        clients.append(client)
        close = client.aclose

        async def tracked_close(*a, **k):
            order.append("redis")
            await close(*a, **k)

        monkeypatch.setattr(client, "aclose", tracked_close)
        return client

    class Sessions:
        kw = {}

        def configure(self, **kwargs):
            self.kw.update(kwargs)

        @asynccontextmanager
        async def __call__(self):
            yield SimpleNamespace(info={}, commit=noop, run_sync=noop)

    async def noop(*args):
        pass

    sessions = Sessions()

    @asynccontextmanager
    async def resources(*args, **kwargs):
        yield {}

    async def dispose():
        order.append("engine")

    async def serve(self, shutdown):
        if failure == "publisher" and isinstance(self, BusinessPublisher):
            raise RuntimeError("test publisher failure")
        await shutdown.wait()
        # Joining one failed background must still await all other resources.
        if failure == "publisher":
            background_closing.set()
            await release_background.wait()
        order.append(type(self).__name__)

    async def stream_close(self):
        order.append("stream")
        await original_stream_close(self)

    async def reader_close(self):
        order.append("reader")
        await original_reader_close(self)

    original_stream_close = RealtimeProductStream.close
    original_reader_close = ConversationReaderHub.close
    monkeypatch.setattr(RealtimeProductStream, "close", stream_close)
    monkeypatch.setattr(ConversationReaderHub, "close", reader_close)
    for component in (BusinessPublisher, CommandPublisher, ProductProjectionService, ScheduleService):
        monkeypatch.setattr(component, "serve", serve)
    monkeypatch.setattr(Redis, "from_url", client_factory)
    monkeypatch.setattr("workmate_product.coordination.WakeupBus", resources)
    monkeypatch.setattr(entry, "skill_catalogs", resources)
    monkeypatch.setattr(entry, "_warn_insecure_settings", lambda: None)
    monkeypatch.setattr(entry, "get_sessionmaker", lambda: sessions)
    monkeypatch.setattr(entry, "dispose_engine", dispose)
    monkeypatch.setattr(entry, "ensure_bootstrap", noop)
    monkeypatch.setattr(entry, "ensure_builtin_skill_library", noop)
    monkeypatch.setattr(
        "workmate_product.skill_storage.get_skill_blob_store", lambda db: object()
    )
    monkeypatch.setattr(
        "workmate_product.skills.catalog.BuiltinCatalog.list_skills",
        lambda *a: empty_page(),
    )
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: SimpleNamespace(enabled=False),
    )
    settings = Settings(
        _env_file=None,
        coordination_redis_url="redis://fixture:6379/0",
        workspace_assistant_enabled=False,
        github_mcp_hub_enabled=False,
    )
    monkeypatch.setattr(entry, "get_settings", lambda: settings)

    async def empty_page():
        return SimpleNamespace(cards=[], next_cursor=None)

    web = entry.build_app()
    if failure == "startup":

        def fail_composition(*args, **kwargs):
            raise RuntimeError("test startup failure")

        monkeypatch.setattr(
            "app.services.durable_runtime.compose_product_services", fail_composition
        )
        with pytest.raises(RuntimeError, match="test startup failure"):
            async with web.router.lifespan_context(web):
                pytest.fail("Startup failure must not yield readiness")
        assert not web.state.product_ready
        assert "redis" in order
        assert order[-1] == "engine"
        return
    if failure == "publisher":
        lifespan = web.router.lifespan_context(web)
        await lifespan.__aenter__()
        await asyncio.sleep(0)
        closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
        try:
            await asyncio.wait_for(background_closing.wait(), 2)
            await asyncio.sleep(0)
            assert "engine" not in order, (
                "Database must remain open while background work drains"
            )
        finally:
            release_background.set()
            with pytest.raises(RuntimeError, match="test publisher failure"):
                await closing
        for task_name in ("ProductProjectionService", "CommandPublisher", "ScheduleService"):
            assert task_name in order, (
                "All background tasks must join even if one failed"
            )
            assert order.index(task_name) < order.index("engine")
        return
    async with web.router.lifespan_context(web):
        stream = web.state.product_stream
        assert isinstance(stream, RealtimeProductStream), (
            "Production gateway must select Redis-backed SSE"
        )
        assert web.state.business_publisher_task is not None
        assert web.state.schedule_task is not None
        assert stream.log.client in clients
        assert len({id(client.connection_pool) for client in clients}) >= 2
        assert stream.log.client.get_retry().get_retries() == 0
        assert not stream.log.client.connection_pool.connection_kwargs.get(
            "retry_on_timeout", False
        )
        await asyncio.sleep(0)
    assert order.index("stream") < order.index("reader")
    assert order.index("reader") < order.index("BusinessPublisher")
    assert order.index("BusinessPublisher") < order.index("redis")
    assert order.index("ScheduleService") < order.index("engine")
    assert order[-1] == "engine"
