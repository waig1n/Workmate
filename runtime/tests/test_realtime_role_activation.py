"""Production Worker owns an independent no-retry realtime transport."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis

from workmate_runtime.config import RuntimeSettings
from workmate_runtime.roles import worker


@pytest.mark.parametrize('execution_failure', [False, True])
async def test_worker_role_activates_realtime_and_closes_it_after_execution(
    monkeypatch, execution_failure,
):
    order, clients, captured = [], [], {}
    original = Redis.from_url

    def client_factory(*args, **kwargs):
        client = original(*args, **kwargs)
        clients.append(client)
        close = client.aclose

        async def tracked_close(*a, **k):
            order.append(("redis", client))
            await close(*a, **k)

        monkeypatch.setattr(client, "aclose", tracked_close)
        return client

    async def noop(*args):
        pass

    async def dispose():
        order.append(("engine", None))

    @asynccontextmanager
    async def knowledge(*args, **kwargs):
        yield None

    @asynccontextmanager
    async def wakeups(*args, **kwargs):
        yield None

    def compose(*args, **kwargs):
        captured.update(kwargs)
        service = SimpleNamespace(ready=asyncio.Event())

        async def serve(shutdown):
            try:
                if execution_failure:
                    raise RuntimeError("test execution failure")
                service.ready.set()
                await shutdown.wait()
            finally:
                order.append(("worker", None))

        return SimpleNamespace(service=service, serve=serve)

    monkeypatch.setattr(Redis, "from_url", client_factory)
    monkeypatch.setattr("workmate_product.coordination.WakeupBus", wakeups)
    monkeypatch.setattr(
        worker, "create_async_engine", lambda *a, **k: SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(worker, "verify_product_schema", noop)
    monkeypatch.setattr(
        worker, "create_sandbox_provider", lambda _: SimpleNamespace(probe=noop)
    )
    monkeypatch.setattr(worker, "configured_knowledge_resources", knowledge)
    monkeypatch.setattr(worker, "compose_worker", compose)
    settings = SimpleNamespace(
        runtime=RuntimeSettings(
            reme_enabled=False, coordination_redis_url="redis://fixture:6379/0"
        ),
        database_url="unused",
        storage_node_id="node",
        workspace_dir="workspace",
        artifact_dir="artifact",
    )
    shutdown = asyncio.Event()
    running = worker.serve_worker(
        settings, shutdown, on_ready=lambda app: shutdown.set()
    )
    if execution_failure:
        with pytest.raises(RuntimeError, match="test execution failure"):
            await asyncio.wait_for(running, 3)
    else:
        await asyncio.wait_for(running, 3)
    log = captured.get("realtime_log")
    assert log is not None, (
        "Production Worker must inject the realtime Writer transport"
    )
    assert log.client is not captured["command_queue"].client
    assert (
        log.client.connection_pool
        is not captured["command_queue"].client.connection_pool
    )
    assert log.client.get_retry().get_retries() == 0
    assert not log.client.connection_pool.connection_kwargs.get(
        "retry_on_timeout", False
    )
    assert order[0][0] == "worker"
    assert order[-1][0] == "engine"
    assert ("redis", log.client) in order
