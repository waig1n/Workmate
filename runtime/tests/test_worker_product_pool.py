"""Worker transport and Product authorization share one PostgreSQL transaction."""
import pytest
from sqlalchemy import text
from test_execution_context import database


async def test_product_reads_uncommitted_transport_writes_and_outer_rollback(database):
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    sessions,admin=database
    pool=ProductWorkerPool(sessions.kw['bind'])
    with pytest.raises(RuntimeError,match='rollback'):
        async with pool.acquire() as db:
            async with db.transaction():
                await db.execute("UPDATE tasks SET title='transport' WHERE id='t'")
                async with db.product_session() as product:
                    assert await product.scalar(text("SELECT title FROM tasks WHERE id='t'"))=='transport'
                    await product.execute(text("UPDATE tasks SET title='product' WHERE id='t'"))
                assert await db.fetchval("SELECT title FROM tasks WHERE id='t'")=='product'
            raise RuntimeError('rollback')
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='Task'


async def test_nested_transport_failure_keeps_outer_transaction_usable(database):
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    sessions,admin=database
    pool=ProductWorkerPool(sessions.kw['bind'])
    async with pool.acquire() as db:
        await db.execute("UPDATE tasks SET title='outer' WHERE id='t'")
        with pytest.raises(ValueError):
            async with db.transaction():
                await db.execute("UPDATE tasks SET title='inner' WHERE id='t'")
                raise ValueError()
        async with db.product_session() as product:
            assert await product.scalar(text("SELECT title FROM tasks WHERE id='t'"))=='outer'
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='outer'


async def test_transport_json_is_text_while_orm_json_stays_decoded(database):
    import json
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    sessions,_=database
    pool=ProductWorkerPool(sessions.kw['bind'])
    async with pool.acquire() as db:
        record=await db.fetchrow("SELECT '{\"value\":[1]}'::jsonb AS payload, ARRAY['x'] AS ids")
        assert json.loads(record['payload'])=={'value':[1]}
        assert record['ids']==['x']
        async with db.product_session() as product:
            assert await product.scalar(text("SELECT '{\"value\":[1]}'::jsonb"))=={'value':[1]}


async def test_native_notifications_follow_outer_commit_and_savepoint_rollback(database):
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    from workmate_product.coordination import WakeupBus
    sessions, _ = database
    bus = WakeupBus('unused')
    pool = ProductWorkerPool(sessions.kw['bind'], wakeup_bus=bus)
    async with pool.acquire() as db:
        db.stage_wakeup('outer')
        async with db.transaction():
            db.stage_wakeup('committed-savepoint')
        assert bus._queue.empty()
        with pytest.raises(ValueError):
            async with db.transaction():
                db.stage_wakeup('rolled-back-savepoint')
                raise ValueError()
    assert bus._pending == {'outer', 'committed-savepoint'}
    with pytest.raises(ValueError):
        async with pool.acquire() as db:
            db.stage_wakeup('rolled-back-outer')
            raise ValueError()
    assert bus._pending == {'outer', 'committed-savepoint'}
