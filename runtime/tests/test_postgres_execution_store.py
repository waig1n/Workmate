"""Real PostgreSQL tests for the Runtime persistence boundary, not API integration."""
import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio

from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership


DDL = """
CREATE TABLE users (id varchar(36) PRIMARY KEY, status text NOT NULL);
CREATE TABLE agents (id varchar(36) PRIMARY KEY, deleted_at timestamptz);
CREATE TABLE conversations (id varchar(36) PRIMARY KEY, user_id varchar(36) REFERENCES users,
    agent_id varchar(36) REFERENCES agents);
CREATE TABLE tasks (id varchar(36) PRIMARY KEY, conversation_id varchar(36) REFERENCES conversations,
    status text NOT NULL);
INSERT INTO users VALUES ('user','active');
INSERT INTO agents VALUES ('agent',NULL);
INSERT INTO conversations VALUES ('conversation','user','agent');
INSERT INTO tasks VALUES ('task','conversation','created'), ('other-task','conversation','created'),
    ('task-b','conversation','created');
ALTER TABLE tasks ADD COLUMN approval_profile text NOT NULL DEFAULT 'standard';
"""

SEED = """
UPDATE conversations SET active_product_task_id='task';
INSERT INTO runtime_sessions(id, conversation_id, storage_node_id) VALUES ('session','conversation','node');
INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
VALUES ('command','session','conversation','task','user','StartTask',0,'{}',repeat('d',64));
INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task','session');
"""



@pytest_asyncio.fixture
async def database():
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires an isolated PostgreSQL database')
    import asyncpg
    schema = 'execution_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    engine = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute(DDL)
        from sqlalchemy.ext.asyncio import create_async_engine
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        engine = create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),
            connect_args={'server_settings': {'search_path': schema}})
        from workmate_product.persistence.models import Base
        def upgrade(connection):
            connection.exec_driver_sql("ALTER TABLE conversations ADD COLUMN active_product_task_id varchar(36) REFERENCES tasks(id), ADD COLUMN deleted_at timestamptz, ADD COLUMN product_seq bigint NOT NULL DEFAULT 0, ADD COLUMN business_seq bigint NOT NULL DEFAULT 0")
            tables = ['runtime_sessions', 'runtime_commands', 'runtime_events', 'runtime_snapshots', 'runtime_task_executions', 'conversation_retirements', 'command_deliveries', 'command_delivery_quarantines', 'workspace_residencies', 'workspace_release_requests', 'workspace_checkpoints', 'recovery_forks', 'runtime_sandbox_attestations', 'runtime_permission_upgrades', 'runtime_suspensions', 'runtime_delivery_baselines', 'runtime_delivery_captures', 'runtime_delivery_seals']
            Base.metadata.create_all(connection, tables=[Base.metadata.tables[name] for name in tables])
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
        await admin.execute(SEED)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=12,
                                        server_settings={'search_path': schema})
        yield pool, admin
    finally:
        if pool:
            await pool.close()
        if engine:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def claim(pool, worker='worker', node='node'):
    async with pool.acquire() as connection:
        return await ExecutionStore(connection).claim_start('command', worker, node, lease_seconds=30)


@pytest.mark.asyncio
async def test_competing_workers_claim_once(database):
    pool, admin = database
    claims = await asyncio.gather(*(claim(pool, f'worker-{i}') for i in range(9)))
    winners = [item for item in claims if item is not None]
    assert len(winners) == 1
    assert winners[0].execution_epoch == 1
    assert await admin.fetchval('SELECT status FROM runtime_commands') == 'claimed'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'


@pytest.mark.asyncio
@pytest.mark.parametrize('column,value', [('storage_node_id', 'other-node'),
    ('active_product_task_id', 'other-task'), ('execution_epoch', 1), ('status', 'running')])
async def test_wrong_affinity_admission_or_epoch_is_not_claimed(database, column, value):
    pool, admin = database
    table = 'conversations' if column == 'active_product_task_id' else 'runtime_sessions'
    await admin.execute(f'UPDATE {table} SET {column}=$1', value)
    assert await claim(pool) is None
    assert await admin.fetchval('SELECT status FROM runtime_commands') == 'accepted'


