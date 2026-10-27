"""Product command admission on a disposable PostgreSQL schema with current runtime tables."""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from workmate_contracts import Command
from workmate_product.application.commands import accept_start, CommandConflict, CommandRejected


async def allow_agent(db, actor_id, agent_id):
    return True


def command(**changes):
    return Command.model_validate({
        'command_id': 'cmd', 'kind': 'StartTask', 'conversation_id': 'c',
        'runtime_session_id': 's', 'product_task_id': 't', 'payload': {'input': 'hello'},
        **changes,
    })


@pytest_asyncio.fixture
async def database():
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires an isolated PostgreSQL database')
    import asyncpg
    schema = 'admit_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    engine = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        # Minimal product facts isolate command admission from other services.
        await admin.execute('''
            CREATE TABLE users (id varchar(36) PRIMARY KEY, status varchar(16) NOT NULL);
            CREATE TABLE agents (id varchar(36) PRIMARY KEY, deleted_at timestamptz);
            CREATE TABLE conversations (id varchar(36) PRIMARY KEY,
                user_id varchar(36) REFERENCES users, agent_id varchar(36) REFERENCES agents);
            CREATE TABLE tasks (id varchar(36) PRIMARY KEY,
                conversation_id varchar(36) REFERENCES conversations, status varchar(32) NOT NULL, run_id varchar(128));
            CREATE TABLE task_messages (id text PRIMARY KEY, task_id text REFERENCES tasks, content text);
            INSERT INTO users VALUES ('u','active'), ('other','active');
            INSERT INTO agents VALUES ('a',NULL);
            INSERT INTO conversations VALUES ('c','u','a');
        ''')
        engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
            connect_args={'server_settings': {'search_path': schema}})
        from workmate_product.persistence.models import Base
        def upgrade(connection):
            connection.exec_driver_sql("ALTER TABLE conversations ADD COLUMN active_product_task_id varchar(36) REFERENCES tasks(id), ADD COLUMN deleted_at timestamptz, ADD COLUMN product_seq bigint NOT NULL DEFAULT 0, ADD COLUMN business_seq bigint NOT NULL DEFAULT 0")
            tables = ['runtime_sessions', 'runtime_commands', 'runtime_events', 'runtime_snapshots', 'runtime_task_executions', 'conversation_retirements', 'command_deliveries', 'command_delivery_quarantines', 'workspace_residencies', 'workspace_release_requests', 'workspace_checkpoints', 'recovery_forks', 'runtime_projection_cursors', 'task_projection_states', 'product_events', 'conversation_event_logs', 'realtime_business_outbox']
            Base.metadata.create_all(connection, tables=[Base.metadata.tables[name] for name in tables])
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
        yield async_sessionmaker(engine, expire_on_commit=False), admin
    finally:
        if engine:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def create_task(db, task_id='t'):
    await db.execute(text("INSERT INTO tasks(id,conversation_id,status) VALUES (:id,'c','created')"), {'id': task_id})
    await db.execute(text("INSERT INTO task_messages VALUES (:id,:id,'hello')"), {'id': task_id})


@pytest.mark.asyncio
async def test_task_message_and_command_commit_once_with_idempotent_retry(database):
    sessions, admin = database
    async with sessions.begin() as db:
        first = await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                                   authorize_agent=allow_agent, prepare_task=create_task)
        # Service must not commit caller's transaction.
        assert await admin.fetchval('SELECT count(*) FROM tasks') == 0
    async with sessions.begin() as db:
        retry = await accept_start(db, command(), actor_user_id='u', storage_node_id='node', authorize_agent=allow_agent)
    assert first.command_id == retry.command_id == 'cmd'
    assert retry.duplicate is True
    assert await admin.fetchval('SELECT count(*) FROM task_messages') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_task_executions') == 1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'


@pytest.mark.asyncio
async def test_failure_after_admission_rolls_back_task_message_pointer_and_command(database):
    sessions, admin = database
    with pytest.raises(RuntimeError, match='injected'):
        async with sessions.begin() as db:
            await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                               authorize_agent=allow_agent, prepare_task=create_task)
            raise RuntimeError('injected caller failure')
    for table in ('tasks','task_messages','runtime_sessions','runtime_commands','runtime_task_executions'):
        assert await admin.fetchval(f'SELECT count(*) FROM {table}') == 0
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None


@pytest.mark.asyncio
async def test_digest_conflict_does_not_replace_original_payload(database):
    sessions, admin = database
    async with sessions.begin() as db:
        await create_task(db)
        await accept_start(db, command(), actor_user_id='u', storage_node_id='node', authorize_agent=allow_agent)
    with pytest.raises(CommandConflict):
        async with sessions.begin() as db:
            await accept_start(db, command(payload={'input': 'changed'}), actor_user_id='u',
                               storage_node_id='node', authorize_agent=allow_agent)
    assert await admin.fetchval("SELECT payload->>'input' FROM runtime_commands") == 'hello'


