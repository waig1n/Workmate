"""R0 transaction proof, not a production repository or schema migration.

Run against a disposable database using WORKMATE_TEST_POSTGRES_DSN. Every test
owns a random schema and drops only that schema. No current product tables used.
"""

import asyncio
import os
import uuid

import pytest


@pytest.mark.asyncio
async def test_session_lock_serializes_competing_commands_and_fences_old_owner():
    dsn = os.getenv("WORKMATE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires isolated PostgreSQL; SQLite is not sufficient")
    import asyncpg

    schema = "claim_" + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute("""
            CREATE TABLE sessions (
                id text PRIMARY KEY, active_task text, owner text,
                epoch bigint NOT NULL DEFAULT 0, lease_until timestamptz
            );
            CREATE TABLE commands (
                id text PRIMARY KEY, session_id text NOT NULL REFERENCES sessions,
                task_id text NOT NULL, claimed boolean NOT NULL DEFAULT false
            );
            INSERT INTO sessions(id) VALUES ('session');
            INSERT INTO commands VALUES ('a','session','task-a',false),
                                        ('b','session','task-b',false);
        """)

        async def claim(command_id, worker, *, fail_before_commit=False):
            connection = await asyncpg.connect(dsn)
            try:
                await connection.execute(f'SET search_path TO "{schema}"')
                async with connection.transaction():
                    session = await connection.fetchrow(
                        "SELECT * FROM sessions WHERE id='session' FOR UPDATE")
                    if session["active_task"] is not None:
                        return None
                    command = await connection.fetchrow(
                        "UPDATE commands SET claimed=true WHERE id=$1 AND NOT claimed RETURNING *",
                        command_id,
                    )
                    if command is None:
                        return None
                    result = await connection.fetchrow("""
                        UPDATE sessions SET active_task=$1, owner=$2, epoch=epoch+1,
                            lease_until=clock_timestamp()+interval '30 seconds'
                        WHERE id='session' RETURNING *
                    """, command["task_id"], worker)
                    if fail_before_commit:
                        raise RuntimeError("injected transaction failure")
                    return result
            finally:
                await connection.close()

        with pytest.raises(RuntimeError, match="injected transaction failure"):
            await claim("a", "failed-worker", fail_before_commit=True)
        assert await admin.fetchval("SELECT active_task FROM sessions") is None
        assert await admin.fetchval("SELECT epoch FROM sessions") == 0
        assert await admin.fetchval("SELECT count(*) FROM commands WHERE claimed") == 0

        claims = await asyncio.gather(*(claim("a", f"worker-{i}") for i in range(8)),
                                      claim("b", "other-task"))
        winners = [row for row in claims if row is not None]
        assert len(winners) == 1
        winner = winners[0]
        assert await admin.fetchval("SELECT count(*) FROM commands WHERE claimed") == 1
        assert winner["epoch"] == 1

        # Recovery fences persistence; it does not retry an uncertain tool.
        await admin.execute("UPDATE sessions SET epoch=epoch+1, owner='recovery'")
        stale = await admin.fetchrow("""
            UPDATE sessions SET active_task=NULL
            WHERE id='session' AND owner=$1 AND epoch=$2
                AND lease_until > clock_timestamp() RETURNING id
        """, winner["owner"], winner["epoch"])
        assert stale is None
        assert await admin.fetchval("SELECT active_task FROM sessions") == winner["active_task"]
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
