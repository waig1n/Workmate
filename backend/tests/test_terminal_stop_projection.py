"""Real Product Stop admission and projection around an already ended Core Reply."""
import json
import os
from uuid import uuid4

import pytest

# Cross-role integration is collected by the explicit Worker+Product test job.
# The API-only environment intentionally has no AgentScope/Runtime dependency.
pytest.importorskip('agentscope',reason='requires the Worker test environment')
pytest.importorskip('workmate_runtime',reason='requires the Worker package')
helpers=pytest.importorskip('test_running_realtime',reason='requires runtime/tests on PYTHONPATH')
from agentscope.message import TextBlock
from workmate_contracts import Command
from workmate_product.application.control_commands import accept_stop
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.execution.terminal_stop import TerminalStopController
from test_durable_projection import projection_database
from test_durable_lifecycle import consume
prepare_reply=helpers.prepare_reply
from test_parked_stop import quiescent


@pytest.mark.parametrize('project_first',[False,True])
async def test_product_stop_after_core_terminal_preserves_natural_reason_and_cancels_once(projection_database,project_first):
    import asyncpg
    from redis.asyncio import Redis
    from redis.retry import Retry
    from redis.backoff import NoBackoff
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.redis_log import RedisConversationLog
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:
        pytest.skip('requires isolated Redis namespace')
    sessions,admin=projection_database
    schema=await admin.fetchval('SHOW search_path')
    await admin.execute("""UPDATE runtime_sessions SET active_task_id=NULL,status='accepted',
        execution_epoch=0,reply_id=NULL;
        UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL;
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
            kind,expected_epoch,payload,content_digest)
        VALUES ('command','s','c','t','u','StartTask',0,'{}',repeat('a',64));""")
    pool=await asyncpg.create_pool(os.environ['WORKMATE_TEST_POSTGRES_DSN'],
        server_settings={'search_path':schema},min_size=1,max_size=4)
    client=Redis.from_url(url,decode_responses=True,retry=Retry(NoBackoff(),0))
    namespace='terminal-stop-'+uuid4().hex
    log=RedisConversationLog(client,namespace=namespace)
    manager=EventLogManager(sessions,log)
    writer=RealtimeWriterFactory(sessions,log_manager=manager,log=log,policy=RealtimePolicy())
    try:
        prepare,calls=prepare_reply([[TextBlock(text='natural completed answer')]])
        result=await RunningExecutionController(pool,verify_cleanup=quiescent,realtime_writer_factory=writer).start(
            'command','original-worker','node',prepare)
        command=Command(command_id='late-stop',kind='StopTask',conversation_id='c',
            runtime_session_id='s',product_task_id='t',reply_id='stale-observation',expected_epoch=0)
        async with sessions.begin() as db:
            accepted=await accept_stop(db,command,actor_user_id='u')
        assert accepted.status=='accepted'
        controller=TerminalStopController(pool)
        if not project_first:
            assert await controller.settle('late-stop','settler','node')
        while await consume(sessions) is not None:
            pass
        if project_first:
            assert await controller.settle('late-stop','settler','node')
        assert len(calls)==1 and result.status=='completed'
        assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='late-stop'")=='completed'
        assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'")=='cancelled'
        assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='task_cancelled'")==1
        end=json.loads(await admin.fetchval("SELECT payload FROM runtime_events WHERE payload->>'type'='REPLY_END'"))
        assert end['finished_reason']=='completed'
        message=await admin.fetchrow("SELECT content,snapshot_phase,snapshot_version FROM task_messages WHERE role='assistant'")
        assert json.loads(message['content'])['text']=='natural completed answer'
        assert message['snapshot_phase']=='final' and message['snapshot_version']>0
        assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type' LIKE 'TEXT_BLOCK_%'")==0
        assert await admin.fetchval('SELECT runtime_seq FROM runtime_sessions')==result.runtime_seq
        assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
        assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    finally:
        keys=[key async for key in client.scan_iter(match='workmate:'+namespace+':*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()
        await pool.close()
