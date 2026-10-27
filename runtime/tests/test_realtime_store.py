"""Production display persistence restores cuts and fences short transactions."""

from types import SimpleNamespace

import pytest
from sqlalchemy import event

from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
from workmate_runtime.execution.realtime_writer import (
    RealtimeAuthorityLost,
    RealtimeUnavailable,
)
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_product.config.realtime import RealtimePolicy
from test_execution_context import database
from test_message_snapshots import running
from test_realtime_writer import Fixture, delta, raw, start

pytestmark = pytest.mark.asyncio


def setup(sessions, *, epoch=1, worker="owner"):
    guard = SimpleNamespace(
        owner=ExecutionOwner("s", "t", worker, epoch),
        reply_id="reply",
        failure=None,
        live=True,
        checks=0,
    )

    async def authority():
        guard.checks += 1
        return guard.failure is None and guard.live

    guard.display_authority = authority
    transport = Fixture()
    original = transport.append

    async def append(ref, envelope):
        assert sessions.kw["bind"].pool.checkedout() == 0
        return await original(ref, envelope)

    transport.append = append
    factory = RealtimeWriterFactory(
        sessions, log_manager=transport, log=transport, policy=RealtimePolicy()
    )
    return guard, factory


async def test_short_cuts_and_same_reply_new_owner_restore(database):
    sessions, admin = database
    await running(admin)
    guard, factory = setup(sessions)
    writer = await factory(guard, None)
    await writer.accept([start(), delta("原文")])
    first = await writer.checkpoint("approval")
    assert first.blocks[0].text == "原文"
    await writer.close()
    await admin.execute(
        "UPDATE runtime_sessions SET owner_id='next',execution_epoch=2; UPDATE runtime_task_executions SET execution_epoch=2"
    )
    resumed_guard, resumed_factory = setup(sessions, epoch=2, worker="next")
    resumed = await resumed_factory(resumed_guard, None)
    await resumed.accept(delta("续写"))
    final = await resumed.checkpoint("final")
    assert final.message_id == first.message_id
    assert final.snapshot_version > first.snapshot_version
    assert final.execution_epoch == 2 and final.blocks[0].text == "原文续写"
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_messages WHERE role='assistant'"
        )
        == 1
    )
    await resumed.close()


async def test_no_body_does_not_create_empty_assistant_or_reference(database):
    sessions, admin = database
    await running(admin)
    guard, factory = setup(sessions)
    writer = await factory(guard, None)
    await writer.accept([raw("TOOL_CALL_START", tool_call_id="call"), start()])
    assert await writer.checkpoint("approval") is None
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_messages WHERE role='assistant'"
        )
        == 0
    )
    await writer.accept(delta("answer"))
    assert (await writer.checkpoint("final")).blocks[0].text == "answer"
    await writer.close()


async def test_delta_authority_is_local_but_save_fences_real_owner(database):
    sessions, admin = database
    await running(admin)
    guard, factory = setup(sessions)
    writer = await factory(guard, None)
    queries = []
    engine = sessions.kw["bind"].sync_engine

    def record(*args):
        queries.append(args[2])

    event.listen(engine, "before_cursor_execute", record)
    try:
        await writer.accept([start(), delta("a")])
        for _ in range(10):
            await writer.accept(delta("b"))
        assert not queries and guard.checks > 10
    finally:
        event.remove(engine, "before_cursor_execute", record)
    await admin.execute("UPDATE runtime_sessions SET owner_id='stolen'")
    with pytest.raises(RealtimeUnavailable, match="commit"):
        await writer.checkpoint("final")
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM task_messages WHERE role='assistant'"
        )
        == 0
    )
    await writer.close()


@pytest.mark.parametrize("state", ["failure", "lease"])
async def test_local_authority_loss_prevents_new_body(database, state):
    sessions, admin = database
    await running(admin)
    guard, factory = setup(sessions)
    writer = await factory(guard, None)
    if state == "failure":
        guard.failure = RuntimeError("heartbeat lost")
    else:
        guard.live = False
    with pytest.raises(RealtimeAuthorityLost):
        await writer.accept(start())
    await writer.close()


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE runtime_sessions SET execution_epoch=2",
        "UPDATE runtime_task_executions SET reply_id='foreign'",
        "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
        "UPDATE conversations SET active_product_task_id=NULL",
    ],
)
async def test_factory_rejects_stale_binding_before_restore(database, change):
    sessions, admin = database
    await running(admin)
    guard, factory = setup(sessions)
    await admin.execute(change)
    with pytest.raises(RealtimeAuthorityLost):
        await factory(guard, None)


async def test_factory_requires_reply_already_bound(database):
    sessions, _ = database
    guard, factory = setup(sessions)
    guard.reply_id = None
    with pytest.raises(RealtimeAuthorityLost):
        await factory(guard, None)


async def test_existing_empty_snapshot_is_retained_and_gets_terminal_reference(
    database,
):
    from workmate_product.application.message_snapshots import save_message_snapshot
    from test_message_snapshots import snapshot

    sessions, admin = database
    await running(admin)
    existing = snapshot(phase="approval").model_copy(
        update={"blocks": [], "covered_cursor": None}
    )
    async with sessions.begin() as db:
        await save_message_snapshot(db, existing, owner_id="owner", unanchored=True)
    guard, factory = setup(sessions)
    writer = await factory(guard, None)
    final = await writer.checkpoint("final")
    assert final is not None and final.message_id == existing.message_id
    assert final.snapshot_version > existing.snapshot_version and final.blocks == []
    assert (
        await admin.fetchval(
            "SELECT snapshot_phase FROM task_messages WHERE role='assistant'"
        )
        == "final"
    )
    await writer.close()


async def test_factory_rejects_message_identity_collision(database):
    from workmate_contracts.realtime import message_id

    sessions, admin = database
    await running(admin)
    await admin.execute(
        "UPDATE task_messages SET id=$1 WHERE role='user'", message_id("t", "reply")
    )
    guard, factory = setup(sessions)
    with pytest.raises(RealtimeAuthorityLost, match="identity"):
        await factory(guard, None)
