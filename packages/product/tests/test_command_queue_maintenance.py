"""Retired directed command streams are settled from PG, never from health expiry."""
import pytest

from test_command_deliveries import database, admit, seed_residency
from test_command_queue import queue
from workmate_product.coordination.command_queue import CommandEnvelope, QueueRoute
from workmate_product.coordination.command_queue_maintenance import CommandQueueMaintenance
from workmate_product.coordination.worker_registry import RedisWorkerRegistry, WorkerRecord


async def setup(database, queue):
    sessions, admin = database
    await admit(sessions)
    await admin.execute("UPDATE command_deliveries SET target_incarnation='warm'")
    route = QueueRoute('node', 'warm')
    await queue.ensure_group(route)
    envelope = CommandEnvelope(command_id='cmd',delivery_generation=1,storage_node_id='node',target_incarnation='warm')
    mid = await queue.publish(route,envelope)
    registry = RedisWorkerRegistry(queue.client,namespace=queue.namespace)
    return CommandQueueMaintenance(sessions,queue,registry),registry,route,mid


async def cycle(value, *, count=4):
    for _ in range(2000):
        if await value.tick(count=count,retain_ms=0):
            return
    pytest.fail('maintenance did not visit isolated stream')


@pytest.mark.parametrize('status', ['completed','rejected','indeterminate'])
async def test_terminal_command_is_durably_settled_before_ack(database,queue,status):
    maintenance,registry,route,mid = await setup(database,queue)
    await database[1].execute('UPDATE runtime_commands SET status=$1',status)
    await queue.read(route,'warm')
    await cycle(maintenance)
    assert await queue.pending(route) == []
    assert await database[1].fetchval('SELECT status FROM command_deliveries') == ('quarantined' if status=='indeterminate' else 'settled')
    assert await queue.stream_has(route,mid)  # Retain final history/group metadata.


async def test_unclaimed_old_route_has_replacement_before_ack(database,queue):
    maintenance,registry,route,mid = await setup(database,queue)
    await cycle(maintenance)
    assert await queue.pending(route) == []
    rows=await database[1].fetch('SELECT delivery_generation,status,target_incarnation FROM command_deliveries ORDER BY delivery_generation')
    assert [tuple(row) for row in rows] == [(1,'superseded','warm'),(2,'pending',None)]
    assert await database[1].fetchval('SELECT status FROM runtime_commands') == 'accepted'


@pytest.mark.parametrize('status',['held','releasing','blocked'])
async def test_unresolved_residence_never_replayed_from_missing_health(database,queue,status):
    maintenance,registry,route,mid = await setup(database,queue)
    await seed_residency(database[1],status)
    await cycle(maintenance)
    assert [p.message_id for p in await queue.pending(route)] == [mid]
    assert await database[1].fetchval('SELECT count(*) FROM command_deliveries') == 1
    assert await database[1].fetchval('SELECT status FROM workspace_residencies') == status
    assert await database[1].fetchval('SELECT error_code FROM command_delivery_quarantines') == 'delivery_holder_unavailable'


@pytest.mark.parametrize('claimed',[False,True])
async def test_missing_redis_health_cannot_steal_live_pg_owner(database,queue,claimed):
    maintenance,registry,route,mid = await setup(database,queue)
    await database[1].execute("UPDATE runtime_sessions SET owner_id='warm',lease_until=clock_timestamp()+interval '1 hour',execution_epoch=1")
    if claimed:
        await database[1].execute("UPDATE runtime_commands SET status='claimed',claimed_by='warm',claimed_epoch=1")
    await queue.read(route,'warm')
    await cycle(maintenance)
    pending=await queue.pending(route)
    assert len(pending)==1 and pending[0].consumer=='warm'
    assert await database[1].fetchval('SELECT status FROM command_deliveries') == 'pending'


async def test_lost_claimed_owner_is_isolated_without_tool_replay(database,queue):
    maintenance,registry,route,mid = await setup(database,queue)
    await database[1].execute("UPDATE runtime_sessions SET owner_id='warm',execution_epoch=1,lease_until=clock_timestamp()-interval '1 second'; UPDATE runtime_commands SET status='claimed',claimed_by='warm',claimed_epoch=1")
    await cycle(maintenance)
    assert await queue.pending(route) == []
    assert await database[1].fetchval('SELECT error_code FROM command_deliveries') == 'execution_owner_lost'
    assert await database[1].fetchval('SELECT status FROM runtime_commands') == 'claimed'


async def test_healthy_draining_worker_and_public_queue_not_competed_for(database,queue):
    maintenance,registry,route,mid = await setup(database,queue)
    await registry.heartbeat(WorkerRecord(incarnation='warm',node_id='node',total_capacity=1,available_capacity=0,draining=True))
    public=QueueRoute('node')
    await queue.ensure_group(public)
    public_mid=await queue.publish(public,CommandEnvelope(command_id='unknown',delivery_generation=1,storage_node_id='node'))
    for _ in range(5):
        await cycle(maintenance)
    assert await queue.pending(route) == [] and await queue.pending(public) == []
    assert (await queue.read(route,'warm'))[0].message_id == mid
    assert (await queue.read(public,'peer'))[0].message_id == public_mid


async def test_invalid_message_and_forged_channel_have_durable_quarantine(database,queue):
    maintenance,registry,route,mid = await setup(database,queue)
    await database[1].execute("UPDATE command_deliveries SET status='quarantined'")
    bad=await queue.client.xadd(queue.stream_key(route),{'payload':'{}'})
    await cycle(maintenance)
    assert await queue.pending(route) == []
    assert await database[1].fetchval('SELECT error_code FROM command_delivery_quarantines WHERE message_id=$1',bad)=='invalid_envelope'
    control=QueueRoute('node','warm',True)
    await queue.ensure_group(control)
    forged=await queue.publish(control,CommandEnvelope(command_id='cmd',delivery_generation=1,storage_node_id='node',target_incarnation='warm'))
    for _ in range(5):
        await cycle(maintenance)
    assert await queue.pending(control) == []
    assert await database[1].fetchval('SELECT error_code FROM command_delivery_quarantines WHERE message_id=$1 AND stream_key=$2',forged,queue.stream_key(control))=='invalid_delivery'


async def test_publisher_invokes_command_maintenance_existing_policy(database):
    from test_command_publisher import Queue
    from workmate_product.coordination.command_publisher import CommandPublisher
    from workmate_product.config.command_queue import CommandQueuePolicy
    calls=[]
    class Maintenance:
        async def tick(self,**kwargs): calls.append(kwargs)
    publisher=CommandPublisher(database[0],Queue(database[1]),CommandQueuePolicy(batch_size=5,retention_seconds=60),command_maintenance=Maintenance())
    await publisher.repair_once()
    assert calls==[{'count':5,'retain_ms':60000}]
