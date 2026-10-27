"""Real pauses/restarts/data loss on the driver's dedicated containers only."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time

import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from test_command_admission import database as admission_database
from test_command_deliveries import admit
from test_postgres_execution_store import database as execution_database
from workmate_runtime.transport.postgres import ExecutionStore, LostOwnership
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.coordination.command_queue import RedisCommandQueue, QueueRoute
from workmate_product.coordination.command_publisher import CommandPublisher
from workmate_product.persistence.repositories.command_deliveries import stage
from scripts.reliability.dependency_drill import POLICY, validate_owner

pytestmark=pytest.mark.asyncio


def fault(role, operation):
    owner=os.environ.get('FAULT_DEPENDENCY_OWNER')
    if not owner: pytest.skip('dedicated dependency driver required')
    name=owner+'-'+role
    data=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
    validate_owner(owner,name,data['Config']['Labels'])
    subprocess.run(['docker',operation,name],check=True,capture_output=True,timeout=15)


def record(name, value):
    Path(os.environ['FAULT_DEPENDENCY_EVIDENCE'],name+'.json').write_text(json.dumps(value,indent=2)+'\n')


def client():
    return Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'],decode_responses=True,
        socket_timeout=.5,socket_connect_timeout=.5,retry=Retry(NoBackoff(),0),retry_on_timeout=False)


async def test_redis_pause_restart_and_loss_repair_unclaimed(admission_database):
    sessions,admin=admission_database
    await admit(sessions)
    redis=client(); queue=RedisCommandQueue(redis,namespace='fault-unclaimed')
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy(
        redelivery_after_seconds=10,repair_interval_seconds=1,retry_max_seconds=1))
    evidence={'policy':POLICY}
    start=time.monotonic()
    fault('redis','pause')
    try:
        await asyncio.wait_for(publisher.publish_once(),5)
        evidence['detection_seconds']=time.monotonic()-start
        assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'
        assert await admin.fetchval('SELECT status FROM command_deliveries')=='pending'
        await asyncio.sleep(max(0,POLICY['pause_seconds']-(time.monotonic()-start)))
    finally:
        fault('redis','unpause')
        evidence['pause_seconds']=time.monotonic()-start
    recovered=time.monotonic()
    async with asyncio.timeout(POLICY['recovery_deadline_seconds']):
        while await admin.fetchval('SELECT status FROM command_deliveries')!='published':
            await publisher.publish_once(); await asyncio.sleep(.1)
    evidence['publication_recovery_seconds']=time.monotonic()-recovered
    # Restart actual dedicated Redis, then erase only its entire disposable DB.
    fault('redis','restart')
    restart=time.monotonic()
    async with asyncio.timeout(POLICY['recovery_deadline_seconds']):
        while True:
            try:
                await redis.ping(); break
            except (RedisConnectionError,RedisTimeoutError):
                await asyncio.sleep(.1)
    evidence['restart_recovery_seconds']=time.monotonic()-restart
    await redis.flushdb()
    lost=time.monotonic()
    async with asyncio.timeout(POLICY['recovery_deadline_seconds']):
        while await admin.fetchval('SELECT max(delivery_generation) FROM command_deliveries')<2:
            await publisher.repair_once(); await asyncio.sleep(.1)
        await publisher.publish_once()
    messages=await queue.read(QueueRoute('node'),'observer',count=2)
    assert len(messages)==1 and messages[0].envelope.delivery_generation==2
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'
    evidence['loss_repair_seconds']=time.monotonic()-lost
    evidence['deliveries']=[dict(row) for row in await admin.fetch('SELECT delivery_generation,status FROM command_deliveries ORDER BY delivery_generation')]
    evidence['passed']=True
    record('redis-unclaimed',evidence)
    await redis.aclose()


async def test_claimed_command_is_not_republished_after_redis_loss(execution_database):
    pool,admin=execution_database
    # Execution-store fixture predates Product's run_id; add schema only before admission.
    await admin.execute('ALTER TABLE tasks ADD COLUMN run_id varchar(128)')
    schema=await admin.fetchval('SELECT current_schema()')
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':schema}})
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    redis=client(); queue=RedisCommandQueue(redis,namespace='fault-claimed')
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy(redelivery_after_seconds=10,repair_interval_seconds=1))
    try:
        async with sessions.begin() as db: await stage(db,'command')
        await publisher.publish_once()
        async with pool.acquire() as db:
            owner=await ExecutionStore(db).claim_start('command','original','node',lease_seconds=300)
        assert owner is not None
        before=dict(await admin.fetchrow('SELECT owner_id,execution_epoch,active_task_id FROM runtime_sessions'))
        await redis.flushdb()
        await asyncio.sleep(POLICY['redelivery_after_seconds']+.2)
        await publisher.repair_once(); await publisher.publish_once()
        assert await admin.fetchval('SELECT count(*) FROM command_deliveries')==1
        assert await admin.fetchval('SELECT status FROM runtime_commands')=='claimed'
        async with pool.acquire() as db:
            assert await ExecutionStore(db).claim_start('command','competitor','node',lease_seconds=30) is None
        assert dict(await admin.fetchrow('SELECT owner_id,execution_epoch,active_task_id FROM runtime_sessions'))==before
        record('redis-claimed',{'passed':True,'owner_unchanged':True,'delivery_generations':1,
            'scope':'real production claim and publisher; no model/tool executed'})
    finally:
        await redis.aclose(); await engine.dispose()


async def test_pg_pause_expired_authority_cannot_renew_or_reclaim(execution_database):
    pool,admin=execution_database
    async with pool.acquire() as db:
        owner=await ExecutionStore(db).claim_start('command','original','node',lease_seconds=POLICY['lease_seconds'])
        assert owner is not None
        before=dict(await admin.fetchrow('SELECT owner_id,execution_epoch,active_task_id FROM runtime_sessions'))
        start=time.monotonic(); fault('postgres','pause')
        async def restore():
            await asyncio.sleep(POLICY['pause_seconds'])
            fault('postgres','unpause')
        restoring=asyncio.create_task(restore())
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(POLICY['operation_timeout_seconds']):
                    await ExecutionStore(db).renew(owner,lease_seconds=30)
        finally:
            await restoring
        elapsed=time.monotonic()-start
        assert elapsed<POLICY['recovery_deadline_seconds']
        with pytest.raises(LostOwnership):
            await ExecutionStore(db).renew(owner,lease_seconds=30)
        assert await ExecutionStore(db).claim_start('command','competitor','node',lease_seconds=30) is None
        assert dict(await admin.fetchrow('SELECT owner_id,execution_epoch,active_task_id FROM runtime_sessions'))==before
        assert await admin.fetchval('SELECT lease_until<clock_timestamp() FROM runtime_sessions')
        record('postgres-authority',{'passed':True,'pause_and_timeout_completion_seconds':elapsed,
            'expired_owner_rejected':True,'automatic_takeover_rejected':True,
            'manual_handling_required':True,'scope':'production execution store; no live tool subprocess'})
