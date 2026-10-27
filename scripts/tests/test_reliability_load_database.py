from types import SimpleNamespace
from scripts.reliability.load_support import common


def test_load_gateway_uses_production_factories_and_only_changes_blob_info():
    calls = []
    engine = object()
    store = object()
    factory = SimpleNamespace(kw={'info': {'existing': 'preserved'}})
    factory.configure = lambda **kw: calls.append(('configure', kw))
    database = SimpleNamespace(
        get_engine=lambda: calls.append('engine') or engine,
        get_sessionmaker=lambda: calls.append('sessionmaker') or factory,
    )
    assert common.configure_database(database, store) is engine
    assert calls == ['engine', 'sessionmaker', ('configure', {'info': {'existing': 'preserved', 'skill_blob_store': store}})]


def test_gateway_entrypoint_calls_fixture_database_setup_without_building_engine():
    from pathlib import Path
    import ast
    tree = ast.parse((Path(common.__file__).with_name('gateway.py')).read_text())
    calls = [node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert 'configure_database' in calls
    assert 'create_async_engine' not in calls
    assert 'async_sessionmaker' not in calls



def test_handshake_propagates_original_consumer_failure():
    import asyncio
    async def scenario():
        import asyncio
        import pytest
        marker = RuntimeError('HTTP 500 original')
        async def failed():
            raise marker
        consumer = asyncio.create_task(failed())
        try:
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(common.wait_handshakes([asyncio.Event()], [consumer]), .5)
            assert caught.value is marker
        finally:
            await asyncio.gather(consumer, return_exceptions=True)
    asyncio.run(scenario())


def test_handshake_succeeds_without_cancelling_live_consumer():
    import asyncio
    async def scenario():
        import asyncio
        ready = asyncio.Event()
        async def consuming():
            ready.set()
            await asyncio.Event().wait()
        consumer = asyncio.create_task(consuming())
        try:
            await common.wait_handshakes([ready], [consumer])
            assert not consumer.done()
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
    asyncio.run(scenario())
