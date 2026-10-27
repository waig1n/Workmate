"""Optional worker realtime wiring without starting tasks or external services."""
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy.ext.asyncio import create_async_engine

from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.realtime.redis_log import RedisConversationLog
from test_worker_workspaces import StubWorkspace


@pytest.mark.parametrize('enabled', [False, True])
async def test_worker_realtime_factory_shares_assembler_sessions_and_exact_policy(tmp_path, monkeypatch, enabled):
    # The actual SQLAlchemy/Redis clients are constructed but never connected.
    engine = create_async_engine('postgresql+asyncpg://test:test@127.0.0.1:1/unused')
    client = Redis(retry=Retry(NoBackoff(), 0))
    close_client = AsyncMock(wraps=client.aclose)
    monkeypatch.setattr(client, 'aclose', close_client)
    monkeypatch.setattr('workmate_product.skill_storage.store_from_settings', lambda _: object())
    monkeypatch.setattr('workmate_product.artifact_storage.artifact_store_from_settings', lambda _: object())
    policy = RealtimePolicy(snapshot_interval_seconds=3.0, snapshot_max_bytes=8192,
        max_buffer_events=32, max_buffer_bytes=524288, operation_timeout_seconds=1.0,
        outage_grace_seconds=8.0)
    settings = RuntimeSettings(reme_enabled=False, skill_cache_dir=str(tmp_path/'skills'), realtime=policy)
    log = RedisConversationLog(client, namespace='worker-role-test')
    options = {'realtime_log':log} if enabled else {}
    app = None
    try:
        app = compose_worker(engine, settings, storage_node_id='node',
            workspace_dir=tmp_path/'workspace', artifact_dir=tmp_path/'artifacts',
            max_file_bytes=1024, max_files=3, workspace_factory=StubWorkspace, **options)
        factory = app.service.controller.realtime_writer_factory
        if enabled:
            assert isinstance(factory, RealtimeWriterFactory)
            assert factory.sessions is app.assembler.sessions
            assert factory.sessions.kw['bind'] is engine
            assert factory.log_manager.sessions is factory.sessions
            assert factory.log_manager.log is log and factory.log is log
            assert factory.policy is settings.realtime
            assert factory.policy == policy
        else:
            assert factory is None
        close_client.assert_not_awaited()
    finally:
        if app is not None:
            await app.workspaces.close()
        close_client.assert_not_awaited()
        await client.aclose()
        await engine.dispose()


# Real store integration uses the existing isolated schema and test-only
# scheduler; production command routing is covered by Streams composition tests.
from test_execution_context import database  # noqa: E402,F401
from test_realtime_redis_log import log  # noqa: E402,F401
from polling_worker import polling_composition  # noqa: E402,F401


async def test_composed_worker_routes_actual_core_body_to_redis_and_saves_pg_cut(
    database, log, tmp_path, monkeypatch, polling_composition,
):
    from workmate_runtime.execution import composition
    from test_worker_composition import test_composed_worker_completes_native_reply_and_capture as scenario
    import json
    from agentscope.model import ChatResponse
    import test_worker_composition as worker_scenario
    original_model = worker_scenario.ScriptModel
    class StreamingModel(original_model):
        async def _call_api(self, *args, **kwargs):
            source = await super()._call_api(*args, **kwargs)
            async def chunks():
                async for chunk in source:
                    # Core 2.0.7 only emits display events for nonfinal chunks.
                    # The original final-only fixture proves checkpoints, not SSE.
                    if chunk.is_last:
                        yield ChatResponse(content=chunk.content, is_last=False)
                    yield chunk
            return chunks()
    monkeypatch.setattr(worker_scenario, 'ScriptModel', StreamingModel)
    original = composition.compose_worker
    workers = []
    def inject(*args, **kwargs):
        worker = original(*args, **kwargs, realtime_log=log)
        workers.append(worker)
        return worker
    monkeypatch.setattr(composition, 'compose_worker', inject)
    await scenario(database, tmp_path, monkeypatch, polling_composition)
    factory = workers[0].service.controller.realtime_writer_factory
    assert factory.sessions is workers[0].assembler.sessions
    ref = await factory.log_manager.ensure('c')
    window = await log.window(ref)
    page = await log.read(ref, window.floor)
    assert any(entry.envelope.text is not None for entry in page.entries)
    assert 'native reply' in json.dumps([entry.envelope.model_dump(mode='json') for entry in page.entries])
    _, admin = database
    assert await admin.fetchval('SELECT count(*) FROM task_messages WHERE snapshot_version>0') == 1
    types = await admin.fetch("SELECT payload->>'type' AS type FROM runtime_events")
    assert not {'TEXT_BLOCK_START','TEXT_BLOCK_DELTA','TEXT_BLOCK_END'}.intersection(row['type'] for row in types)
