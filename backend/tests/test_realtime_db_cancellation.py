"""Real PostgreSQL connections must return after level-triggered SSE cancellation."""
import asyncio
import gc
import os

import anyio
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.conversation_business_state import ConversationBusinessState


@pytest_asyncio.fixture
async def pooled_sessions():
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires isolated PostgreSQL')
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
                                 pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize('operation', ['authorize', 'active_display', 'read_view', 'display_snapshot'])
async def test_cancelled_authority_returns_checked_out_connection(pooled_sessions, monkeypatch, caplog, operation):
    sessions = pooled_sessions
    state = ConversationBusinessState(sessions, 'actor')
    entered = anyio.Event()
    progressed = []

    async def authorize(db, conversation_id):
        await db.execute(text('SELECT 1'))
        entered.set()
        await anyio.sleep_forever()
        progressed.append('unauthorized continuation')

    monkeypatch.setattr(state, '_authorize', authorize)
    for _ in range(5):
        async with anyio.create_task_group() as group:
            async def invoke():
                if operation == 'display_snapshot':
                    from workmate_contracts.realtime import message_id
                    reference = {'message_id': message_id('t', 'r'), 'snapshot_version': 1,
                                 'execution_epoch': 1, 'sha256': '0' * 64}
                    await state.display_snapshot('conversation', reference, task_id='t', reply_id='r')
                else:
                    await getattr(state, operation)('conversation')
            group.start_soon(invoke)
            await entered.wait()
            group.cancel_scope.cancel()
        # Cleanup must finish before the request task returns, not rely on GC.
        assert sessions.kw['bind'].pool.checkedout() == 0
        async with sessions.begin() as db:
            assert await db.scalar(text('SELECT 42')) == 42
        entered = anyio.Event()
    gc.collect()
    await asyncio.sleep(0)
    assert not progressed
    assert not any('non-checked-in' in record.message or 'Exception terminating' in record.message
                   for record in caplog.records)


async def test_cancelled_commit_exit_returns_connection(pooled_sessions):
    from sqlalchemy import event
    state = ConversationBusinessState(pooled_sessions, 'actor')
    engine = pooled_sessions.kw['bind']
    scope = None

    def cancel_at_commit(connection):
        scope.cancel()

    event.listen(engine.sync_engine, 'commit', cancel_at_commit)
    try:
        with anyio.CancelScope() as scope:
            async with state._read_transaction() as db:
                await db.execute(text('SELECT 1'))
        assert scope.cancel_called
        assert engine.pool.checkedout() == 0
    finally:
        event.remove(engine.sync_engine, 'commit', cancel_at_commit)


async def test_client_socket_disconnect_during_reauthorization_returns_pool(http_scene, monkeypatch, caplog):
    import socket
    import httpx
    import uvicorn

    scene = http_scene
    acquired = asyncio.Event()
    armed = False
    original = ConversationBusinessState._authorize

    async def authorize(self, db, conversation_id):
        result = await original(self, db, conversation_id)
        if armed:
            acquired.set()
            await anyio.sleep_forever()
        return result

    monkeypatch.setattr(ConversationBusinessState, '_authorize', authorize)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(scene.web, lifespan='off', log_level='warning'))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(timeout=10) as client:
            async with client.stream('GET', f'http://127.0.0.1:{port}{scene.path}',
                    headers={'Authorization': 'Bearer ' + scene.token}) as response:
                assert response.status_code == 200
                iterator = response.aiter_bytes()
                assert await anext(iterator)
                armed = True
                await asyncio.wait_for(acquired.wait(), 5)
                assert scene.sessions.kw['bind'].pool.checkedout() == 1
            # Exiting stream closes the actual client socket while the server
            # is awaiting authorization with a checked-out PG connection.
            async with asyncio.timeout(3):
                while scene.stream._handles or scene.sessions.kw['bind'].pool.checkedout():
                    await asyncio.sleep(.01)
        assert not scene.reader._hubs
        gc.collect()
        await asyncio.sleep(0)
        assert not any('non-checked-in' in record.message or 'Exception terminating' in record.message
                       for record in caplog.records)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()


# Shared real PG/Redis scene, imported explicitly so pytest registers dependencies.
from test_durable_projection import projection_database  # noqa: E402,F401
from test_business_reset_composition import composition  # noqa: E402,F401
from test_realtime_stream_http import http_scene  # noqa: E402,F401


