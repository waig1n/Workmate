"""Retirement requests and conversation tombstones cannot be reversed."""
import pytest
from test_durable_projection import projection_database


async def test_retirement_request_and_tombstone_are_immutable(projection_database):
    sessions, admin = projection_database
    await admin.execute("UPDATE conversations SET deleted_at=clock_timestamp() WHERE id='c'")
    await admin.execute("INSERT INTO conversation_retirements(conversation_id,session_id,storage_node_id,execution_epoch,requested_by) VALUES ('c','s','node',1,'u')")
    import asyncpg
    for sql in ("UPDATE conversations SET deleted_at=NULL", "DELETE FROM conversation_retirements",
                "UPDATE conversation_retirements SET storage_node_id='other'"):
        with pytest.raises(asyncpg.PostgresError):
            await admin.execute(sql)
    await admin.execute("UPDATE conversation_retirements SET completed_at=clock_timestamp(),completed_by='worker'")
    with pytest.raises(asyncpg.PostgresError):
        await admin.execute("UPDATE conversation_retirements SET completed_at=NULL,completed_by=NULL")
