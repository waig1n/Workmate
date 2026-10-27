"""Outbox network effects follow committed leases and never imply execution."""
from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace
import pytest
from test_command_deliveries import database, admit
from test_command_queue import queue as real_queue
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.coordination.command_publisher import CommandPublisher

pytestmark=pytest.mark.asyncio


class Queue:
    def __init__(self,admin):
        self.admin=admin
        self.sent=[]
        self.exists=True
        self.failure=False
        self.trimmed=[]
    async def ensure_group(self,route): pass
    async def publish(self,route,envelope):
        assert await self.admin.fetchval('SELECT status FROM command_deliveries')=='publishing'
        if self.failure: raise ConnectionError('do not log credentials')
        self.sent.append(envelope)
        return '1-0'
    async def stream_has(self,route,message_id): return self.exists
    async def trim_acknowledged(self,*args,**kwargs): return 0
    async def trim_page(self,**kwargs):
        self.trimmed.append(kwargs)
        return 12 if kwargs['cursor']==0 else 0


async def test_publish_committed_intent_and_record_receipt(database):
    sessions,admin=database
    await admit(sessions)
    queue=Queue(admin)
    assert await CommandPublisher(sessions,queue,CommandQueuePolicy()).publish_once()==1
    assert len(queue.sent)==1
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='published'
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'


async def test_redis_failure_keeps_intent_and_bounded_retry(database):
    sessions,admin=database
    await admit(sessions)
    queue=Queue(admin);queue.failure=True
    await CommandPublisher(sessions,queue,CommandQueuePolicy(max_attempts=1)).publish_once()
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='quarantined'
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'


async def test_repair_republishes_lost_message_but_not_normal_queue_wait(database):
    sessions,admin=database
    await admit(sessions)
    queue=Queue(admin)
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy())
    await publisher.publish_once()
    async def old():
        await admin.execute("UPDATE command_deliveries SET published_at=now()-interval '1 hour',next_attempt_at=now()-interval '1 hour'")
    await old()
    await publisher.repair_once()
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1
    queue.exists=False
    await old()
    await publisher.repair_once()
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==2
    assert await admin.fetchval('SELECT status FROM command_deliveries WHERE delivery_generation=1')=='superseded'
    assert await admin.fetchval('SELECT status FROM command_deliveries WHERE delivery_generation=2')=='pending'


async def test_repair_maintains_settled_only_streams_without_candidates(database):
    sessions,admin=database
    queue=Queue(admin)
    policy=CommandQueuePolicy(batch_size=4,retention_seconds=60)
    publisher=CommandPublisher(sessions,queue,policy)
    await publisher.repair_once()
    await publisher.repair_once()
    assert queue.trimmed==[
        {'cursor':0,'count':4,'retain_ms':60000},
        {'cursor':12,'count':4,'retain_ms':60000}]


async def test_xadd_success_receipt_loss_retries_identity_not_execution(database,monkeypatch):
    from workmate_product.persistence.repositories import command_deliveries
    sessions,admin=database
    await admit(sessions)
    queue=Queue(admin)
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy())
    original=command_deliveries.published
    async def lose_receipt(*args,**kwargs):
        raise ConnectionError('receipt unavailable')
    monkeypatch.setattr(command_deliveries,'published',lose_receipt)
    with pytest.raises(ExceptionGroup):
        await publisher.publish_once()
    assert len(queue.sent)==1
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='publishing'
    await admin.execute("UPDATE command_deliveries SET lease_until=now()-interval '1 second'")
    monkeypatch.setattr(command_deliveries,'published',original)
    await publisher.publish_once()
    assert len(queue.sent)==2 and queue.sent[0]==queue.sent[1]
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'
    assert await admin.fetchval('SELECT attempt_count FROM command_deliveries')==2


