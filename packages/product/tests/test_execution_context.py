"""Product execution facts and immutable admission specs on real PostgreSQL."""
import asyncio
import importlib.util
import os
from pathlib import Path
import uuid

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from workmate_product.application.execution_context import (
    ExecutionContextRejected, SelectedModel, record_execution_spec, read_execution_context,
)
from workmate_product.persistence.models import (
    Base, User, Scope, ScopeClosure, ScopeMembership, Agent, Conversation, Task,
    TaskMessage,
)

MODEL = SelectedModel(profile_name='chat', model_type='openai', model='model',
                      api_key_env='CHAT_MODEL_API_KEY', base_url_env='CHAT_BASE_URL')


@pytest_asyncio.fixture
async def database():
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires isolated PostgreSQL')
    import asyncpg
    schema = 'context_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    engine = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        engine = create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),
            connect_args={'server_settings': {'search_path':schema}})
        async with engine.begin() as connection:
            path = Path(__file__).resolve().parents[3] / 'backend/alembic/versions/0001_initial_schema.py'
            spec = importlib.util.spec_from_file_location('initial_schema', path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            def initialize(c):
                with Operations.context(MigrationContext.configure(c)):
                    migration.upgrade()
            await connection.run_sync(initialize)
            # Authorization cases explicitly define their own catalog and grants.
            for table in ('role_permissions', 'roles', 'permissions', 'scope_closure', 'scopes'):
                await connection.execute(text(f'DELETE FROM {table}'))
        from test_skill_blob_storage import MemoryBlobStore
        sessions = async_sessionmaker(engine,expire_on_commit=False,
            info={'skill_blob_store': MemoryBlobStore()})
        async with sessions.begin() as db:
            db.add(User(id='u',email='u@example.test',password_hash='x',display_name='User'))
            await db.flush()
            db.add(Scope(id='scope',kind='personal',owner_user_id='u',name='Personal'))
            await db.flush()
            db.add_all([ScopeClosure(ancestor_id='scope',descendant_id='scope',depth=0),
                        ScopeMembership(scope_id='scope',user_id='u'),
                        Agent(id='a',slug='agent',display_name='Agent',scope_id='scope',
                              tool_policy={'denied_tools':['initial_tool']},system_prompt='Be helpful')])
            await db.flush()
            db.add(Conversation(id='c',user_id='u',agent_id='a',model_profile='chat'))
            await db.flush()
            db.add(Task(id='t',conversation_id='c',title='Task',approval_profile='standard'))
            await db.flush()
            db.add(TaskMessage(id='message',task_id='t',conversation_id='c',role='user',content={'text':'hello'}))
            await db.execute(text("UPDATE conversations SET active_product_task_id='t'"))
            await db.execute(text("INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('s','c','node')"))
            await db.execute(text("INSERT INTO runtime_task_executions(task_id,session_id) VALUES ('t','s')"))
            await db.execute(text("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
                VALUES ('cmd','s','c','t','u','StartTask','{"tool_policy":{},"input":"forged"}',repeat('a',64))"""))
        yield sessions,admin
    finally:
        if engine:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def record(sessions):
    async with sessions.begin() as db:
        return await record_execution_spec(db,task_id='t',session_id='s',actor_user_id='u',
                                           selected_model=MODEL,knowledge_enabled=False)


async def claim(admin):
    await admin.execute("""UPDATE runtime_sessions SET owner_id='worker',execution_epoch=1,
        active_task_id='t',status='preparing',lease_until=clock_timestamp()+interval '30 seconds';
        UPDATE runtime_task_executions SET execution_epoch=1,status='preparing';
        UPDATE runtime_commands SET status='claimed',claimed_by='worker',claimed_epoch=1;""")


async def read(sessions, **overrides):
    async with sessions.begin() as db:
        return await read_execution_context(db,**dict(session_id='s',task_id='t',worker_id='worker',
            execution_epoch=1,storage_node_id='node',knowledge_enabled=False,**overrides))


@pytest.mark.asyncio
async def test_spec_and_product_input_are_trusted_separate_from_command_payload(database):
    sessions,admin = database
    spec = await record(sessions)
    await admin.execute("UPDATE agents SET tool_policy='{" + '"denied_tools":["current_tool"]' + "}'")
    await claim(admin)
    context = await read(sessions)
    assert context.initial_spec == spec
    assert context.initial_spec.tool_policy.denied_tools == ('initial_tool',)
    assert context.current_tool_policy.denied_tools == ('current_tool',)
    assert context.effective_tool_policy.denied_tools == ('current_tool','initial_tool')
    assert context.input_messages[0].content == {'text':'hello'}
    assert context.actor_user_id == 'u' and context.agent_id == 'a'
    assert context.initial_spec.selected_model == MODEL


@pytest.mark.asyncio
async def test_missing_initial_spec_fails_closed(database):
    sessions,admin = database
    await claim(admin)
    with pytest.raises(ExecutionContextRejected,match='spec'):
        await read(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    "UPDATE users SET status='disabled'", "UPDATE agents SET deleted_at=now()",
    "UPDATE scopes SET owner_user_id=NULL", "UPDATE conversations SET active_product_task_id=NULL",
    "UPDATE runtime_sessions SET active_task_id=NULL", "UPDATE runtime_sessions SET execution_epoch=2",
    "UPDATE runtime_sessions SET owner_id='foreign'", "UPDATE runtime_sessions SET storage_node_id='foreign'",
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
    "UPDATE runtime_task_executions SET execution_epoch=2",
    "UPDATE runtime_task_executions SET state_version=1",
    "UPDATE runtime_task_executions SET reply_id='foreign'",
    "UPDATE tasks SET status='cancelled'",
])
async def test_stale_or_revoked_context_is_rejected(database,mutation):
    sessions,admin = database
    await record(sessions)
    await claim(admin)
    await admin.execute(mutation)
    with pytest.raises(ExecutionContextRejected):
        await read(sessions)


@pytest.mark.asyncio
async def test_spec_is_caller_transactional_and_immutable(database):
    sessions,admin = database
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await record_execution_spec(db,task_id='t',session_id='s',actor_user_id='u',
                                        selected_model=MODEL,knowledge_enabled=False)
            assert await admin.fetchval('SELECT count(*) FROM runtime_execution_specs') == 0
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM runtime_execution_specs') == 0
    await record(sessions)
    import asyncpg
    for sql in ("UPDATE runtime_execution_specs SET digest=repeat('b',64)",
                'DELETE FROM runtime_execution_specs'):
        with pytest.raises(asyncpg.RaiseError,match='immutable'):
            await admin.execute(sql)


@pytest.mark.asyncio
async def test_concurrent_record_retries_preserve_one_spec_and_initial_policy(database):
    sessions,admin = database
    specs = await asyncio.gather(*(record(sessions) for _ in range(4)))
    assert all(spec == specs[0] for spec in specs)
    await admin.execute("UPDATE agents SET tool_policy='{}'")
    assert await record(sessions) == specs[0]
    assert await admin.fetchval('SELECT count(*) FROM runtime_execution_specs') == 1
    await claim(admin)
    with pytest.raises(ExecutionContextRejected,match='claimed'):
        await record(sessions)


def test_selected_model_rejects_literal_secrets_and_unknown_config():
    from pydantic import ValidationError
    for extras in ({'api_key':'secret'},{'base_url':'https://secret@example.test'}, {'parameters':{}}):
        with pytest.raises(ValidationError):
            SelectedModel(**{**MODEL.model_dump(),**extras})


async def bind_capability(sessions, identity, *, ready=True, grant=True):
    from workmate_product.persistence.models import (
        AgentCapabilityBinding, CapabilityInstallation, ResourceShare)
    async with sessions.begin() as db:
        if await db.get(Scope,'org') is None:
            db.add(Scope(id='org',kind='organization',name='Org'))
            await db.flush()
            db.add_all([ScopeClosure(ancestor_id='org',descendant_id='org',depth=0),
                        ScopeMembership(scope_id='org',user_id='u')])
            await db.flush()
        db.add(CapabilityInstallation(id=identity,capability_type='skill',scope_id='org',
            name=identity,enabled=True))
        await db.flush()
        if grant:
            db.add(ResourceShare(id=f'grant-{identity}',capability_id=identity,
                                 target_scope_id='org',created_by='u'))
        db.add(AgentCapabilityBinding(agent_id='a',capability_installation_id=identity))
        if ready:
            from workmate_product.application.skill_repository import record_skill_artifact
            from workmate_product.persistence.models import CapabilitySkillArtifact
            from test_skill_artifacts import archive
            artifact=await record_skill_artifact(db,archive({'SKILL.md':
                f'---\nname: {identity}\ndescription: Fixture Skill\n---\nWork.'}))
            db.add(CapabilitySkillArtifact(capability_id=identity,artifact_digest=artifact.digest))


@pytest.mark.asyncio
async def test_capability_ceiling_excludes_additions_and_rejects_removed_original(database):
    sessions,admin = database
    await bind_capability(sessions,'original')
    await record(sessions)
    await bind_capability(sessions,'added')
    await claim(admin)
    context = await read(sessions)
    assert [c.id for c in context.capabilities] == ['original']
    await admin.execute("DELETE FROM agent_capability_bindings WHERE capability_installation_id='original'")
    with pytest.raises(ExecutionContextRejected,match='removed'):
        await read(sessions)


@pytest.mark.asyncio
async def test_disabled_capability_fails_current_authority(database):
    sessions,admin = database
    await bind_capability(sessions,'original')
    await record(sessions)
    await claim(admin)
    await admin.execute('UPDATE capability_installations SET enabled=false')
    with pytest.raises(ExecutionContextRejected,match='unavailable'):
        await read(sessions)


@pytest.mark.asyncio
async def test_model_and_platform_profile_cannot_silently_change_initial_spec(database):
    sessions,admin = database
    await record(sessions)
    async with sessions.begin() as db:
        with pytest.raises(ExecutionContextRejected,match='conflicts'):
            await record_execution_spec(db,task_id='t',session_id='s',actor_user_id='u',
                selected_model=MODEL.model_copy(update={'model':'different'}))
    await claim(admin)
    await admin.execute("UPDATE agents SET platform_tool_profile='workspace'")
    with pytest.raises(ExecutionContextRejected,match='profile'):
        await read(sessions)


@pytest.mark.asyncio
async def test_old_identity_map_does_not_hide_revocation(database):
    sessions,admin = database
    await record(sessions)
    await claim(admin)
    async with sessions.begin() as db:
        cached = await db.get(Agent,'a')
        await admin.execute('UPDATE agents SET deleted_at=now()')
        with pytest.raises(ExecutionContextRejected):
            await read_execution_context(db,session_id='s',task_id='t',worker_id='worker',
                execution_epoch=1,storage_node_id='node')
        assert cached.deleted_at is None  # Caller identity map is intentionally untouched.


def test_shared_context_import_does_not_load_web_or_engine():
    from test_shared_authorization import run_python
    run_python('''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi','agentscope','app','backend','workmate_runtime'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0,Reject())
from workmate_product.application.execution_context import read_execution_context,record_execution_spec
''')


@pytest.mark.asyncio
async def test_knowledge_requires_current_enablement_and_original_bindings(database):
    from workmate_product.persistence.models import KnowledgeBase
    sessions,admin = database
    async with sessions.begin() as db:
        db.add_all([KnowledgeBase(id=key,scope_id='scope',name=key,runtime_id=key,
                                 runtime_owner_id='u',enabled=True) for key in ('kb1','kb2')])
        agent = await db.get(Agent,'a')
        agent.knowledge_config = {'knowledge_base_ids':['kb1']}
    with pytest.raises(ExecutionContextRejected,match='unavailable'):
        await record(sessions)
    async with sessions.begin() as db:
        await record_execution_spec(db,task_id='t',session_id='s',actor_user_id='u',
                                    selected_model=MODEL,knowledge_enabled=True)
    await claim(admin)
    await admin.execute("UPDATE agents SET knowledge_config='{" + '"knowledge_base_ids":["kb1","kb2"]' + "}'")
    async with sessions.begin() as db:
        context = await read_execution_context(db,session_id='s',task_id='t',worker_id='worker',
            execution_epoch=1,storage_node_id='node',knowledge_enabled=True)
    assert context.knowledge_config.knowledge_base_ids == ['kb1']
    with pytest.raises(ExecutionContextRejected,match='unavailable'):
        await read(sessions)
    await admin.execute("UPDATE agents SET knowledge_config='{}'")
    async with sessions.begin() as db:
        with pytest.raises(ExecutionContextRejected,match='removed'):
            await read_execution_context(db,session_id='s',task_id='t',worker_id='worker',
                execution_epoch=1,storage_node_id='node',knowledge_enabled=True)


@pytest.mark.asyncio
async def test_cached_scope_ownership_cannot_hide_revocation(database):
    sessions,admin = database
    await record(sessions)
    await claim(admin)
    async with sessions.begin() as db:
        cached = await db.get(Scope,'scope')
        await admin.execute('UPDATE scopes SET owner_user_id=NULL')
        with pytest.raises(ExecutionContextRejected,match='access'):
            await read_execution_context(db,session_id='s',task_id='t',worker_id='worker',
                execution_epoch=1,storage_node_id='node')
        assert cached.owner_user_id == 'u'


@pytest.mark.asyncio
async def test_initial_schema_spec_columns_and_immutability(database):
    from sqlalchemy import inspect
    from workmate_product.persistence.models import RuntimeExecutionSpec
    sessions,admin = database
    async with sessions.kw['bind'].begin() as connection:
        def roundtrip(c):
            assert set(col['name'] for col in inspect(c).get_columns('runtime_execution_specs')) == set(RuntimeExecutionSpec.__table__.columns.keys())
        await connection.run_sync(roundtrip)
    assert await admin.fetchval('SELECT count(*) FROM runtime_execution_specs') == 0
    assert await admin.fetchval('SELECT count(*) FROM tasks') == 1
    await record(sessions)
    import asyncpg
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await admin.execute('DELETE FROM runtime_execution_specs')


def test_sqlite_spec_immutability_matches_postgres():
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from workmate_product.persistence.models import RuntimeExecutionSpec
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE tasks(id VARCHAR(36) PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE runtime_sessions(id VARCHAR(128) PRIMARY KEY)')
        RuntimeExecutionSpec.__table__.create(connection)
        connection.exec_driver_sql("INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest) VALUES ('t','s',1,'{}',?)",('d'*64,))
        for sql in ('UPDATE runtime_execution_specs SET schema_version=1','DELETE FROM runtime_execution_specs'):
            with pytest.raises(IntegrityError,match='immutable'):
                connection.exec_driver_sql(sql)
    engine.dispose()


@pytest.mark.asyncio
async def test_durable_upgrade_keeps_initial_spec_and_initial_deny_ceiling(database):
    sessions,admin=database
    original=await record(sessions)
    await claim(admin)
    await admin.execute("""UPDATE runtime_sessions SET status='awaiting_interaction',reply_id='reply',state_version=1;
        UPDATE runtime_task_executions SET status='awaiting_interaction',reply_id='reply',state_version=1;
        UPDATE tasks SET status='awaiting_permission',run_id='reply';
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest,
            reply_id,expected_epoch) VALUES ('upgrade','s','c','t','u','ResumeInteraction',
            '{"full_access":true,"decision_version":"upgrade","state_version":1}',repeat('a',64),'reply',1);
        INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
            VALUES ('t','s','worker',1,'workspace');
        INSERT INTO runtime_permission_upgrades(task_id,session_id,command_id,permission_version,state_version,
            actor_user_id,worker_id,execution_epoch,initial_profile)
            VALUES ('t','s','upgrade','upgrade',1,'u','worker',1,'standard');""")
    context=await read(sessions)
    assert context.initial_spec==original
    assert context.effective_approval_profile=='full_access' and context.permission_version=='upgrade'
    assert 'initial_tool' in context.effective_tool_policy.denied_tools
    await admin.execute("UPDATE users SET status='disabled' WHERE id='u'")
    with pytest.raises(ExecutionContextRejected):
        await read(sessions)