@pytest.mark.asyncio
async def test_expired_owner_is_not_automatically_reexecuted(database):
    pool, admin = database
    owner = await claim(pool)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    assert await claim(pool, 'replacement') is None
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        with pytest.raises(LostOwnership):
            await store.renew(owner, lease_seconds=30)
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=0, status='completed',
                                      reply_id='reply', snapshot={'state': {}}, events=[{'type': 'end'}])
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 0


@pytest.mark.asyncio
async def test_boundary_state_and_events_are_atomic_and_sequence_is_durable(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        await store.renew(owner, lease_seconds=30)
        result = await store.save_boundary(owner, expected_state_version=0, status='awaiting_interaction',
            reply_id='reply', snapshot={'pending': ['call']}, events=[{'type': 'start'}, {'type': 'ask'}])
        assert result == (1, 2)
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=0, status='completed',
                reply_id='reply', snapshot={}, events=[{'type': 'end'}])
        result = await store.save_boundary(owner, expected_state_version=1, status='completed',
            reply_id='reply', snapshot={'pending': []}, events=[{'type': 'end'}])
        assert result == (2, 3)
    assert [r['runtime_seq'] for r in await admin.fetch('SELECT runtime_seq FROM runtime_events ORDER BY runtime_seq')] == [1, 2, 3]
    assert json.loads(await admin.fetchval('SELECT snapshot FROM runtime_sessions')) == {'pending': []}
    # Terminal persistence does not release the next Product Task's delivery gate.
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 'task'


@pytest.mark.asyncio
async def test_event_failure_rolls_back_snapshot_and_sequence(database):
    pool, admin = database
    owner = await claim(pool)
    await admin.execute("ALTER TABLE runtime_events ADD CHECK (payload->>'type' <> 'fail')")
    async with pool.acquire() as connection:
        import asyncpg
        with pytest.raises(asyncpg.CheckViolationError):
            await ExecutionStore(connection).save_boundary(owner, expected_state_version=0,
                status='completed', reply_id='reply', snapshot={'new': True},
                events=[{'type': 'start'}, {'type': 'fail'}])
    assert await admin.fetchval('SELECT state_version FROM runtime_sessions') == 0
    assert await admin.fetchval('SELECT runtime_seq FROM runtime_sessions') == 0
    assert await admin.fetchval('SELECT snapshot FROM runtime_sessions') is None
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 0


