"""Sandbox proofs remain immutable across independent execution epochs."""
import pytest
from sqlalchemy import inspect
from test_durable_projection import projection_database
from test_full_access_upgrade import prepared


async def test_initial_schema_proof_keys(projection_database):
    sessions,admin=projection_database
    await prepared(sessions,admin)
    original=dict(await admin.fetchrow('SELECT * FROM runtime_sandbox_attestations'))
    async with sessions.begin() as db:
        def run(connection):
            assert set(inspect(connection).get_pk_constraint('runtime_sandbox_attestations')['constrained_columns'])=={'task_id','execution_epoch'}
            task_fk=next(f for f in inspect(connection).get_foreign_keys('runtime_permission_upgrades') if f['constrained_columns']==['task_id'])
            assert task_fk['referred_table']=='tasks'
        await (await db.connection()).run_sync(run)
    assert dict(await admin.fetchrow('SELECT * FROM runtime_sandbox_attestations'))==original


async def test_multiple_epochs_are_immutable(projection_database):
    sessions,admin=projection_database
    await prepared(sessions,admin)
    await admin.execute("""INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
        VALUES ('t','s','new-worker',2,'workspace')""")
    assert await admin.fetchval('SELECT count(*) FROM runtime_sandbox_attestations')==2
    import asyncpg
    with pytest.raises(asyncpg.RaiseError):
        await admin.execute("UPDATE runtime_sandbox_attestations SET worker_id='rewrite' WHERE execution_epoch=1")
