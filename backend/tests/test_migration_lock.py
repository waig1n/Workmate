"""Independent startup migrations serialize within an isolated PostgreSQL schema."""

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.migration_lock import migration_lock


@pytest.fixture
async def migration_engine():
    dsn = os.getenv("WORKMATE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires an explicit test PostgreSQL DSN")
    import asyncpg

    admin = await asyncpg.connect(dsn)
    schema = "migration_lock_" + uuid4().hex
    await admin.execute(f"CREATE SCHEMA {schema}")
    engine = create_async_engine(
        dsn.replace("postgresql://", "postgresql+asyncpg://"),
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        yield engine
    finally:
        await engine.dispose()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


def upgrade(connection, fail=False):
    with migration_lock(connection):
        assert not connection.in_transaction(), "Alembic must own its transaction"
        context = MigrationContext.configure(connection)
        with context.begin_transaction():
            if (
                connection.scalar(text("SELECT to_regclass('migration_marker')"))
                is not None
            ):
                return False
            connection.execute(text("SELECT pg_sleep(0.05)"))
            connection.execute(
                text("CREATE TABLE migration_marker (revision text PRIMARY KEY)")
            )
            connection.execute(text("INSERT INTO migration_marker VALUES ('head')"))
            if fail:
                connection.execute(text("SELECT missing_migration_column"))
        return True


async def test_concurrent_migrations_check_revision_only_after_lock(migration_engine):
    async def run():
        async with migration_engine.connect() as connection:
            return await connection.run_sync(upgrade)

    async with asyncio.timeout(5):
        result = await asyncio.gather(run(), run())
    assert sorted(result) == [False, True]
    async with migration_engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT revision FROM migration_marker"))
            == "head"
        )


async def test_failed_migration_rolls_back_and_releases_same_live_connection(
    migration_engine,
):
    from sqlalchemy.exc import DBAPIError

    async with migration_engine.connect() as first:
        with pytest.raises(DBAPIError):
            await first.run_sync(lambda connection: upgrade(connection, fail=True))
        assert not first.in_transaction()
        # Keep first physical session open: close-induced unlocking cannot hide a leak.
        async with migration_engine.connect() as second:
            async with asyncio.timeout(3):
                assert await second.run_sync(upgrade)
        assert (
            await first.scalar(text("SELECT revision FROM migration_marker")) == "head"
        )


def test_sqlite_lock_does_not_commit_callers_transaction():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            with connection.begin():
                with migration_lock(connection):
                    assert connection.in_transaction()
                    connection.execute(text("CREATE TABLE marker (id integer)"))
                assert connection.in_transaction()
    finally:
        engine.dispose()


def test_alembic_environment_locks_before_context_configuration(monkeypatch):
    import alembic.context
    import app.config
    import scripts.migration_lock as locks

    events = []

    @contextmanager
    def lock(connection):
        events.append("lock")
        try:
            yield
        finally:
            events.append("unlock")

    @contextmanager
    def transaction():
        events.append("transaction")
        yield

    config = SimpleNamespace(
        config_file_name=None,
        set_main_option=lambda *a: None,
        get_main_option=lambda *a: "sqlite://",
    )
    monkeypatch.setattr(alembic.context, "config", config, raising=False)
    monkeypatch.setattr(alembic.context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(
        alembic.context, "configure", lambda **k: events.append("configure")
    )
    monkeypatch.setattr(alembic.context, "begin_transaction", transaction)
    monkeypatch.setattr(
        alembic.context, "run_migrations", lambda: events.append("migrate")
    )
    monkeypatch.setattr(
        app.config, "get_settings", lambda: SimpleNamespace(database_url="sqlite://")
    )
    monkeypatch.setattr(locks, "migration_lock", lock)
    namespace = runpy.run_path(str(Path(__file__).parents[1] / "alembic/env.py"))
    assert events == ["configure", "transaction", "migrate"], (
        "Offline SQL must not acquire locks"
    )
    events.clear()
    namespace["do_run_migrations"](object())
    assert events == ["lock", "configure", "transaction", "migrate", "unlock"]


def test_unlock_failure_invalidates_session_without_masking_migration_error():
    from unittest.mock import Mock

    connection = Mock()
    connection.dialect.name = "postgresql"
    connection.in_transaction.return_value = False
    connection.scalar.side_effect = [123, OSError("unlock connection lost")]
    with pytest.raises(RuntimeError, match="original migration failure"):
        with migration_lock(connection):
            raise RuntimeError("original migration failure")
    connection.invalidate.assert_called_once()


def test_uncertain_lock_acquisition_discards_connection():
    from unittest.mock import Mock

    connection = Mock()
    connection.dialect.name = "postgresql"
    connection.in_transaction.return_value = False
    connection.scalar.return_value = 123
    connection.execute.side_effect = OSError("acquisition response lost")
    with pytest.raises(OSError, match="acquisition response lost"):
        with migration_lock(connection):
            pytest.fail("Migration must not run after ambiguous acquisition")
    connection.invalidate.assert_called_once()


def test_unlock_failure_after_success_is_reported():
    from unittest.mock import Mock

    connection = Mock()
    connection.dialect.name = "postgresql"
    connection.in_transaction.return_value = False
    connection.scalar.side_effect = [123, OSError("unlock connection lost")]
    with pytest.raises(OSError, match="unlock connection lost"):
        with migration_lock(connection):
            pass
    connection.invalidate.assert_called_once()
