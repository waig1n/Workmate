"""Knowledge bootstrap serializes schema creation across Worker replicas."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_concurrent_first_boot_and_failed_boot_release_schema_lock(monkeypatch):
    from workmate_runtime.rag.lifecycle import initialize_knowledge_schema
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('WORKMATE_TEST_POSTGRES_DSN required')
    url = dsn.replace('postgresql://', 'postgresql+asyncpg://', 1)
    schema = 'knowledge_boot_' + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engines = [create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
               for _ in range(2)]
    try:
        from agentscope.app.storage._sql._tables import _Base
        original = _Base.metadata.create_all
        calls = []
        def fail(connection):
            calls.append('failed')
            raise RuntimeError('schema fixture failed')
        monkeypatch.setattr(_Base.metadata, 'create_all', fail)
        with pytest.raises(RuntimeError, match='schema fixture failed'):
            await initialize_knowledge_schema(engines[0])
        monkeypatch.setattr(_Base.metadata, 'create_all', original)
        await asyncio.wait_for(asyncio.gather(*(initialize_knowledge_schema(e) for e in engines)), 15)
        async with engines[1].connect() as connection:
            tables = await connection.scalar(text('SELECT count(*) FROM information_schema.tables WHERE table_schema=:schema'), {'schema': schema})
            assert tables == len(_Base.metadata.tables)
        assert calls == ['failed']
    finally:
        for engine in engines:
            await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_schema_initialization_failure_disposes_engine(monkeypatch):
    from types import SimpleNamespace
    from workmate_runtime.rag import lifecycle
    disposed = []
    async def dispose():
        disposed.append(True)
    engine = SimpleNamespace(dispose=dispose)
    monkeypatch.setattr('sqlalchemy.ext.asyncio.create_async_engine', lambda *a, **k: engine)
    async def fail(value):
        assert value is engine
        raise RuntimeError('initialization failed')
    monkeypatch.setattr(lifecycle, 'initialize_knowledge_schema', fail)
    settings = SimpleNamespace(knowledge=SimpleNamespace(enabled=True),
        as_database_url='postgresql+asyncpg://fixture/unused')
    with pytest.raises(RuntimeError, match='initialization failed'):
        async with lifecycle.configured_knowledge_resources(settings, node_id='index-fixture'):
            pytest.fail('failed schema must not enter knowledge lifecycle')
    assert disposed == [True]