@pytest.mark.asyncio
async def test_other_command_cannot_bypass_session_exclusivity(database):
    pool, admin = database
    await claim(pool)
    await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
        VALUES ('second','session','conversation','other-task','user','StartTask',1,'{}',repeat('d',64))""")
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        assert await store.claim_start('missing', 'worker', 'node', lease_seconds=30) is None
        assert await store.claim_start('second', 'other-worker', 'node', lease_seconds=30) is None
    assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='second'") == 'accepted'


@pytest.mark.asyncio
async def test_stale_epoch_and_wrong_reply_cannot_mutate_state(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        await store.save_boundary(owner, expected_state_version=0, status='awaiting_interaction',
            reply_id='original', snapshot={}, events=[{'type': 'ask'}])
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=1, status='completed',
                reply_id='foreign', snapshot={}, events=[{'type': 'end'}])
        await admin.execute("UPDATE runtime_sessions SET execution_epoch=execution_epoch+1, owner_id='new-owner'")
        with pytest.raises(LostOwnership):
            await store.renew(owner, lease_seconds=30)
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=1, status='completed',
                reply_id='original', snapshot={}, events=[{'type': 'end'}])
    assert await admin.fetchval('SELECT state_version FROM runtime_sessions') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 1


@pytest.mark.asyncio
async def test_terminal_boundary_is_immutable(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        await store.save_boundary(owner, expected_state_version=0, status='completed',
            reply_id='reply', snapshot={}, events=[{'type': 'end'}])
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=1, status='cancelled',
                reply_id='reply', snapshot={}, events=[{'type': 'late-stop'}])
    assert await admin.fetchval('SELECT status FROM runtime_sessions') == 'completed'


@pytest.mark.asyncio
async def test_invalid_input_is_rejected_before_using_database():
    store = ExecutionStore(None)
    for seconds in (0, 301, True, 1.5):
        with pytest.raises(ValueError, match='lease_seconds'):
            await store.claim_start('c', 'worker', 'node', lease_seconds=seconds)
    with pytest.raises(ValueError, match='worker_id'):
        await store.claim_start('c', '', 'node', lease_seconds=30)
    for status, reply, events in [('running', 'r', [{}]), ('completed', None, [{}]),
                                  ('completed', 'r', [])]:
        with pytest.raises(ValueError):
            await store.save_boundary(None, expected_state_version=0, status=status,
                                      reply_id=reply, snapshot={}, events=events)


@pytest.mark.asyncio
async def test_expiry_while_writing_events_rolls_back_boundary(database):
    pool, admin = database
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        owner = await store.claim_start('command', 'worker', 'node', lease_seconds=1)
        await admin.execute("""
            CREATE FUNCTION delay_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM pg_sleep(1.1); RETURN NEW; END $$;
            CREATE TRIGGER delay_insert BEFORE INSERT ON runtime_events
            FOR EACH ROW EXECUTE FUNCTION delay_event();
        """)
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=0, status='completed',
                reply_id='reply', snapshot={}, events=[{'type': 'end'}])
    assert await admin.fetchval('SELECT state_version FROM runtime_sessions') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 0


@pytest.mark.asyncio
async def test_next_task_binds_new_reply_without_erasing_conversation_state(database):
    pool, admin = database
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        owner = await store.claim_start('command', 'worker', 'node', lease_seconds=30)
        await store.save_boundary(owner, expected_state_version=0, status='completed',
            reply_id='reply-a', snapshot={'history': ['a']}, events=[{'type': 'end-a'}])
        # Simulate Product's already-closed delivery/admission transaction.
        async with admin.transaction():
            await admin.execute("UPDATE runtime_sessions SET active_task_id=NULL")
            await admin.execute("UPDATE conversations SET active_product_task_id='task-b'")
            await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
                VALUES ('command-b','session','conversation','task-b','user','StartTask',1,'{}',repeat('d',64))""")
        await admin.execute("INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task-b','session')")
        next_owner = await store.claim_start('command-b', 'worker-b', 'node', lease_seconds=30)
        assert next_owner.execution_epoch == 2
        assert json.loads(await admin.fetchval('SELECT snapshot FROM runtime_sessions')) == {'history': ['a']}
        await store.save_boundary(next_owner, expected_state_version=1, status='completed',
            reply_id='reply-b', snapshot={'history': ['a', 'b']}, events=[{'type': 'end-b'}])
    assert await admin.fetchval('SELECT reply_id FROM runtime_sessions') == 'reply-b'
    assert await admin.fetchval('SELECT runtime_seq FROM runtime_sessions') == 2


