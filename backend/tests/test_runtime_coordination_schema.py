"""Current model and deployment contracts."""
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import configure_mappers


def test_runtime_models_registered_and_relationships_resolve():
    from workmate_product.persistence.models import (
        Base,
        Conversation,
        RuntimeCommand,
        RuntimeEvent,
        RuntimeSession,
    )
    configure_mappers()
    for model in (RuntimeCommand, RuntimeEvent, RuntimeSession):
        assert Base.metadata.tables[model.__tablename__] is model.__table__
    assert Conversation.__table__.c.active_product_task_id.nullable
    assert 'admitted_task_id' not in RuntimeSession.__table__.c


def test_postgres_metadata_create_drop_handles_active_task_cycle():
    """Opt-in real PostgreSQL check; creates and drops only a fresh random schema."""
    import asyncio
    import os
    import uuid

    dsn = os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires an isolated PostgreSQL database')

    async def verify():
        import asyncpg
        from sqlalchemy.ext.asyncio import create_async_engine
        from workmate_product.persistence.models import Base

        schema = 'metadata_' + uuid.uuid4().hex
        admin = await asyncpg.connect(dsn)
        engine = None
        try:
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            engine = create_async_engine(
                dsn.replace('postgresql://', 'postgresql+asyncpg://'),
                connect_args={'server_settings': {'search_path': schema}},
            )
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
                def check(connection):
                    inspector = inspect(connection)
                    active_fk = next(fk for fk in inspector.get_foreign_keys('conversations')
                                     if fk['constrained_columns'] == ['active_product_task_id'])
                    assert active_fk['name'] == 'fk_conversation_active_product_task'
                    assert active_fk['referred_table'] == 'tasks'
                    assert not active_fk['options'].get('ondelete')
                    snapshot = next(col for col in inspector.get_columns('runtime_sessions')
                                    if col['name'] == 'snapshot')
                    assert str(snapshot['type']) == 'JSONB'
                await db.run_sync(check)
                await db.run_sync(Base.metadata.drop_all)
                assert await db.run_sync(lambda connection: inspect(connection).get_table_names()) == []
        finally:
            if engine:
                await engine.dispose()
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()

    asyncio.run(verify())
