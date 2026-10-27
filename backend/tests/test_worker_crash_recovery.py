"""SIGKILL + actual Streams Worker restart preserves uncertain side-effect fences."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from test_durable_projection import projection_database, consume
from workmate_product.application.execution_quarantine import report_expired_executions
from workmate_product.coordination.command_queue import RedisCommandQueue
from workmate_product.coordination.command_publisher import CommandPublisher
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.persistence.repositories.command_deliveries import stage


async def test_sigkill_restart_reports_unknown_without_reexecuting_tool(projection_database,tmp_path):
    interpreter=os.getenv('WORKMATE_TEST_CORE_PYTHON')
    redis_url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not interpreter or not redis_url:
        pytest.skip('requires isolated Redis and Core interpreter')
    sessions,admin=projection_database
    root=Path(__file__).resolve().parents[2]
    namespace='crash-recovery-'+uuid4().hex
    effect=tmp_path/'effect.txt'
    redis=Redis.from_url(redis_url,decode_responses=True,retry=Retry(NoBackoff(),0),retry_on_timeout=False)
    queue=RedisCommandQueue(redis,namespace=namespace)
    publisher=CommandPublisher(sessions,queue,CommandQueuePolicy())
    await admin.execute("UPDATE runtime_sessions SET status='accepted',active_task_id=NULL,execution_epoch=0,reply_id=NULL")
    await admin.execute("UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL")
    await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('0',64))""")
    async with sessions.begin() as db:
        await stage(db,'start')
    assert await publisher.publish_once()==1
    schema=await admin.fetchval('SELECT current_schema()')
    environment={**os.environ,'WORKMATE_TEST_REALTIME_NAMESPACE':namespace,'WORKMATE_TEST_EFFECT_FILE':str(effect),
        'PYTHONPATH':os.pathsep.join(str(root/p) for p in ('backend','runtime','runtime/tests','packages/product/src','packages/contracts/src'))}
    children=[]
    async def spawn():
        worker=await asyncio.create_subprocess_exec(interpreter,str(root/'runtime/tests/run_crash_worker.py'),schema,
            env=environment,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        children.append(worker)
        ready=await asyncio.wait_for(worker.stdout.readline(),15)
        assert ready==b'ready\n', (ready,(await worker.stderr.read()).decode())
        return worker
    try:
        first=await spawn()
        assert await asyncio.wait_for(first.stdout.readline(),15)==b'tool_waiting\n'
        async with asyncio.timeout(10):
            while not await admin.fetchval('SELECT count(*) FROM task_messages WHERE snapshot_version>0'):
                await asyncio.sleep(.05)
        while await consume(sessions):
            pass
        assert effect.read_text()=='one effect\n'
        first.kill()  # No finally block, graceful shutdown, or test-written lease expiry.
        await asyncio.wait_for(first.wait(),5)
        assert first.returncode==-9
        async with asyncio.timeout(6):
            while await admin.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions'):
                await asyncio.sleep(.05)
        owner=dict(await admin.fetchrow('SELECT * FROM runtime_sessions'))
        second=await spawn()
        await publisher.repair_once()
        assert await admin.fetchval('SELECT status FROM tasks')=='failed'
        assert await report_expired_executions(sessions)==0
        await asyncio.sleep(.3)
        assert effect.read_text()=='one effect\n'
        assert dict(await admin.fetchrow('SELECT * FROM runtime_sessions'))==owner
        assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='start'")=='claimed'
        assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
        # New browser RESET uses the saved partial body and failed Task, even if
        # no final Core snapshot can exist after this hard process failure.
        from app.services.conversation_business_state import ConversationBusinessState
        from workmate_product.config.realtime import RealtimePolicy
        from workmate_product.realtime.composition import create_realtime_log
        from workmate_product.realtime.log_manager import EventLogManager
        from workmate_product.realtime.reader import ConversationReaderHub
        from workmate_product.realtime.reset_builder import ResetBuilder
        log=create_realtime_log(redis,namespace=namespace,policy=RealtimePolicy())
        hub=ConversationReaderHub(log,block_ms=20)
        try:
            builder=ResetBuilder(EventLogManager(sessions,log),log,hub,ConversationBusinessState(sessions,'u').read_view)
            reset=await asyncio.wait_for(builder.build('c'),5)
            try:
                assert reset.reset.business_state['tasks'][0]['status']=='failed'
                assert reset.reset.sync_status=='incomplete'
                assert reset.active.refresh_possible is False
                assert any('persisted partial answer' in b.text for s in reset.reset.snapshots for b in s.blocks)
            finally:
                await reset.subscription.aclose()
        finally:
            await hub.close()
        second.stdin.write(b'shutdown\n')
        await second.stdin.drain()
        assert json.loads(await asyncio.wait_for(second.stdout.readline(),5))=={'tool_calls':0}
        await asyncio.wait_for(second.wait(),5)
        assert second.returncode==0
    finally:
        for worker in children:
            if worker.returncode is None:
                worker.kill()
                await worker.wait()
        keys=[key async for key in redis.scan_iter(match=f'workmate:{namespace}:*')]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
