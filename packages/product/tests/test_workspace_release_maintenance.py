"""Gateway maintenance never interprets dead health as resource release proof."""
import pytest

from test_execution_context import database
from test_workspace_residencies import held, idle, request, proof
from test_workspace_release_queue import queue, route, notice
from workmate_product.coordination.worker_registry import RedisWorkerRegistry, WorkerRecord
from workmate_product.coordination.workspace_release_maintenance import WorkspaceReleaseMaintenance
from workmate_product.persistence.repositories import workspace_residencies as store


async def setup(database, queue, *, terminal=False):
    sessions, admin = database
    await held(sessions, admin)
    row = await request(sessions)
    if terminal:
        await idle(admin)
        async with sessions.begin() as db:
            claim = await store.begin_release(db, row['id'], 'worker', 'node')
            await store.finish_release(db, claim, proof(claim))
    await queue.ensure_group(route())
    envelope = notice(request_id=row['id'], session_id='s')
    mid = await queue.publish(route(), envelope)
    registry = RedisWorkerRegistry(queue.client, namespace=queue.namespace)
    return WorkspaceReleaseMaintenance(sessions, queue, registry), registry, mid


async def cycle(maintenance, queue, *, count=4):
    # SCAN traverses the entire local Redis keyspace even with namespace MATCH.
    # Stop after finding our single stream, rather than assume one SCAN page.
    for _ in range(2000):
        if await maintenance.tick(count=count, retain_ms=0):
            return
    pytest.fail('maintenance did not visit isolated test stream')


async def test_offline_terminal_unread_is_acknowledged_after_pg_disposition(database, queue):
    maintenance, registry, mid = await setup(database, queue, terminal=True)
    await cycle(maintenance, queue)
    assert await queue.pending(route()) == []
    assert await queue.read(route(), 'worker') == []
    assert await queue.stream_has(route(), mid)  # Conservative final entry retention, no blind DEL.


async def test_offline_pending_is_diagnosed_without_fabricating_release(database, queue):
    maintenance, registry, mid = await setup(database, queue)
    await cycle(maintenance, queue)
    sessions, admin = database
    assert await admin.fetchval('SELECT status FROM workspace_release_requests') == 'pending'
    assert await admin.fetchval('SELECT status FROM workspace_residencies') == 'held'
    assert [row.message_id for row in await queue.pending(route())] == [mid]
    assert await admin.fetchval('SELECT error_code FROM command_delivery_quarantines') == 'release_holder_unavailable'
    await cycle(maintenance, queue)
    assert await admin.fetchval('SELECT count(*) FROM command_delivery_quarantines') == 1


async def test_healthy_draining_worker_stream_is_not_competed_for(database, queue):
    maintenance, registry, mid = await setup(database, queue, terminal=True)
    await registry.heartbeat(WorkerRecord(incarnation='worker', node_id='node', total_capacity=2,
        available_capacity=0, draining=True))
    await cycle(maintenance, queue)
    assert await queue.pending(route()) == []
    assert (await queue.read(route(), 'worker'))[0].message_id == mid


async def test_old_pending_terminal_and_malformed_are_acknowledged(database, queue):
    maintenance, registry, mid = await setup(database, queue, terminal=True)
    await queue.ensure_group(route())
    await queue.read(route(), 'worker')
    bad = await queue.client.xadd(queue.stream_key(route()), {'payload':'{"kind":"StopTask"}'})
    await cycle(maintenance, queue)
    assert await queue.pending(route()) == []
    assert await queue.read(route(), 'worker') == []
    assert await database[1].fetchval('SELECT message_id FROM command_delivery_quarantines') == bad


async def test_forged_release_tuple_is_durably_isolated(database, queue):
    maintenance, registry, mid = await setup(database, queue)
    wrong = await queue.publish(route(), notice(request_id='unknown', session_id='s'))
    await cycle(maintenance, queue)
    await cycle(maintenance, queue)
    assert await database[1].fetchval("SELECT error_code FROM command_delivery_quarantines WHERE message_id=$1", wrong) == 'release_identity_mismatch'
    assert [row.message_id for row in await queue.pending(route())] == [mid]


async def test_pg_failure_never_acknowledges_unread_message(database, queue, monkeypatch):
    maintenance, registry, mid = await setup(database, queue, terminal=True)
    async def failing(*args, **kwargs):
        raise ConnectionError('isolated database failure')
    monkeypatch.setattr(maintenance, '_disposition', failing)
    with pytest.raises(ConnectionError):
        await cycle(maintenance, queue)
    assert [row.message_id for row in await queue.pending(route())] == [mid]


async def test_publisher_invokes_release_maintenance_using_existing_policy(database):
    from test_command_publisher import Queue
    from workmate_product.coordination.command_publisher import CommandPublisher
    from workmate_product.config.command_queue import CommandQueuePolicy
    calls = []
    class Maintenance:
        async def tick(self, **kwargs): calls.append(kwargs)
    publisher = CommandPublisher(database[0], Queue(database[1]), CommandQueuePolicy(batch_size=3, retention_seconds=60),
                                 release_maintenance=Maintenance())
    await publisher.repair_once()
    assert calls == [{'count':3, 'retain_ms':60000}]


@pytest.mark.parametrize('status', ['releasing', 'blocked', 'stale'])
async def test_request_disposition_controls_ack_not_health_expiry(database, queue, status):
    maintenance, registry, mid = await setup(database, queue)
    await idle(database[1])
    row = await request(database[0])
    async with database[0].begin() as db:
        claim = await store.begin_release(db, row['id'], 'worker', 'node')
        if status == 'blocked':
            await store.block_release(db, claim, 'cleanup_unproven')
    if status == 'stale':
        await database[1].execute("UPDATE workspace_release_requests SET status='stale', completed_at=now()")
    await cycle(maintenance, queue)
    assert bool(await queue.pending(route())) == (status == 'releasing')
    assert await database[1].fetchval('SELECT status FROM workspace_release_requests') == status


async def test_one_message_budget_does_not_starve_unread_behind_blocked_pending(database, queue):
    maintenance, registry, mid = await setup(database, queue)
    await cycle(maintenance, queue, count=1)
    bad = await queue.client.xadd(queue.stream_key(route()), {'payload':'{}'})
    for _ in range(4):
        await cycle(maintenance, queue, count=1)
    assert [row.message_id for row in await queue.pending(route())] == [mid]
    assert await database[1].fetchval('SELECT error_code FROM command_delivery_quarantines WHERE message_id=$1', bad) == 'invalid_envelope'


async def test_maintenance_skips_other_namespaces_and_malformed_keys(database, queue):
    from workmate_product.coordination.workspace_release_queue import RedisWorkspaceReleaseQueue
    from workmate_product.coordination.workspace_release_maintenance import WorkspaceReleaseMaintenance
    from workmate_product.coordination.worker_registry import RedisWorkerRegistry
    healthy = await setup(database, queue, terminal=True)
    other = RedisWorkspaceReleaseQueue(queue.client, namespace=queue.namespace+'-unrelated')
    mid = await other.publish(route(), notice())
    try:
        for _ in range(8):
            await healthy[0].tick(count=100, retain_ms=0)
        assert await other.stream_has(route(), mid)
        assert await queue.client.xinfo_groups(other.stream_key(route())) == []
    finally:
        await queue.client.delete(other.stream_key(route()))
