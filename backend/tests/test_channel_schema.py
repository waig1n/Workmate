"""Initial PostgreSQL schema enforces channel admission and provenance."""
import importlib.util
import os
from pathlib import Path
import uuid
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

@pytest_asyncio.fixture
async def migrated():
    dsn=os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn: pytest.skip('requires isolated PostgreSQL')
    import asyncpg
    admin=await asyncpg.connect(dsn)
    schema='channel_migration_'+uuid.uuid4().hex
    engine=None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        engine=create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),connect_args={'server_settings':{'search_path':schema}})
        spec=importlib.util.spec_from_file_location('channels_migration',Path(__file__).parents[1]/'alembic/versions/0001_initial_schema.py')
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        async def apply(operation):
            def run(conn):
                with Operations.context(MigrationContext.configure(conn)): operation()
            async with engine.begin() as conn: await conn.run_sync(run)
        await apply(migration.upgrade)
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from workmate_product.persistence.models import User, Scope, Agent, Conversation, Task
        async with async_sessionmaker(engine).begin() as db:
            db.add_all([User(id='u',email='u@example.test',password_hash='fixture',display_name='User'),
                        Scope(id='s',kind='organization',name='Example')])
            await db.flush()
            db.add(Agent(id='a',slug='example',display_name='Example',scope_id='s'))
            await db.flush()
            db.add(Conversation(id='c',user_id='u',agent_id='a'))
            await db.flush()
            db.add(Task(id='old',conversation_id='c',title='Example'))
        yield admin,migration,apply
    finally:
        if engine: await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()

async def seed(db):
    await db.execute('''INSERT INTO channel_connections(id,name,provider,agent_id,scope_id) VALUES ('ch','Bot','wecom','a','s');
        INSERT INTO channel_inbox(id,channel_id,channel_version,message_id,sender_id,chat_id,chat_type,text,target) VALUES ('i','ch',1,'msg','ext','private','private','hello','{}');
        INSERT INTO tasks(conversation_id,title,id,origin,approval_profile,channel_inbox_id) VALUES ('c','Example','t','channel','standard','i');
        UPDATE channel_inbox SET task_id='t',user_id='u',conversation_id='c',status='processed' WHERE id='i';''')

async def test_initial_schema_task_origin_and_channel_uniqueness(migrated):
    db,_,_=migrated
    assert await db.fetchval("SELECT origin FROM tasks WHERE id='old'") == 'user'
    assert await db.fetchval("SELECT count(*) FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id WHERE p.key='channel.manage'") == 1
    await seed(db)
    with pytest.raises(Exception,match='ck_task_schedule_origin'):
        await db.execute("INSERT INTO tasks(conversation_id,title,id,origin,approval_profile) VALUES ('c','Example','bad','channel','standard')")
    with pytest.raises(Exception,match='uq_channel_inbox_message'):
        await db.execute("INSERT INTO channel_inbox(id,channel_id,channel_version,message_id,sender_id,chat_id,chat_type,text,target) VALUES ('j','ch',1,'msg','ext','p','private','hello','{}')")

@pytest.mark.parametrize('assignment',["user_id=NULL","task_id=NULL","conversation_id=NULL","sender_id='other'","text='changed'","target='{}'::json"])
async def test_provenance_immutable(migrated,assignment):
    db,_,_=migrated
    await seed(db)
    if assignment=="target='{}'::json":
        assignment="target='{\"chat\":\"other\"}'::json"
    with pytest.raises(Exception,match='immutable'):
        await db.execute('UPDATE channel_inbox SET '+assignment+" WHERE id='i'")