async def test_real_redis_ambiguous_publication_is_fenced_by_durable_command(database,real_queue,monkeypatch):
    from workmate_product.coordination.command_queue import QueueRoute
    from workmate_product.persistence.repositories import command_deliveries
    sessions,admin=database
    await admit(sessions)
    publisher=CommandPublisher(sessions,real_queue,CommandQueuePolicy())
    publish=real_queue.publish
    async def ambiguous(*args,**kwargs):
        await publish(*args,**kwargs)
        raise ConnectionError('reply lost after XADD')
    monkeypatch.setattr(real_queue,'publish',ambiguous)
    await publisher.publish_once()
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='pending'
    await admin.execute('UPDATE command_deliveries SET next_attempt_at=now()')
    monkeypatch.setattr(real_queue,'publish',publish)
    await publisher.publish_once()
    route=QueueRoute('node')
    messages=await real_queue.read(route,'worker',count=2)
    assert len(messages)==2 and messages[0].envelope==messages[1].envelope
    async with sessions.begin() as db:
        first=await command_deliveries.classify(db,messages[0].envelope,worker_id='worker',node_id='node')
        assert first.action=='execute'
    await admin.execute("UPDATE runtime_commands SET status='completed'")
    for message in messages:
        async with sessions.begin() as db:
            settled=await command_deliveries.classify(db,message.envelope,worker_id='worker',node_id='node')
            assert settled.action=='ack'
        await real_queue.ack(route,[message.message_id])
    assert await real_queue.pending(route,min_idle_ms=0,count=10)==[]
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='settled'


async def test_invalid_delivery_quarantines_instead_of_retrying_forever(database):
    sessions,admin=database
    await admit(sessions)
    queue=Queue(admin)
    async def invalid(*args): raise ValueError('invalid route')
    queue.publish=invalid
    await CommandPublisher(sessions,queue,CommandQueuePolicy()).publish_once()
    assert await admin.fetchval('SELECT error_code FROM command_deliveries')=='invalid_delivery'
    assert await admin.fetchval('SELECT status FROM command_deliveries')=='quarantined'


async def test_shutdown_joins_blocked_publication_leaving_recoverable_lease(database):
    sessions,admin=database
    await admit(sessions)
    queue=Queue(admin)
    entered=asyncio.Event();cancelled=asyncio.Event();shutdown=asyncio.Event()
    async def blocked(*args):
        entered.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    queue.publish=blocked
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy(lease_seconds=300))
    running=asyncio.create_task(publisher.serve(shutdown))
    try:
        await asyncio.wait_for(entered.wait(),2)
        shutdown.set()
        await asyncio.wait_for(running,1)
        assert cancelled.is_set()
        assert await admin.fetchval('SELECT status FROM command_deliveries')=='publishing'
    finally:
        running.cancel()
        await asyncio.gather(running,return_exceptions=True)


async def test_publication_cycle_fault_retries_and_shutdown_completes(database):
    sessions,admin=database
    shutdown=asyncio.Event()
    publisher=CommandPublisher(sessions,Queue(admin),CommandQueuePolicy(poll_interval_seconds=.01))
    calls=[]
    async def publish():
        calls.append(1)
        if len(calls)==1: raise ConnectionError('private URI')
        shutdown.set()
    publisher.publish_once=publish
    await asyncio.wait_for(publisher.serve(shutdown),2)
    assert len(calls)==2


async def test_repair_preserves_ownerless_warm_holder_delivery(database):
    from test_command_deliveries import seed_residency
    from workmate_product.persistence.repositories import command_deliveries as outbox
    sessions,admin=database
    await admit(sessions)
    await seed_residency(admin)
    await admin.execute("UPDATE command_deliveries SET target_incarnation='warm'")
    publisher=CommandPublisher(sessions,Queue(admin),CommandQueuePolicy())
    await publisher.publish_once()
    await admin.execute("UPDATE command_deliveries SET published_at=now()-interval '1 hour',next_attempt_at=now()-interval '1 hour'")
    await publisher.repair_once()
    assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1
    assert await admin.fetchval('SELECT target_incarnation FROM command_deliveries')=='warm'


async def test_repair_quarantines_superseded_intent_without_redis_republication(database):
    sessions, admin = database
    await admit(sessions)
    queue = Queue(admin)
    publisher = CommandPublisher(sessions, queue, CommandQueuePolicy())
    await admin.execute("UPDATE runtime_sessions SET superseded_at=clock_timestamp()")
    await publisher.publish_once()
    assert queue.sent == []
    await publisher.repair_once()
    assert queue.sent == []
    assert await admin.fetchval('SELECT status FROM command_deliveries') == 'quarantined'
    assert await admin.fetchval('SELECT error_code FROM command_deliveries') == 'session_superseded'
    assert await admin.fetchval('SELECT status FROM runtime_commands') == 'accepted'
