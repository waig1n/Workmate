"""Two Gateway startup transactions share RBAC seed ownership, not process state."""
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func,select
from sqlalchemy.ext.asyncio import async_sessionmaker,create_async_engine

from app.services import bootstrap
from workmate_product.persistence.models import Base,Role,Scope,ScopeKind


@pytest_asyncio.fixture
async def bootstrap_database():
    dsn=os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires explicitly configured local PostgreSQL')
    import asyncpg
    schema='bootstrap_'+uuid4().hex
    admin=await asyncpg.connect(dsn)
    engine=None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        engine=create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),
            connect_args={'server_settings':{'search_path':schema}})
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine,expire_on_commit=False),admin
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def test_two_gateway_bootstraps_seed_one_root_and_role_set(bootstrap_database,monkeypatch):
    sessions,admin=bootstrap_database
    entered,release,second_entered=asyncio.Event(),asyncio.Event(),asyncio.Event()
    actual=bootstrap._ensure_roles
    calls=0
    async def paused(db):
        nonlocal calls
        calls+=1
        if calls==1:
            entered.set()
            await release.wait()
        else:
            second_entered.set()
        return await actual(db)
    monkeypatch.setattr(bootstrap,'_ensure_roles',paused)
    tasks=[]
    try:
        async with sessions() as first,sessions() as second:
            pid=await second.scalar(select(func.pg_backend_pid()))
            tasks.append(asyncio.create_task(bootstrap.ensure_bootstrap(first)))
            async with asyncio.timeout(5):
                await entered.wait()
                tasks.append(asyncio.create_task(bootstrap.ensure_bootstrap(second)))
                # Observe a real advisory wait or the old unprotected second
                # seed attempt; no timing-only assumption decides the race.
                while not second_entered.is_set():
                    if await admin.fetchval("SELECT wait_event='advisory' FROM pg_stat_activity WHERE pid=$1",pid):
                        break
                    await asyncio.sleep(.01)
                if second_entered.is_set():
                    await tasks[1]
                release.set()
                await asyncio.gather(*tasks)
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Scope).where(Scope.kind==ScopeKind.ORGANIZATION))==1
            assert set(await db.scalars(select(Role.key)))==set(bootstrap.ROLE_GRANTS)
        # The commit also released transaction-scoped ownership for a restart.
        async with sessions() as db:
            async with asyncio.timeout(5):
                await bootstrap.ensure_bootstrap(db)
    finally:
        release.set()
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)


async def test_failed_bootstrap_rollback_releases_seed_ownership(bootstrap_database,monkeypatch):
    sessions,admin=bootstrap_database
    actual=bootstrap._ensure_roles
    async def fail(db):
        raise RuntimeError('fixture bootstrap failure')
    monkeypatch.setattr(bootstrap,'_ensure_roles',fail)
    async with sessions() as db:
        with pytest.raises(RuntimeError,match='fixture bootstrap failure'):
            await bootstrap.ensure_bootstrap(db)
        await db.rollback()
    monkeypatch.setattr(bootstrap,'_ensure_roles',actual)
    async with sessions() as db:
        async with asyncio.timeout(5):
            await bootstrap.ensure_bootstrap(db)
    assert await admin.fetchval("SELECT count(*) FROM scopes WHERE kind='organization'")==1


async def test_restart_seed_respects_user_provisioning_root_lock(bootstrap_database,monkeypatch):
    sessions,admin=bootstrap_database
    async with sessions() as db:
        await bootstrap.ensure_bootstrap(db)
    roles_entered=asyncio.Event()
    actual=bootstrap._ensure_roles
    async def observed(db):
        roles_entered.set()
        return await actual(db)
    monkeypatch.setattr(bootstrap,'_ensure_roles',observed)
    task=None
    try:
        async with sessions() as provisioning,sessions() as restart:
            root=await provisioning.scalar(select(Scope).where(
                Scope.kind==ScopeKind.ORGANIZATION).with_for_update())
            root.name='Customer organization'
            await provisioning.flush()
            pid=await restart.scalar(select(func.pg_backend_pid()))
            task=asyncio.create_task(bootstrap.ensure_bootstrap(restart))
            async with asyncio.timeout(5):
                while not roles_entered.is_set():
                    if await admin.fetchval("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",pid):
                        break
                    await asyncio.sleep(.01)
                assert not roles_entered.is_set()
                await provisioning.commit()
                await task
        assert await admin.fetchval("SELECT name FROM scopes WHERE kind='organization'")=='Customer organization'
    finally:
        if task is not None:
            if not task.done(): task.cancel()
            await asyncio.gather(task,return_exceptions=True)