async def test_read_transaction_rolls_back_and_preserves_error(pooled_sessions):
    state = ConversationBusinessState(pooled_sessions, 'actor')
    with pytest.raises(ValueError, match='original error'):
        async with state._read_transaction() as db:
            await db.execute(text('CREATE TABLE rollback_probe (id integer)'))
            raise ValueError('original error')
    assert pooled_sessions.kw['bind'].pool.checkedout() == 0
    async with pooled_sessions.begin() as db:
        assert await db.scalar(text("SELECT to_regclass('rollback_probe')")) is None


async def test_stalled_read_cleanup_invalidates_connection_within_deadline(pooled_sessions, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    import app.services.conversation_business_state as module
    state = ConversationBusinessState(pooled_sessions, 'actor')
    monkeypatch.setattr(module, '_READ_CLEANUP_SECONDS', .05, raising=False)

    async def stalled_exit(self, *args):
        await anyio.sleep_forever()

    monkeypatch.setattr(AsyncSessionTransaction, '__aexit__', stalled_exit)
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            async with state._read_transaction() as db:
                await db.execute(text('SELECT 1'))
    assert pooled_sessions.kw['bind'].pool.checkedout() == 0
    # A discarded connection must not exhaust the single-slot pool.
    async with pooled_sessions() as db:
        assert await db.scalar(text('SELECT 42')) == 42


async def test_repeated_task_cancel_joins_cleanup_and_propagates(pooled_sessions, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    entered = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSessionTransaction.__aexit__

    async def paused_exit(self, *args):
        entered.set()
        await release.wait()
        await original(self, *args)

    monkeypatch.setattr(AsyncSessionTransaction, '__aexit__', paused_exit)
    state = ConversationBusinessState(pooled_sessions, 'actor')

    async def read():
        async with state._read_transaction() as db:
            await db.execute(text('SELECT 1'))

    task = asyncio.create_task(read())
    await asyncio.wait_for(entered.wait(), 2)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert pooled_sessions.kw['bind'].pool.checkedout() == 0


@pytest.mark.parametrize('cancellation', ['scope', 'repeated_task'])
async def test_cancelled_in_flight_query_discards_socket_without_leaking(
        pooled_sessions, monkeypatch, caplog, tmp_path, cancellation):
    """Cancel actual SQL and observe server termination, not just pool return."""
    import asyncpg
    import json
    import time

    observer = await asyncpg.connect(os.environ['WORKMATE_TEST_POSTGRES_DSN'])
    state = ConversationBusinessState(pooled_sessions, 'actor')
    entered = anyio.Event()
    backend_pid = None
    owner = None

    async def slow_authorization(db, conversation_id):
        nonlocal backend_pid
        backend_pid = await db.scalar(text('SELECT pg_backend_pid()'))
        entered.set()
        await db.execute(text('SELECT pg_sleep(30)'))
        raise AssertionError('Cancelled authorization continued')

    monkeypatch.setattr(state, '_authorize', slow_authorization)
    async def invoke():
        nonlocal owner
        owner = asyncio.current_task()
        await state.authorize('conversation')

    try:
        async with asyncio.timeout(5):
            async with anyio.create_task_group() as group:
                group.start_soon(invoke)
                await entered.wait()
                while not await observer.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                        WHERE pid=$1 AND state='active' AND query LIKE '%pg_sleep%')""", backend_pid):
                    await asyncio.sleep(.01)
                cancelled_at = time.monotonic()
                if cancellation == 'scope':
                    group.cancel_scope.cancel()
                else:
                    for _ in range(3):
                        owner.cancel()
                        await asyncio.sleep(0)
            assert pooled_sessions.kw['bind'].pool.checkedout() == 0
            while await observer.fetchval('SELECT count(*) FROM pg_stat_activity WHERE pid=$1', backend_pid):
                await asyncio.sleep(.01)
            elapsed = time.monotonic() - cancelled_at
            assert elapsed < 3
            async with pooled_sessions.begin() as db:
                assert await db.scalar(text('SELECT 42')) == 42
        gc.collect()
        assert not any('non-checked-in' in record.message or 'Exception terminating' in record.message
                       for record in caplog.records)
        (tmp_path / 'query-cancel-evidence.json').write_text(json.dumps({
            'cancellation': cancellation, 'server_backend_terminated': True,
            'seconds': elapsed, 'checked_out': pooled_sessions.kw['bind'].pool.checkedout(),
            'subsequent_query': 42, 'connection_cleanup_warnings': 0,
        }, indent=2))
    finally:
        await observer.close()