@pytest.mark.asyncio
async def test_read_boundary_reconciles_committed_revision_without_replaying(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        assert (await store.load_boundary(owner)).snapshot is None
        await store.save_boundary(owner, expected_state_version=0, status='awaiting_interaction',
            reply_id='reply', snapshot={'pending': ['call']}, events=[{'type': 'ask'}])
        boundary = await store.load_boundary(owner)
        assert (boundary.state_version, boundary.runtime_seq, boundary.reply_id) == (1, 1, 'reply')
        assert boundary.status == 'awaiting_interaction'
        assert boundary.snapshot == {'pending': ['call']}
        await admin.execute("UPDATE runtime_sessions SET owner_id='another-owner', execution_epoch=execution_epoch+1")
        with pytest.raises(LostOwnership):
            await store.load_boundary(owner)


@pytest.mark.asyncio
@pytest.mark.parametrize('statement', [
    "UPDATE tasks SET status='cancelled' WHERE id='task'",
    "UPDATE agents SET deleted_at=now()",
    "UPDATE users SET status='disabled'",
    "UPDATE runtime_commands SET protocol_version=2",
    "UPDATE conversations SET active_product_task_id=NULL",
])
async def test_product_state_is_checked_inside_claim_transaction(database, statement):
    pool, admin = database
    await admin.execute(statement)
    assert await claim(pool) is None
    assert await admin.fetchval('SELECT status FROM runtime_commands') == 'accepted'


@pytest.mark.asyncio
async def test_cross_conversation_command_is_rejected_after_locked_binding_check(database):
    pool, admin = database
    await admin.execute("INSERT INTO conversations(id,user_id,agent_id) VALUES ('foreign','user','agent')")
    await admin.execute("UPDATE runtime_commands SET conversation_id='foreign'")
    assert await claim(pool) is None
    assert await admin.fetchval('SELECT status FROM runtime_commands') == 'accepted'


@pytest.mark.asyncio
async def test_task_history_is_atomic_and_survives_next_task(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        await store.save_boundary(owner, expected_state_version=0, status='awaiting_interaction',
            reply_id='reply-a', snapshot={'pending': ['a']}, events=[{'type': 'ask'}])
        first = await admin.fetchrow('SELECT * FROM runtime_snapshots')
        await store.save_boundary(owner, expected_state_version=1, status='completed',
            reply_id='reply-a', snapshot={'pending': []}, events=[{'type': 'end'}])
        task = await admin.fetchrow("SELECT * FROM runtime_task_executions WHERE task_id='task'")
        assert (task['status'], task['reply_id'], task['state_version']) == ('completed', 'reply-a', 2)
        assert task['snapshot_id'] != first['id']
        assert json.loads(first['payload']) == {'pending': ['a']}
        # Simulate independently proven Product delivery closure, not Worker release.
        await admin.execute('UPDATE runtime_sessions SET active_task_id=NULL')
        await admin.execute("UPDATE conversations SET active_product_task_id='task-b'")
        await admin.execute("INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task-b','session')")
        await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
            VALUES ('command-b','session','conversation','task-b','user','StartTask',1,'{}',repeat('d',64))""")
        second = await store.claim_start('command-b', 'second', 'node', lease_seconds=30)
        assert await admin.fetchval("SELECT input_snapshot_id FROM runtime_task_executions WHERE task_id='task-b'") == task['snapshot_id']
        await store.save_boundary(second, expected_state_version=2, status='completed',
            reply_id='reply-b', snapshot={'history': ['a','b']}, events=[{'type': 'end'}])
        assert await admin.fetchrow("SELECT * FROM runtime_task_executions WHERE task_id='task'") == task
        assert await admin.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1', first['id']) == first
        assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 3


@pytest.mark.asyncio
async def test_missing_execution_fails_closed_without_claim(database):
    pool, admin = database
    await admin.execute('DELETE FROM runtime_task_executions')
    assert await claim(pool) is None
    assert await admin.fetchval('SELECT execution_epoch FROM runtime_sessions') == 0


@pytest.mark.asyncio
async def test_boundary_failure_preserves_task_pointer_and_history(database):
    pool, admin = database
    owner = await claim(pool)
    await admin.execute("ALTER TABLE runtime_events ADD CHECK (payload->>'type' <> 'fail')")
    async with pool.acquire() as connection:
        import asyncpg
        with pytest.raises(asyncpg.CheckViolationError):
            await ExecutionStore(connection).save_boundary(owner, expected_state_version=0,
                status='completed', reply_id='reply', snapshot={'new': True}, events=[{'type': 'fail'}])
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    row = await admin.fetchrow('SELECT status, snapshot_id, state_version FROM runtime_task_executions')
    assert tuple(row) == ('preparing', None, 0)
    assert await admin.fetchval('SELECT snapshot_id FROM runtime_sessions') is None


@pytest.mark.asyncio
async def test_history_read_after_expiry_does_not_grant_execution_rights(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        assert await store.load_task_snapshot('session', 'task') is None
        await store.save_boundary(owner, expected_state_version=0, status='awaiting_interaction',
            reply_id='reply', snapshot={'pending': ['a']}, events=[{'type': 'ask'}])
        before = await store.load_boundary(owner)
        await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        assert await store.load_task_snapshot('session', 'task') == before
        assert await store.load_task_snapshot('foreign-session', 'task') is None
        with pytest.raises(LostOwnership):
            await store.load_boundary(owner)
        with pytest.raises(LostOwnership):
            await store.save_boundary(owner, expected_state_version=1, status='completed',
                reply_id='reply', snapshot={}, events=[{'type': 'end'}])
    assert await claim(pool, 'replacement') is None


@pytest.mark.asyncio
async def test_corrupt_execution_revision_rolls_back_all_boundary_writes(database):
    pool, admin = database
    owner = await claim(pool)
    await admin.execute('UPDATE runtime_task_executions SET state_version=99')
    async with pool.acquire() as connection:
        with pytest.raises(LostOwnership, match='Task execution'):
            await ExecutionStore(connection).save_boundary(owner, expected_state_version=0,
                status='completed', reply_id='reply', snapshot={}, events=[{'type': 'end'}])
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert await admin.fetchval('SELECT state_version FROM runtime_sessions') == 0


@pytest.mark.asyncio
async def test_boundary_takes_task_fk_lock_before_session_to_avoid_claim_deadlock(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as claimant, pool.acquire() as writer:
        pid = await writer.fetchval('SELECT pg_backend_pid()')
        transaction = claimant.transaction()
        await transaction.start()
        await claimant.execute("SELECT id FROM tasks WHERE id='task' FOR UPDATE")
        save = asyncio.create_task(ExecutionStore(writer).save_boundary(owner,
            expected_state_version=0, status='completed', reply_id='reply',
            snapshot={}, events=[{'type': 'end'}]))
        try:
            async with asyncio.timeout(5):
                while await admin.fetchval("SELECT wait_event_type FROM pg_stat_activity WHERE pid=$1", pid) != 'Lock':
                    await asyncio.sleep(0.01)
            # A writer waiting for Task's FK lock must not already hold Session.
            await claimant.execute("SELECT id FROM runtime_sessions WHERE id='session' FOR UPDATE NOWAIT")
        finally:
            await transaction.rollback()
            await save


@pytest.mark.asyncio
async def test_loading_context_uses_immutable_source_not_session_json_cache(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        await store.save_boundary(owner, expected_state_version=0, status='awaiting_interaction',
            reply_id='reply', snapshot={'pending': ['call']}, events=[{'type': 'ask'}])
        await admin.execute("UPDATE runtime_sessions SET snapshot='{}'")
        assert (await store.load_boundary(owner)).snapshot == {'pending': ['call']}
        await admin.execute('UPDATE runtime_sessions SET state_version=99')
        with pytest.raises(LostOwnership):
            await store.load_boundary(owner)


@pytest.mark.asyncio
async def test_begin_reply_binds_task_and_session_once_with_started_event(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        assert await store.begin_reply(owner, 'reply', {'type': 'REPLY_START'}) == 1
        with pytest.raises(LostOwnership):
            await store.begin_reply(owner, 'reply', {'type': 'REPLY_START'})
        with pytest.raises(LostOwnership):
            await store.begin_reply(owner, 'another', {'type': 'REPLY_START'})
    assert tuple(await admin.fetchrow('SELECT status,reply_id,runtime_seq,state_version FROM runtime_sessions')) == ('running','reply',1,0)
    assert tuple(await admin.fetchrow('SELECT status,reply_id,state_version FROM runtime_task_executions')) == ('running','reply',0)
    assert json.loads(await admin.fetchval('SELECT payload FROM runtime_events')) == {'type': 'REPLY_START'}


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['begin_reply','append_events'])
async def test_streaming_write_rolls_back_if_event_insert_fails(database, method):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        if method == 'append_events':
            await store.begin_reply(owner, 'reply', {'type':'start'})
        before = await admin.fetchrow('SELECT status,reply_id,runtime_seq FROM runtime_sessions')
        await admin.execute("ALTER TABLE runtime_events ADD CHECK (payload->>'type' <> 'fail')")
        import asyncpg
        with pytest.raises(asyncpg.CheckViolationError):
            if method == 'begin_reply':
                await store.begin_reply(owner, 'reply', {'type':'fail'})
            else:
                await store.append_events(owner, 'reply', [{'type':'delta'}, {'type':'fail'}])
    assert await admin.fetchrow('SELECT status,reply_id,runtime_seq FROM runtime_sessions') == before
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == before['runtime_seq']


@pytest.mark.asyncio
async def test_append_events_allocates_concurrent_sequences_without_snapshot_revision(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        await ExecutionStore(connection).begin_reply(owner, 'reply', {'type':'start'})
    async def append(index):
        async with pool.acquire() as connection:
            return await ExecutionStore(connection).append_events(owner, 'reply', [{'index':index}])
    assert sorted(await asyncio.gather(*(append(i) for i in range(8)))) == list(range(2,10))
    assert await admin.fetchval('SELECT state_version FROM runtime_sessions') == 0
    assert await admin.fetchval('SELECT state_version FROM runtime_task_executions') == 0
    assert await admin.fetchval('SELECT count(*) FROM runtime_snapshots') == 0
    assert [row['runtime_seq'] for row in await admin.fetch('SELECT runtime_seq FROM runtime_events ORDER BY runtime_seq')] == list(range(1,10))


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    "UPDATE runtime_sessions SET owner_id='other'",
    "UPDATE runtime_sessions SET execution_epoch=execution_epoch+1",
    "UPDATE runtime_sessions SET reply_id='other'",
    "UPDATE runtime_sessions SET status='awaiting_interaction'",
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
    "UPDATE runtime_task_executions SET reply_id='other'",
])
async def test_append_rejects_stale_or_wrong_execution(database, mutation):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        await store.begin_reply(owner, 'reply', {'type':'start'})
        await admin.execute(mutation)
        with pytest.raises(LostOwnership):
            await store.append_events(owner, 'reply', [{'type':'delta'}])
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 1


@pytest.mark.asyncio
async def test_begin_rejects_corrupt_execution_without_partial_binding(database):
    pool, admin = database
    owner = await claim(pool)
    await admin.execute("UPDATE runtime_task_executions SET reply_id='already-bound'")
    async with pool.acquire() as connection:
        with pytest.raises(LostOwnership):
            await ExecutionStore(connection).begin_reply(owner, 'reply', {'type':'start'})
    assert tuple(await admin.fetchrow('SELECT status,reply_id,runtime_seq FROM runtime_sessions')) == ('preparing',None,0)


@pytest.mark.asyncio
@pytest.mark.parametrize('method,value', [
    ('begin_reply', {}), ('begin_reply', []), ('begin_reply', {'number':float('nan')}),
    ('append_events', []), ('append_events', [{}]), ('append_events', ['text']),
    ('append_events', [{'number':float('inf')}]), ('append_events', {'type':'delta'}),
])
async def test_streaming_write_rejects_invalid_events_before_database_access(method, value):
    with pytest.raises(ValueError):
        await getattr(ExecutionStore(None), method)(None, 'reply', value)


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['', ' '*3, None, 1, 'r'*129])
async def test_streaming_write_rejects_invalid_reply_before_database_access(reply):
    for method, payload in [('begin_reply', {'type':'start'}), ('append_events', [{'type':'delta'}])]:
        with pytest.raises(ValueError):
            await getattr(ExecutionStore(None), method)(None, reply, payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['begin_reply','append_events'])
async def test_streaming_write_rechecks_lease_after_event_insertion(database, method):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        if method == 'append_events':
            await store.begin_reply(owner, 'reply', {'type':'start'})
        before = await admin.fetchrow('SELECT status,reply_id,runtime_seq FROM runtime_sessions')
        await admin.execute('''
            CREATE FUNCTION expire_stream_lease() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'; RETURN NEW; END $$;
            CREATE TRIGGER expire_stream_lease BEFORE INSERT ON runtime_events FOR EACH ROW EXECUTE FUNCTION expire_stream_lease();
        ''')
        with pytest.raises(LostOwnership):
            if method == 'begin_reply':
                await store.begin_reply(owner, 'reply', {'type':'start'})
            else:
                await store.append_events(owner, 'reply', [{'type':'delta'}])
    assert await admin.fetchrow('SELECT status,reply_id,runtime_seq FROM runtime_sessions') == before
    assert await admin.fetchval('SELECT count(*) FROM runtime_events') == before['runtime_seq']


@pytest.mark.asyncio
async def test_check_running_is_read_only_and_rejects_preparing_or_expired_owner(database):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as connection:
        store = ExecutionStore(connection)
        with pytest.raises(LostOwnership):
            await store.check_running(owner, 'reply')
        await store.begin_reply(owner, 'reply', {'type':'start'})
        assert await store.check_running(owner, 'reply') is None
        with pytest.raises(LostOwnership):
            await store.check_running(owner, 'other')
        await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        with pytest.raises(LostOwnership):
            await store.check_running(owner, 'reply')
    assert await admin.fetchval('SELECT runtime_seq FROM runtime_sessions') == 1
    assert await admin.fetchval('SELECT state_version FROM runtime_sessions') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['begin_reply','append_events'])
async def test_streaming_writer_takes_task_lock_before_session(database, method):
    pool, admin = database
    owner = await claim(pool)
    async with pool.acquire() as claimant, pool.acquire() as writer:
        store = ExecutionStore(writer)
        if method == 'append_events':
            await store.begin_reply(owner, 'reply', {'type':'start'})
        pid = await writer.fetchval('SELECT pg_backend_pid()')
        transaction = claimant.transaction()
        await transaction.start()
        await claimant.execute("SELECT id FROM tasks WHERE id='task' FOR UPDATE")
        payload = {'type':'start'} if method == 'begin_reply' else [{'type':'delta'}]
        pending = asyncio.create_task(getattr(store, method)(owner, 'reply', payload))
        try:
            async with asyncio.timeout(5):
                while await admin.fetchval('SELECT wait_event_type FROM pg_stat_activity WHERE pid=$1', pid) != 'Lock':
                    await asyncio.sleep(0.01)
            await claimant.execute("SELECT id FROM runtime_sessions WHERE id='session' FOR UPDATE NOWAIT")
        finally:
            await transaction.rollback()
            await pending


@pytest.mark.asyncio
@pytest.mark.parametrize('status,worker,allowed',[
    ('held','warm',True),('held','peer',False),('releasing','warm',False),
    ('blocked','warm',False),('released','peer',True)])
async def test_workspace_residency_fences_start_claim(database,status,worker,allowed):
    pool,admin=database
    await admin.execute("""INSERT INTO workspace_residencies(session_id,storage_node_id,workspace_generation,
        holder_worker_id,hold_version,status,release_request_id,last_task_id,last_execution_epoch)
        VALUES ('session','node','directory','warm',1,$1::varchar,
        CASE WHEN $1::varchar='held' THEN NULL ELSE 'release' END,'task',1)""",status)
    owner=await claim(pool,worker)
    assert (owner is not None)==allowed
    assert await admin.fetchval('SELECT status FROM runtime_commands')==('claimed' if allowed else 'accepted')

@pytest.mark.asyncio
async def test_superseded_session_fences_old_owner_but_new_session_can_execute(database):
    pool, admin = database
    async with pool.acquire() as db:
        store = ExecutionStore(db)
        owner = await store.claim_start('command', 'worker', 'node', lease_seconds=30)
        await store.begin_reply(owner, 'reply', {'type': 'REPLY_START'})
        await admin.execute("UPDATE runtime_sessions SET superseded_at=clock_timestamp() WHERE id='session'")
        for operation in (
            lambda: store.renew(owner, lease_seconds=30),
            lambda: store.load_boundary(owner),
            lambda: store.check_running(owner, 'reply'),
            lambda: store.append_events(owner, 'reply', [{'type': 'REPLY_END'}]),
            lambda: store.save_boundary(owner, expected_state_version=0, status='completed',
                reply_id='reply', snapshot={}, events=[{'type': 'REPLY_END'}]),
        ):
            with pytest.raises(LostOwnership):
                await operation()
        assert await admin.fetchval('SELECT count(*) FROM runtime_events') == 1
        await admin.execute("""
            UPDATE conversations SET active_product_task_id='task-b';
            INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('new-session','conversation','node');
            INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('task-b','new-session');
            INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,expected_epoch,payload,content_digest)
            VALUES ('new-command','new-session','conversation','task-b','user','StartTask',0,'{}',repeat('d',64));
        """)
        new_owner = await store.claim_start('new-command', 'new-worker', 'node', lease_seconds=30)
        assert new_owner.session_id == 'new-session'
        assert await store.begin_reply(new_owner, 'new-reply', {'type': 'REPLY_START'}) == 1
        await store.renew(new_owner, lease_seconds=30)
        assert await admin.fetchval("SELECT owner_id FROM runtime_sessions WHERE id='session'") == 'worker'
