"""Batch ingestion preserves replay and rollback on real PostgreSQL."""
import asyncio

import pytest
from sqlalchemy import event as sql_event

from test_projection import database, source, project  # noqa: F401
from test_command_admission import database as admission_database  # noqa: F401
from workmate_product.projections import store


async def consume(sessions, *, batch=True, projector=project):
    async with sessions.begin() as db:
        fn = store.consume_batch if batch else store.consume_next
        return await fn(db, 'c', project=projector)


async def seed(admin, count, *, delta='a', kind='TEXT_BLOCK_DELTA'):
    for seq in range(1, count + 1):
        await source(admin, seq, {'type': kind, 'reply_id': 'reply', 'delta': delta})


async def test_batch_writes_state_and_cursor_once_and_replays_every_event(database):
    sessions, admin = database
    await seed(admin, 6)
    statements = []
    commits = []
    def committed(conn):
        commits.append(conn)
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = sessions.kw['bind'].sync_engine
    sql_event.listen(engine, 'before_cursor_execute', record)
    sql_event.listen(engine, 'commit', committed)
    try:
        result = await consume(sessions)
    finally:
        sql_event.remove(engine, 'before_cursor_execute', record)
        sql_event.remove(engine, 'commit', committed)
    assert result.runtime_seq == 6
    assert len(commits) == 1
    assert await admin.fetchval('SELECT content FROM task_messages') == 'aaaaaa'
    for table in ('task_projection_states', 'runtime_projection_cursors'):
        assert sum(f'INSERT INTO {table}' in sql for sql in statements) == 1
    assert sum('FROM agents' in sql and 'FOR KEY SHARE' in sql for sql in statements) == 1
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 6
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 6
    assert await consume(sessions) is None


async def test_batch_middle_failure_rolls_back_even_if_caller_commits_then_retry(database):
    sessions, admin = database
    await seed(admin, 3)
    async def fail(db, event, state, boundary):
        result = await project(db, event, state, boundary)
        if event.runtime_seq == 2:
            raise RuntimeError('middle failure')
        return result
    async with sessions.begin() as db:
        with pytest.raises(RuntimeError, match='middle failure'):
            await store.consume_batch(db, 'c', project=fail)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    assert await admin.fetchval('SELECT count(*) FROM task_projection_states') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors') == 0
    assert await admin.fetchval('SELECT product_seq FROM conversations') == 0
    assert await admin.fetchval('SELECT content FROM task_messages') == 'hello'
    assert (await consume(sessions)).runtime_seq == 3
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 3


@pytest.mark.parametrize('boundary', ['TOOL_CALL_START', 'TOOL_CALL_END', 'TOOL_RESULT_END',
                                     'REPLY_END', 'REQUIRE_USER_CONFIRM'])
async def test_batch_stops_before_boundary_and_processes_boundary_alone(database, boundary):
    sessions, admin = database
    await seed(admin, 2)
    await source(admin, 3, {'type': boundary, 'reply_id': 'reply'})
    await source(admin, 4, {'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'reply', 'delta': 'b'})
    assert (await consume(sessions)).runtime_seq == 2
    assert (await consume(sessions)).runtime_seq == 3
    assert (await consume(sessions)).runtime_seq == 4


@pytest.mark.parametrize('count,delta,first', [(20, 'a', 16), (3, 'x'*70000, 1),
                                             (2, 'x'*140000, 1)], ids=['count', 'bytes', 'large-first'])
async def test_batch_count_and_byte_limits_and_large_first_event(database, count, delta, first):
    sessions, admin = database
    await seed(admin, count, delta=delta)
    assert (await consume(sessions)).runtime_seq == first
    while await consume(sessions):
        pass
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == count


async def test_single_entrypoint_remains_single_and_batch_consumers_serialize(database):
    sessions, admin = database
    await seed(admin, 10)
    assert (await consume(sessions, batch=False)).runtime_seq == 1
    results = await asyncio.gather(consume(sessions), consume(sessions))
    assert sum(result is not None for result in results) == 1
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 10
    assert await admin.fetchval('SELECT content FROM task_messages') == 'a'*10


async def test_batch_validates_each_source_and_rolls_back_on_gap(database):
    sessions, admin = database
    await seed(admin, 1)
    await source(admin, 3, {'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'reply', 'delta': 'c'})
    with pytest.raises(store.ProjectionConflict, match='gap'):
        await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0


@pytest.mark.parametrize('change', ['reply', 'epoch'])
async def test_batch_never_crosses_identity_change(database, change):
    sessions, admin = database
    await seed(admin, 2)
    if change == 'reply':
        await admin.execute("UPDATE runtime_events SET payload=jsonb_set(payload,'{reply_id}','\"other\"') WHERE runtime_seq=2")
    else:
        await admin.execute('UPDATE runtime_events SET execution_epoch=2 WHERE runtime_seq=2')
    assert (await consume(sessions)).runtime_seq == 1
    with pytest.raises(store.ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 1


async def test_batch_skip_locked_leaves_another_consumers_conversation_alone(database):
    sessions, admin = database
    await seed(admin, 2)
    async with sessions.begin() as owner:
        await store.lock_product_conversation(owner, 'c')
        async with sessions.begin() as other:
            assert await asyncio.wait_for(store.consume_batch(other, 'c', project=project, skip_locked=True), 2) is None
    assert (await consume(sessions)).runtime_seq == 2


@pytest.mark.parametrize('raw', [None, [], {'type':'TEXT_BLOCK_DELTA','reply_id':'reply','session_id':'foreign'}])
async def test_malformed_or_foreign_session_cannot_commit_partial_batch(database, raw):
    sessions, admin = database
    await seed(admin, 1)
    await source(admin, 2, raw)
    with pytest.raises(store.ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0


async def test_batch_stops_at_task_change(database):
    from sqlalchemy import text
    sessions, admin = database
    await seed(admin, 1)
    async with sessions.begin() as db:
        await db.execute(text("INSERT INTO tasks VALUES ('next','c','running')"))
        await db.execute(text("""INSERT INTO runtime_task_executions(task_id,session_id,execution_epoch,status,reply_id)
            VALUES ('next','s',1,'running','next-reply')"""))
    await source(admin, 2, {'type':'TEXT_BLOCK_DELTA','reply_id':'next-reply','delta':'b'}, task='next')
    assert (await consume(sessions)).runtime_seq == 1
    assert (await consume(sessions)).runtime_seq == 2
    assert await admin.fetchval('SELECT count(*) FROM task_projection_states') == 2
