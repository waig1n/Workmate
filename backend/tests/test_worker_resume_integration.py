"""One real Worker process, Product decision admission and durable projection."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from test_realtime_redis_log import log  # noqa: F401
from workmate_contracts import Command
from workmate_product.application.control_commands import accept_resume, accept_stop
from test_durable_projection import projection_database  # noqa: F401
from test_durable_interactions import consume


@pytest.mark.parametrize('finish', ['complete','stop'])
@pytest.mark.parametrize('supervised', [False,True])
async def test_product_partial_decisions_resume_core_without_losing_reply_or_text(projection_database, log, finish, supervised):
    interpreter = os.getenv('WORKMATE_TEST_CORE_PYTHON')
    if not interpreter:
        pytest.skip('requires real Core interpreter')
    sessions, admin = projection_database
    root = Path(__file__).resolve().parents[2]
    await admin.execute("UPDATE runtime_sessions SET status='accepted',active_task_id=NULL,execution_epoch=0,reply_id=NULL")
    await admin.execute("UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL")
    await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('0',64))""")
    schema = await admin.fetchval('SELECT current_schema()')
    paths = [str(root / path) for path in
             ('runtime', 'packages/contracts/src', 'packages/product/src')]
    if os.environ.get('PYTHONPATH'):
        paths.append(os.environ['PYTHONPATH'])
    worker = await asyncio.create_subprocess_exec(interpreter,str(root/'runtime/tests/run_control_worker.py'),schema,
        env={**os.environ,'PYTHONPATH':os.pathsep.join(paths),
             'WORKMATE_TEST_REALTIME_NAMESPACE':log.namespace},
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def run(command):
        worker.stdin.write((command+'\n').encode())
        await worker.stdin.drain()
        line = await asyncio.wait_for(worker.stdout.readline(),20)
        assert line, (await worker.stderr.read()).decode()
        return json.loads(line)
    async def drain():
        while await consume(sessions):
            pass
    async def allow(*args):
        return True
    try:
        ready = await asyncio.wait_for(worker.stdout.readline(),15)
        if ready != b'ready\n':
            _, stderr = await asyncio.wait_for(worker.communicate(),5)
            pytest.fail(f'Worker did not become ready: {ready!r}\n{stderr.decode()}')
        first = await run('start')
        assert first['tool_calls'] == 0
        await drain()
        state = json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
        original_metrics = state['metrics']
        original = await admin.fetchrow("SELECT * FROM task_messages WHERE role='assistant'")
        assert json.loads(original['content'])['text'] == 'before'
        assert original['snapshot_phase'] == 'approval'
        assert 'text' not in state
        reply = first['result']['reply_id']
        version = state['pending_interactions'][0]['request_version']
        if supervised:
            worker.stdin.write(b'supervise\n')
            await worker.stdin.drain()
            assert await asyncio.wait_for(worker.stdout.readline(),10)==b'supervising\n'
            await asyncio.sleep(3)  # Longer than supervisor lease; no notifications sent.
        async def completed(cid):
            async with asyncio.timeout(15):
                while await admin.fetchval('SELECT status FROM runtime_commands WHERE id=$1',cid)!='completed':
                    await asyncio.sleep(0.03)
            row = await admin.fetchrow('SELECT * FROM runtime_task_executions')
            return {'result':dict(row)}
        decisions = [('resume-a','a'),('resume-b','b')] if finish=='complete' else [('resume-a','a')]
        for cid, call in decisions:
            execution = await admin.fetchrow('SELECT * FROM runtime_task_executions')
            state = json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
            pending = state['pending_interactions'][0]
            assert pending['request_version'] == version
            command = Command(command_id=cid,kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=reply,expected_epoch=1,interaction_id=pending['interaction_id'],
                request_version=version,payload={'decision_version':cid,'snapshot_id':execution['snapshot_id'],
                'state_version':execution['state_version'],'decisions':[{'tool_call_id':call,'approved':True}]})
            async with sessions.begin() as db:
                await accept_resume(db,command,actor_user_id='u',authorize_resume=allow)
            result = await completed(cid) if supervised else await run(cid)
            assert result['result']['reply_id'] == reply
            if not supervised:
                assert result['tool_calls'] == (1 if call=='a' else 2)
            assert await admin.fetchval('SELECT status FROM runtime_commands WHERE id=$1',cid) == 'completed'
            await drain()
        if finish=='stop':
            async with sessions.begin() as db:
                await accept_stop(db,Command(command_id='stop',kind='StopTask',conversation_id='c',
                    runtime_session_id='s',product_task_id='t'),actor_user_id='u')
            stopped = await completed('stop') if supervised else await run('stop')
            if not supervised:
                assert stopped['tool_calls']==1 and stopped['result']['pending_tool_call_ids']==[]
            assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
            await drain()
        if supervised:
            final = json.loads(await asyncio.wait_for(worker.stdout.readline(),10))
            assert final['tool_calls']==len(decisions)
        state = json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
        final_message = await admin.fetchrow("SELECT * FROM task_messages WHERE role='assistant'")
        assert json.loads(final_message['content'])['text'] == ('beforeafter' if finish=='complete' else 'before')
        assert final_message['id'] == original['id']
        assert final_message['snapshot_version'] > original['snapshot_version']
        assert final_message['snapshot_phase'] == ('final' if finish=='complete' else 'interrupted')
        assert 'text' not in state
        assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type' LIKE 'TEXT_BLOCK_%'") == 0
        if finish=='stop':
            assert state['pending_interactions']==[]
            assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_abandoned'")==1
        assert state['metrics']['started_at'] == original_metrics['started_at']
        assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_resumed'") == len(decisions)
        assert await admin.fetchval('SELECT status FROM tasks') == ('completed' if finish=='complete' else 'cancelled')
        assert await admin.fetchval('SELECT active_product_task_id FROM conversations') == 't'
        assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') == 't'
    finally:
        if worker.returncode is None:
            worker.stdin.close()
            try:
                await asyncio.wait_for(worker.wait(),5)
            except TimeoutError:
                worker.kill()
                await worker.wait()
