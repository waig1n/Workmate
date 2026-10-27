"""Fresh PostgreSQL initialization and refusal boundaries."""
import importlib.util
import io
import os
from pathlib import Path
import uuid

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine


VERSIONS = Path(__file__).parents[1] / 'alembic' / 'versions'


def initial_migration():
    spec = importlib.util.spec_from_file_location('initial_schema', VERSIONS / '0001_initial_schema.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def schema_database():
    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires isolated PostgreSQL')
    import asyncpg
    schema = 'initial_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    engine = create_async_engine(dsn.replace('postgresql://', 'postgresql+asyncpg://'),
        connect_args={'server_settings': {'search_path': schema}})
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        yield engine, admin, initial_migration()
    finally:
        await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def apply(engine, operation):
    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            operation()
    async with engine.begin() as connection:
        await connection.run_sync(run)


@pytest_asyncio.fixture
async def schema_connection(schema_database):
    engine, admin, migration = schema_database
    await apply(engine, migration.upgrade)
    yield admin






def test_single_initial_revision_is_frozen_and_offline_renderable():
    config = Config()
    config.set_main_option('script_location', str(VERSIONS.parent))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1
    assert revisions[0].revision == '0001_initial_schema'
    assert revisions[0].down_revision is None
    source = (VERSIONS / '0001_initial_schema.py').read_text()
    assert 'workmate_product' not in source
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql',
        opts={'as_sql': True, 'output_buffer': buffer})
    with Operations.context(context):
        initial_migration().upgrade()
    assert 'CREATE TABLE tasks' in buffer.getvalue()
    assert 'CREATE TRIGGER' in buffer.getvalue()


async def test_initialization_has_only_product_catalog_seed_data(schema_connection):
    db = schema_connection
    assert await db.fetchval('SELECT count(*) FROM users') == 0
    assert await db.fetchval('SELECT count(*) FROM tasks') == 0
    assert await db.fetchval('SELECT count(*) FROM resource_shares') == 0
    assert await db.fetchval('SELECT count(*) FROM role_bindings') == 0
    assert await db.fetchval("SELECT count(*) FROM roles WHERE key='admin' AND is_system") == 1
    assert await db.fetchval('SELECT count(*) FROM role_permissions') == await db.fetchval('SELECT count(*) FROM permissions')
    assert await db.fetchval("SELECT to_regclass('rbac_migration_state')") is None


async def test_initialization_rejects_populated_schema_without_mutation(schema_database):
    engine, db, migration = schema_database
    await db.execute("CREATE TABLE existing_data(value text); INSERT INTO existing_data VALUES ('keep')")
    with pytest.raises(RuntimeError, match='empty database schema'):
        await apply(engine, migration.upgrade)
    assert await db.fetchval('SELECT value FROM existing_data') == 'keep'
    assert await db.fetchval("SELECT to_regclass('tasks')") is None


async def test_downgrade_refuses_to_destroy_initialized_schema(schema_database):
    engine, db, migration = schema_database
    await apply(engine, migration.upgrade)
    with pytest.raises(RuntimeError, match='cannot be downgraded'):
        await apply(engine, migration.downgrade)
    assert await db.fetchval("SELECT to_regclass('tasks')") is not None


async def test_current_model_columns_exist_in_initialized_database(schema_connection):
    from workmate_product.persistence.models import Base
    actual = {}
    for row in await schema_connection.fetch('''SELECT table_name,column_name
            FROM information_schema.columns WHERE table_schema=current_schema()'''):
        actual.setdefault(row['table_name'], set()).add(row['column_name'])
    assert set(actual) == set(Base.metadata.tables)
    for table in Base.metadata.tables.values():
        assert actual[table.name] == set(table.c.keys()), table.name


async def test_bootstrap_after_initialization_is_idempotent_and_grants_no_users(schema_database):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services.bootstrap import ensure_bootstrap
    from app.auth.permissions import ROLE_GRANTS
    engine, db, migration = schema_database
    await apply(engine, migration.upgrade)
    sessions = async_sessionmaker(engine)
    async with sessions() as session:
        await ensure_bootstrap(session)
        await ensure_bootstrap(session)
    granted = {row['key'] for row in await db.fetch('''
        SELECT p.key FROM permissions p
        JOIN role_permissions rp ON rp.permission_id=p.id
        JOIN roles r ON r.id=rp.role_id WHERE r.key='admin' ''')}
    assert granted == set(ROLE_GRANTS['admin'])
    assert await db.fetchval("SELECT count(*) FROM scopes WHERE kind='organization'") == 1
    assert await db.fetchval('SELECT count(*) FROM role_bindings') == 0
    assert await db.fetchval('SELECT count(*) FROM users') == 0
