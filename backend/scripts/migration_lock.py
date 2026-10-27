"""Serialize online PostgreSQL Alembic invocations, not application execution."""

from contextlib import contextmanager

from sqlalchemy import text


@contextmanager
def migration_lock(connection):
    if connection.dialect.name != "postgresql":
        yield
        return
    if connection.in_transaction():
        raise RuntimeError("Migration lock requires a fresh connection")
    acquired = failed = False
    try:
        # Advisory locks are database-local. Include the effective schema so
        # isolated test/tenant schemas do not block each other's migration.
        key = connection.scalar(
            text("SELECT hashtextextended('workmate:alembic:' || current_schema(), 0)")
        )
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        acquired = True
        # A session lock survives commit; clear autobegin before Alembic decides
        # whether it owns the migration transaction (including version updates).
        connection.commit()
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            if connection.in_transaction():
                connection.rollback()
            if acquired:
                released = connection.scalar(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
                connection.commit()
                if released is not True:
                    raise RuntimeError("Migration lock was not owned during release")
            else:
                # A failed acquisition may have lost its network response after
                # taking the lock. Discard the session instead of pooling it.
                connection.invalidate()
        except BaseException:
            try:
                connection.invalidate()
            except BaseException:
                pass
            if not failed:
                raise
