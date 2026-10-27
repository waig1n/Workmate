"""Real Core, controller, display store and Redis on isolated Product schemas."""
import json
import os
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolCallBlock
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from workmate_contracts.realtime import MessageSnapshotReference
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.persistence.models import TaskMessage
from workmate_product.persistence.repositories.message_snapshots import from_message
from workmate_product.realtime.log_manager import EventLogManager
from workmate_product.realtime.redis_log import RedisConversationLog
from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.stopping import ParkedStopController
from workmate_runtime.transport.product_pool import ProductWorkerPool
from test_core_engine import ConfirmTool
from test_execution_context import database
from test_parked_stop import quiescent
from test_resume_controller import prepare_resume
from test_running_realtime import BODY_TYPES, prepare_reply


@pytest.mark.asyncio
@pytest.mark.parametrize('parked_stop', [False, True])
async def test_real_writer_commits_display_before_execution_boundary(database, parked_stop):
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires isolated Redis namespace')
    sessions, admin = database
    policy = RealtimePolicy()
    client = Redis.from_url(url, decode_responses=True, retry=Retry(NoBackoff(), 0))
    namespace = 'display-composition-' + uuid4().hex
    log = RedisConversationLog(client, namespace=namespace,
        retention_ms=policy.retention_seconds * 1000, max_bytes=policy.max_log_bytes,
        ttl_seconds=policy.ttl_seconds)
    manager = EventLogManager(sessions, log)
    factory = RealtimeWriterFactory(sessions, log_manager=manager, log=log, policy=policy)
    controller = RunningExecutionController(ProductWorkerPool(sessions.kw['bind']),
        verify_cleanup=quiescent, realtime_writer_factory=factory)
    tool = ConfirmTool() if parked_stop else None
    blocks = [TextBlock(text='实际保存的正文')]
    if tool is not None:
        blocks.append(ToolCallBlock(id='call', name=tool.name, input='{}'))
    prepare, _ = prepare_reply([blocks], tool=tool)
    try:
        result = await controller.start('cmd', 'owner', 'node', prepare)
        assert result is not None
        if parked_stop:
            assert result.status == 'awaiting_interaction' and tool.calls == 0
            await admin.execute("UPDATE tasks SET status='cancelling' WHERE id='t'")
            await admin.execute("""INSERT INTO runtime_commands(
                id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
                VALUES ('stop','s','c','t','u','StopTask','{}',repeat('a',64))""")
            result = await ParkedStopController(controller.pool,
                realtime_writer_factory=factory).stop('stop', 'owner', 'node',
                prepare_resume(tool), verify_cleanup=quiescent)
            assert result.status == 'interrupted' and tool.calls == 0
        else:
            assert result.status == 'completed'
        async with sessions.begin() as db:
            from workmate_contracts.realtime import message_id
            row = await db.get(TaskMessage, message_id('t', result.reply_id))
            cut = from_message(row)
        assert cut.phase == ('interrupted' if parked_stop else 'final')
        assert ''.join(b.text for b in cut.blocks) == '实际保存的正文'
        boundary = json.loads(await admin.fetchval("""SELECT payload FROM runtime_events
            WHERE payload->>'type'='EXECUTION_BOUNDARY' ORDER BY runtime_seq DESC LIMIT 1"""))
        assert boundary['display_snapshot'] == MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json')
        types = [r['type'] for r in await admin.fetch("SELECT payload->>'type' AS type FROM runtime_events")]
        assert not BODY_TYPES.intersection(types)
        ref = await manager.ensure('c')
        window = await log.window(ref)
        page = await log.read(ref, window.floor)
        assert any(event.envelope.text is not None for event in page.entries)
    finally:
        keys = [key async for key in client.scan_iter(match='workmate:' + namespace + ':*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_proven,boundary_mutation', [
    (True, None), (False, None),
    (True, 'missing_failure'), (True, 'forged_failure'),
    (True, 'forged_finished_reason'), (True, 'unproven_cleanup'),
])
async def test_disconnected_redis_drains_running_tool_and_preserves_display(
        database, cleanup_proven, boundary_mutation):
    import asyncio
    from test_running_stop import WaitingTool
    from workmate_runtime.execution.controller import ExecutionAborted
    from workmate_runtime.transport.delivery import DeliveryStore, DeliveryConflict
    from test_worker_delivery_store import BASELINE, CAPTURE
    url = os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires isolated Redis namespace')
    sessions, admin = database
    # Redis refusal is immediate; give real PostgreSQL snapshot commits enough
    # time so host load does not become an unrelated snapshot-storage failure.
    policy = RealtimePolicy(outage_grace_seconds=1, operation_timeout_seconds=.5,
                            snapshot_interval_seconds=.1)
    client = Redis.from_url(url,decode_responses=True,retry=Retry(NoBackoff(),0))
    unavailable = Redis(unix_socket_path='/tmp/workmate-no-redis-'+uuid4().hex,
                        decode_responses=True,retry=Retry(NoBackoff(),0))
    namespace = 'display-fault-'+uuid4().hex
    log = RedisConversationLog(client,namespace=namespace)
    manager = EventLogManager(sessions,log)
    await manager.ensure('c')
    log.client = unavailable
    factory = RealtimeWriterFactory(sessions,log_manager=manager,log=log,policy=policy)
    async def cleanup(*args):
        return cleanup_proven and await quiescent(*args)
    pool = ProductWorkerPool(sessions.kw['bind'])
    async def seal(owner, reply_id, status, snapshot):
        async with pool.acquire() as db:
            return await DeliveryStore(db).record_seal(owner, 'node', reply_id=reply_id,
                status=status, snapshot=snapshot, payload=CAPTURE)
    controller = RunningExecutionController(pool, verify_cleanup=cleanup,
        realtime_writer_factory=factory, seal_terminal=seal)
    tool = WaitingTool()
    prepare, _ = prepare_reply([[TextBlock(text='saved prefix'),
        ToolCallBlock(id='call',name=tool.name,input='{}')]],tool=tool)
    async def prepare_with_baseline(owner, snapshot, guard):
        async with pool.acquire() as db:
            await DeliveryStore(db).record_baseline(owner, 'node', BASELINE)
        return await prepare(owner, snapshot, guard)
    try:
        if cleanup_proven:
            result = await asyncio.wait_for(controller.start('cmd','owner','node',prepare_with_baseline),8)
            assert result.status == 'failed'
            boundary=json.loads(await admin.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='EXECUTION_BOUNDARY'"))
            assert boundary['execution_failure']=={'code':'realtime_unavailable','finished_reason':'interrupted'}
            assert boundary['resource_cleanup']=={'core_drained':True,'resources_quiescent':True}
            from workmate_contracts.realtime import message_id
            async with sessions.begin() as db:
                row=await db.get(TaskMessage,message_id('t',result.reply_id))
                cut=from_message(row)
            assert cut.phase=='interrupted' and cut.incomplete and cut.covered_cursor is None
            assert ''.join(b.text for b in cut.blocks)=='saved prefix'
            assert boundary['display_snapshot']==MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json')
            types=[r['type'] for r in await admin.fetch("SELECT payload->>'type' AS type FROM runtime_events")]
            assert 'TOOL_RESULT_END' in types and 'MODEL_CALL_END' in types
            assert not BODY_TYPES.intersection(types)
            assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_seals') == 1
            if boundary_mutation:
                if boundary_mutation == 'missing_failure':
                    boundary.pop('execution_failure')
                elif boundary_mutation == 'forged_failure':
                    boundary['execution_failure']['code'] = 'forged_failure'
                elif boundary_mutation == 'forged_finished_reason':
                    boundary['execution_failure']['finished_reason'] = 'completed'
                else:
                    boundary['resource_cleanup']['resources_quiescent'] = False
                await admin.execute("""UPDATE runtime_events SET payload=$1::jsonb
                    WHERE payload->>'type'='EXECUTION_BOUNDARY'""", json.dumps(boundary))
                with pytest.raises(DeliveryConflict):
                    await DeliveryStore(admin).record_capture(result.owner, 'node', CAPTURE)
                assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures') == 0
            else:
                digest = await DeliveryStore(admin).record_capture(result.owner, 'node', CAPTURE)
                assert digest == await DeliveryStore(admin).record_capture(result.owner, 'node', CAPTURE)
                assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures') == 1
        else:
            with pytest.raises(ExecutionAborted):
                await asyncio.wait_for(controller.start('cmd','owner','node',prepare_with_baseline),8)
            assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type'='EXECUTION_BOUNDARY'")==0
            assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='cmd'")=='claimed'
            assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_seals') == 0
            assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures') == 0
        assert tool.calls==1 and tool.exited==1
    finally:
        keys=[key async for key in client.scan_iter(match='workmate:'+namespace+':*')]
        if keys:
            await client.delete(*keys)
        await unavailable.aclose()
        await client.aclose()
