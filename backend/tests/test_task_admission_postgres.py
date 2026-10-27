"""Shared admission race test; WORKMATE_TEST_POSTGRES_URL must be disposable."""

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.task_admission import task_admission
def _async_url():
    value = os.environ['WORKMATE_TEST_POSTGRES_URL']
    if value.startswith('postgresql+asyncpg://'):
        return value
    return 'postgresql+asyncpg://' + value.split('://', 1)[-1]


@pytest.mark.integration
async def test_postgres_admission_serializes_independent_connections(monkeypatch):
    if not os.getenv("WORKMATE_TEST_POSTGRES_URL"):
        pytest.skip("set WORKMATE_TEST_POSTGRES_URL to a disposable database")
    schema = f"admission_test_{uuid.uuid4().hex}"
    engine = create_async_engine(
        _async_url(), connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine)
    monkeypatch.setattr(get_settings(), "task_max_outstanding", 3)
    monkeypatch.setattr(get_settings(), "task_max_outstanding_per_user", 2)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
            await conn.execute(text("CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT)"))
            await conn.execute(text("CREATE TABLE tasks (id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT)"))
            await conn.execute(text("CREATE TABLE runtime_commands (id TEXT, task_id TEXT, kind TEXT, status TEXT)"))
            await conn.execute(text("CREATE TABLE runtime_task_executions (task_id TEXT, status TEXT)"))
            await conn.execute(text("INSERT INTO conversations VALUES ('a', 'a'), ('b', 'b')"))

        async def submit(user):
            async with sessions() as db:
                try:
                    async with task_admission(db, user):
                        await asyncio.sleep(0.01)
                        await db.execute(text(
                            "INSERT INTO tasks VALUES (:id, :cid, 'created')",
                        ), {"id": str(uuid.uuid4()), "cid": user})
                        await db.commit()
                    return True
                except HTTPException as exc:
                    assert exc.status_code == 429
                    return False

        results = await asyncio.gather(*(submit(user) for user in ["a", "b"] * 6))
        assert sum(results) == 3
        async with engine.connect() as conn:
            counts = (await conn.execute(text(
                "SELECT conversation_id, count(*) FROM tasks GROUP BY conversation_id",
            ))).all()
            assert all(count <= 2 for _, count in counts)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await engine.dispose()
