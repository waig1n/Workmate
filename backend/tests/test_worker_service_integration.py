"""Product only writes durable commands; a separate Worker discovers all work."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from workmate_contracts import Command
from workmate_product.application.control_commands import accept_resume, accept_stop
from test_durable_projection import projection_database
from test_durable_interactions import consume


@pytest.mark.parametrize('mode',['complete','parked_stop','running_stop','approval_expiry'])
async def test_autonomous_worker_lifecycle(projection_database,mode):
    interpreter = os.getenv('WORKMATE_TEST_CORE_PYTHON')
    if not interpreter:
        pytest.skip('requires real Core interpreter')
    redis_url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not redis_url:
        pytest.skip('requires isolated local Redis')
    from uuid import uuid4
    from redis.asyncio import Redis
    namespace='service-test-'+uuid4().hex
    redis=Redis.from_url(redis_url,decode_responses=True)
    sessions, db = projection_database
    root = Path(__file__).resolve().parents[2]
    await db.execute("UPDATE runtime_sessions SET status='accepted',active_task_id=NULL,execution_epoch=0,reply_id=NULL")
    await db.execute("UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL")
    await db.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('0',64))""")
    schema = await db.fetchval('SELECT current_schema()')
    paths = [str(root / path) for path in
             ('runtime', 'packages/contracts/src', 'packages/product/src')]
    if os.environ.get('PYTHONPATH'):
        paths.append(os.environ['PYTHONPATH'])
    worker = await asyncio.create_subprocess_exec(interpreter,str(root/'runtime/tests/run_service_worker.py'),schema,mode,
        env={**os.environ,'WORKMATE_TEST_REALTIME_NAMESPACE':namespace,'PYTHONPATH':os.pathsep.join(paths)},
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def completed(cid):
        async with asyncio.timeout(15):
            while await db.fetchval('SELECT status FROM runtime_commands WHERE id=$1',cid)!='completed':
                await asyncio.sleep(.03)
    async def project():
        while await consume(sessions):
            pass
    async def allow(*args):
        return True
    try:
        ready = await asyncio.wait_for(worker.stdout.readline(),15)
        if ready != b'ready\n':
            _, stderr = await asyncio.wait_for(worker.communicate(),5)
            pytest.fail(f'Worker did not become ready: {ready!r}\n{stderr.decode()}')
        if mode == 'running_stop':
            assert await asyncio.wait_for(worker.stdout.readline(),10) == b'model_waiting\n'
            await project()
        else:
            await completed('start')
            await project()
        reply = await db.fetchval('SELECT reply_id FROM runtime_task_executions')
        await asyncio.sleep(2.2)  # Cross the original lease with no Worker messages.
        decisions = [] if mode == 'running_stop' else ['a','b'] if mode == 'complete' else ['a']
        for call in decisions:
            execution = await db.fetchrow('SELECT * FROM runtime_task_executions')
            state = json.loads(await db.fetchval('SELECT state FROM task_projection_states'))
            pending = state['pending_interactions'][0]
            command = Command(command_id='resume-'+call,kind='ResumeInteraction',conversation_id='c',
                runtime_session_id='s',product_task_id='t',reply_id=reply,expected_epoch=1,
                interaction_id=pending['interaction_id'],request_version=pending['request_version'],
                payload={'decision_version':'decision-'+call,'snapshot_id':execution['snapshot_id'],
                    'state_version':execution['state_version'],'decisions':[{'tool_call_id':call,'approved':True}]})
            async with sessions.begin() as transaction:
                await accept_resume(transaction,command,actor_user_id='u',authorize_resume=allow)
            await completed(command.command_id)
            await project()
        if mode != 'complete':
            async with sessions.begin() as transaction:
                if mode == 'approval_expiry':
                    from workmate_product.application.approval_expiry import expire_approval
                    await db.execute("UPDATE task_events SET created_at=now()-interval '25 hours' WHERE event_type='hitl_request'")
                    assert await expire_approval(transaction,'c')
                else:
                    await accept_stop(transaction,Command(command_id='stop',kind='StopTask',conversation_id='c',
                        runtime_session_id='s',product_task_id='t'),actor_user_id='u')
            stop_id = await db.fetchval("SELECT id FROM runtime_commands WHERE kind='StopTask'")
            await completed(stop_id)
            await completed('start')
            await project()
        assert await db.fetchval('SELECT status FROM tasks') == ('completed' if mode=='complete' else 'cancelled')
        assert await db.fetchval('SELECT reply_id FROM runtime_task_executions') == reply
        if mode != 'running_stop':
            phase=await db.fetchval('SELECT snapshot_phase FROM task_messages WHERE snapshot_version>0')
            assert phase == ('final' if mode=='complete' else 'interrupted')
            assert 'before' in await db.fetchval('SELECT content::text FROM task_messages WHERE snapshot_version>0')
        assert not await db.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type' LIKE 'TEXT_BLOCK_%'")
        assert not await db.fetchval("SELECT count(*) FROM product_events WHERE payload->>'type' LIKE 'TEXT_MESSAGE_%'")
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
        assert await db.fetchval('SELECT active_product_task_id FROM conversations') == 't'
        worker.stdin.write(b'shutdown\n')
        await worker.stdin.drain()
        final = json.loads(await asyncio.wait_for(worker.stdout.readline(),10))
        assert final == {'tool_calls':len(decisions),'failures':{}}
        await asyncio.wait_for(worker.wait(),5)
        assert worker.returncode == 0, (await worker.stderr.read()).decode()
    finally:
        if worker.returncode is None:
            worker.stdin.close()
            try:
                await asyncio.wait_for(worker.wait(),5)
            except TimeoutError:
                worker.kill()
                await worker.wait()

        keys=[key async for key in redis.scan_iter(match=f'workmate:{namespace}:*')]
        if keys: await redis.delete(*keys)
        await redis.aclose()