@pytest.mark.asyncio
async def test_concurrent_retries_create_one_command(database):
    sessions, admin = database
    async def accept():
        async with sessions.begin() as db:
            return await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                                      authorize_agent=allow_agent, prepare_task=create_task)
    results = await asyncio.gather(*(accept() for _ in range(8)))
    assert sum(not result.duplicate for result in results) == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_task_executions') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['actor', 'permission', 'deleted', 'cancelled', 'other_active'])
async def test_invalid_authority_or_product_state_rejects_command(database, case):
    sessions, admin = database
    async with sessions.begin() as db:
        await create_task(db)
        if case == 'deleted':
            await db.execute(text('UPDATE agents SET deleted_at=now()'))
        if case == 'cancelled':
            await db.execute(text("UPDATE tasks SET status='cancelled'"))
        if case == 'other_active':
            await create_task(db, 'other-task')
            await db.execute(text("UPDATE conversations SET active_product_task_id='other-task'"))
    async def denied(db, actor, agent):
        return False
    with pytest.raises(CommandRejected):
        async with sessions.begin() as db:
            await accept_start(db, command(), actor_user_id='other' if case=='actor' else 'u',
                               storage_node_id='node', authorize_agent=denied if case=='permission' else allow_agent)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 0


@pytest.mark.asyncio
async def test_idempotent_retry_skips_new_task_and_capacity_callback(database):
    sessions, admin = database
    async with sessions.begin() as db:
        await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                           authorize_agent=allow_agent, prepare_task=create_task)
    async def capacity_exhausted(db):
        raise AssertionError('Retry must not create a Task or re-run capacity checks')
    async with sessions.begin() as db:
        duplicate = await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                                       authorize_agent=allow_agent, prepare_task=capacity_exhausted)
    assert duplicate.duplicate


@pytest.mark.asyncio
async def test_second_task_admission_rolls_back_while_delivery_gate_is_open(database):
    sessions, admin = database
    async with sessions.begin() as db:
        await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                           authorize_agent=allow_agent, prepare_task=create_task)
    async def create_second(db):
        await create_task(db, 'second')
    with pytest.raises(CommandRejected):
        async with sessions.begin() as db:
            await accept_start(db, command(command_id='second-cmd', product_task_id='second'),
                               actor_user_id='u', storage_node_id='node', authorize_agent=allow_agent,
                               prepare_task=create_second)
    assert await admin.fetchval('SELECT count(*) FROM tasks') == 1
    assert await admin.fetchval('SELECT count(*) FROM task_messages') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands') == 1
    assert await admin.fetchval('SELECT count(*) FROM runtime_task_executions') == 1


@pytest.mark.asyncio
async def test_product_admission_and_worker_claim_share_official_schema(database):
    from workmate_runtime.transport.postgres import ExecutionStore
    sessions, admin = database
    async with sessions.begin() as db:
        await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                           authorize_agent=allow_agent, prepare_task=create_task)
    store = ExecutionStore(admin)
    owner = await store.claim_start('cmd', 'worker', 'node', lease_seconds=30)
    assert owner.task_id == 't' and owner.execution_epoch == 1
    record = await admin.fetchrow('SELECT status,claimed_by,claimed_epoch,claimed_at FROM runtime_commands')
    assert record['status'] == 'claimed' and record['claimed_by'] == 'worker'
    assert record['claimed_epoch'] == 1 and record['claimed_at'] is not None
    await store.save_boundary(owner, expected_state_version=0, status='completed', reply_id='reply',
                              snapshot={'state': {}}, events=[{'type': 'end'}])
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'


def test_product_package_import_is_independent_of_web_and_engine():
    import subprocess
    import sys
    probe = '''
import sys
import importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'agentscope', 'app'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from workmate_product.application.commands import accept_start
from workmate_product.projections.store import consume_next, read_product_events
from workmate_product.application.control_commands import accept_resume, accept_stop
'''
    completed = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
async def test_existing_session_epoch_affinity_and_gate_are_checked(database):
    sessions, admin = database
    await admin.execute("INSERT INTO runtime_sessions(id,conversation_id,storage_node_id,execution_epoch) VALUES ('s','c','node',2)")
    with pytest.raises(CommandRejected):
        async with sessions.begin() as db:
            await accept_start(db, command(), actor_user_id='u', storage_node_id='node',
                               authorize_agent=allow_agent, prepare_task=create_task)
    assert await admin.fetchval('SELECT count(*) FROM tasks') == 0
    async with sessions.begin() as db:
        accepted = await accept_start(db, command(expected_epoch=2), actor_user_id='u', storage_node_id='node',
                                      authorize_agent=allow_agent, prepare_task=create_task)
    assert accepted.command_id == 'cmd'


@pytest.mark.asyncio
async def test_unowned_transaction_and_other_command_kind_are_rejected():
    class NoTransaction:
        def in_transaction(self):
            return False
    with pytest.raises(CommandRejected):
        await accept_start(NoTransaction(), command(), actor_user_id='u', storage_node_id='node', authorize_agent=allow_agent)
    with pytest.raises(CommandRejected):
        await accept_start(None, command(kind='StopTask'), actor_user_id='u', storage_node_id='node', authorize_agent=allow_agent)
