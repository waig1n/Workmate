"""Committed business facts publish without database locks or execution effects."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from workmate_contracts.realtime import RealtimeCursor
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.persistence.repositories import realtime_outbox as repository
from workmate_product.realtime.business_publisher import BusinessPublisher
from workmate_product.realtime.redis_log import LogRef
from test_realtime_outbox import database,stage

pytestmark=pytest.mark.asyncio


class Manager:
    def __init__(self,sessions,admin):
        self.sessions,self.admin=sessions,admin
        self.ref=LogRef('c',str(uuid4()),str(uuid4()))
        self.current=True
    async def install(self):
        await self.admin.execute('''INSERT INTO conversation_event_logs
            (conversation_id,generation,incarnation,version,status,initialization_token)
            VALUES ('c',$1,$2,1,'active',$3)''',self.ref.generation,self.ref.incarnation,str(uuid4()))
    async def ensure(self,cid):
        assert cid=='c'
        return self.ref
    async def is_current(self,ref):return self.current


class Log:
    def __init__(self,sessions,admin):
        self.sessions,self.admin=sessions,admin
        self.sent=[];self.failure=None;self.wait=None;self.entered=asyncio.Event()
    async def append(self,ref,envelope):
        assert await self.admin.fetchval('SELECT status FROM realtime_business_outbox WHERE event_id=$1',envelope.event_id)=='publishing'
        # A separately acquired row lock proves no publisher transaction spans IO.
        async with self.sessions.begin() as db:
            await db.execute(text('SELECT event_id FROM realtime_business_outbox FOR UPDATE NOWAIT'))
        self.entered.set()
        if self.wait is not None: await self.wait.wait()
        self.sent.append(envelope)
        if self.failure: raise self.failure
        return SimpleNamespace(cursor=RealtimeCursor(conversation_id=ref.conversation_id,
            generation=ref.generation,stream_id=f'{len(self.sent)}-0'))


async def setup(database,*,payload=None):
    sessions,admin=database
    fact=await stage(sessions,payload=payload)
    manager=Manager(sessions,admin);await manager.install()
    log=Log(sessions,admin)
    return fact,manager,log,BusinessPublisher(sessions,manager,log,CommandQueuePolicy())


async def retry(admin):
    await admin.execute("UPDATE realtime_business_outbox SET lease_until=CASE WHEN status='publishing' THEN clock_timestamp()-interval '1 second' ELSE NULL END,next_attempt_at=clock_timestamp()")


async def test_publish_exact_business_fact_after_committed_claim(database):
    fact,manager,log,publisher=await setup(database)
    assert await publisher.publish_once()==1
    assert log.sent[0].event_id==fact.product_event_id
    assert log.sent[0].source=='business' and log.sent[0].business_seq==1
    assert log.sent[0].execution_epoch is None and log.sent[0].payload==fact.payload
    row=await manager.admin.fetchrow('SELECT * FROM realtime_business_outbox')
    assert row['status']=='published' and row['published_stream_id']=='1-0'
    assert row['published_generation']==manager.ref.generation
    assert row['published_incarnation']==manager.ref.incarnation
    assert await publisher.publish_once()==0


async def test_response_loss_repeats_notification_identity_not_business_action(database):
    fact,manager,log,publisher=await setup(database)
    log.failure=ConnectionError('sensitive connection detail')
    await publisher.publish_once()
    assert await manager.admin.fetchval('SELECT status FROM realtime_business_outbox')=='pending'
    await retry(manager.admin);log.failure=None
    await publisher.publish_once()
    assert len(log.sent)==2 and log.sent[0]==log.sent[1]
    assert await manager.admin.fetchval('SELECT count(*) FROM product_events')==1
    assert await manager.admin.fetchval('SELECT attempt_count FROM realtime_business_outbox')==2


async def test_protocol_invalid_blocks_with_sanitized_error_code(database,caplog):
    _,manager,log,publisher=await setup(database,payload={'type':'CUSTOM','name':'TASK_COMPLETED',
        'value':{'number':2**53,'text':'private tool arguments'}})
    await publisher.publish_once()
    assert log.sent==[]
    row=await manager.admin.fetchrow('SELECT * FROM realtime_business_outbox')
    assert row['status']=='blocked' and row['error_code']=='protocol_invalid'
    assert 'private tool arguments' not in caplog.text
    assert await publisher.publish_once()==0


async def test_generation_change_after_xadd_requeues_without_stale_receipt(database):
    _,manager,log,publisher=await setup(database)
    manager.current=False
    await publisher.publish_once()
    row=await manager.admin.fetchrow('SELECT * FROM realtime_business_outbox')
    assert row['status']=='pending' and row['error_code']=='generation_changed'
    assert row['published_generation'] is None and len(log.sent)==1


async def test_pg_fence_rejects_rotation_after_optimistic_current_check(database):
    _,manager,log,publisher=await setup(database)
    async def racing(ref):
        await manager.admin.execute('UPDATE conversation_event_logs SET generation=$1',str(uuid4()))
        return True
    manager.is_current=racing
    await publisher.publish_once()
    row=await manager.admin.fetchrow('SELECT * FROM realtime_business_outbox')
    assert row['status']=='pending' and row['published_generation'] is None


async def test_receipt_failure_leaves_claim_for_lease_expiry_retry(database,monkeypatch):
    _,manager,log,publisher=await setup(database)
    original=repository.published
    async def fail(*args,**kwargs):raise ConnectionError('PG response unavailable')
    monkeypatch.setattr(repository,'published',fail)
    with pytest.raises(ExceptionGroup): await publisher.publish_once()
    assert await manager.admin.fetchval('SELECT status FROM realtime_business_outbox')=='publishing'
    await retry(manager.admin);monkeypatch.setattr(repository,'published',original)
    await publisher.publish_once()
    assert len(log.sent)==2 and log.sent[0]==log.sent[1]
    assert await manager.admin.fetchval('SELECT status FROM realtime_business_outbox')=='published'


async def test_shutdown_cancels_inflight_io_but_keeps_recoverable_claim(database):
    _,manager,log,publisher=await setup(database)
    log.wait=asyncio.Event();shutdown=asyncio.Event()
    serving=asyncio.create_task(publisher.serve(shutdown))
    await asyncio.wait_for(log.entered.wait(),5)
    shutdown.set();await asyncio.wait_for(serving,5)
    assert await manager.admin.fetchval('SELECT status FROM realtime_business_outbox')=='publishing'
    await retry(manager.admin);log.wait=None
    await publisher.publish_once()
    assert await manager.admin.fetchval('SELECT status FROM realtime_business_outbox')=='published'


from test_realtime_redis_log import log as real_log


async def test_real_redis_response_loss_replays_stable_business_id(database,real_log,monkeypatch):
    sessions,admin=database
    fact=await stage(sessions)
    manager=Manager(sessions,admin)
    manager.ref=await real_log.initialize('c',manager.ref.generation)
    await manager.install()
    publisher=BusinessPublisher(sessions,manager,real_log,CommandQueuePolicy())
    original=real_log.append
    async def ambiguous(ref,envelope):
        await original(ref,envelope)
        raise ConnectionError('XADD reply lost')
    monkeypatch.setattr(real_log,'append',ambiguous)
    await publisher.publish_once()
    assert await admin.fetchval('SELECT status FROM realtime_business_outbox')=='pending'
    await retry(admin);monkeypatch.setattr(real_log,'append',original)
    await publisher.publish_once()
    page=await real_log.read(manager.ref,RealtimeCursor(conversation_id='c',generation=manager.ref.generation,stream_id='0-0'))
    assert len(page.entries)==2
    assert page.entries[0].envelope==page.entries[1].envelope
    assert page.entries[0].envelope.event_id==fact.product_event_id
    assert await admin.fetchval('SELECT count(*) FROM product_events')==1
    assert await admin.fetchval('SELECT status FROM realtime_business_outbox')=='published'


async def test_network_retry_has_bounded_delay_without_command_attempt_cutoff(database):
    _,manager,log,publisher=await setup(database)
    publisher.policy=CommandQueuePolicy(max_attempts=1,retry_max_seconds=3)
    await manager.admin.execute('UPDATE realtime_business_outbox SET attempt_count=1000')
    log.failure=ConnectionError('network unavailable')
    await publisher.publish_once()
    row=await manager.admin.fetchrow('''SELECT *,next_attempt_at-clock_timestamp() AS delay
        FROM realtime_business_outbox''')
    assert row['status']=='pending' and row['attempt_count']==1001
    assert 0 < row['delay'].total_seconds() <= 3


async def test_temporary_log_handshake_failure_recovers_head_and_next_event(database):
    from workmate_product.persistence.repositories.event_logs import LogUnavailable
    sessions,admin=database
    first,manager,log,publisher=await setup(database)
    second=await stage(sessions,seq=2)
    original=manager.ensure
    attempts=0
    async def unavailable_once(cid):
        nonlocal attempts
        attempts+=1
        if attempts==1: raise LogUnavailable('Concurrent log rotation requires a new handshake')
        return await original(cid)
    manager.ensure=unavailable_once
    assert await publisher.publish_once()==1
    rows=await admin.fetch('SELECT status,error_code FROM realtime_business_outbox ORDER BY business_seq')
    assert [(r['status'],r['error_code']) for r in rows]==[('pending','log_unavailable'),('pending',None)]
    assert log.sent==[]
    await retry(admin)
    assert await publisher.publish_once()==1
    assert await publisher.publish_once()==1
    assert [e.event_id for e in log.sent]==[first.product_event_id,second.product_event_id]
    assert await admin.fetchval("SELECT count(*) FROM realtime_business_outbox WHERE status='published'")==2
    assert await admin.fetchval('SELECT count(*) FROM product_events')==2


async def test_transport_value_error_is_retryable_not_poison_data(database):
    _,manager,log,publisher=await setup(database)
    log.failure=ValueError('transport state is temporarily unavailable')
    await publisher.publish_once()
    row=await manager.admin.fetchrow('SELECT status,error_code FROM realtime_business_outbox')
    assert dict(row)=={'status':'pending','error_code':'log_unavailable'}
    await retry(manager.admin);log.failure=None
    await publisher.publish_once()
    assert await manager.admin.fetchval('SELECT status FROM realtime_business_outbox')=='published'


async def test_maximum_product_payload_publishes_with_envelope_metadata(database,real_log):
    import json
    from workmate_product.projections.store import ProjectedEvent, append_business_events
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_product.realtime.redis_log import RedisConversationLog
    sessions,admin=database
    log=RedisConversationLog(real_log.client,namespace=real_log.namespace,
        retention_ms=60000,max_bytes=262144+4096,ttl_seconds=120)
    manager=EventLogManager(sessions,log)
    ref=await manager.ensure('c')
    payload={'type':'CUSTOM','name':'maximum_business_payload','value':''}
    payload['value']='x'*(262144-len(json.dumps(payload,allow_nan=False,sort_keys=True).encode()))
    # Match the existing Product admission budget exactly, including its JSON
    # separators. The realtime wrapper must have independent metadata headroom.
    assert len(json.dumps(payload,allow_nan=False,sort_keys=True).encode())==262144
    async with sessions.begin() as db:
        assert await append_business_events(db,'c',operation_key='large_business_notification',
            task_id='t',reply_id='reply',items=[ProjectedEvent(key='notice',event_type='CUSTOM',payload=payload)])==(1,)
    assert await admin.fetchval('SELECT status FROM realtime_business_outbox')=='pending'
    publisher=BusinessPublisher(sessions,manager,log,CommandQueuePolicy())
    assert await publisher.publish_once()==1
    row=await admin.fetchrow('SELECT * FROM realtime_business_outbox')
    assert row['status']=='published',row['error_code']
    page=await log.read(ref,RealtimeCursor(conversation_id='c',generation=ref.generation,stream_id='0-0'))
    assert len(page.entries)==1 and page.entries[0].envelope.event_id==row['event_id']
    assert page.entries[0].envelope.payload==payload
    assert page.entries[0].cursor.stream_id==row['published_stream_id']
    assert 262144 < page.window.payload_bytes <= 262144+4096
