"""Full metadata + Product + Core + two real compositions across approval restart.

Model, provider attestation and host file tools are fixtures; PostgreSQL, lifecycle, file ownership,
command handling, permission grants and artifact delivery are real local code.
The Streams suite also runs this scenario with a real production sandbox.
"""
from polling_worker import polling_composition
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from agentscope.message import TextBlock, ToolCallBlock
from workmate_contracts import Command
from workmate_product.application.control_commands import accept_resume, accept_stop
from workmate_product.application.full_access import accept_full_access
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from test_core_engine import ScriptModel
from test_execution_context import database, record
from test_worker_workspaces import StubWorkspace
from test_multiworker_composition import wait_for
from test_realtime_log_manager import MemoryLog
from workmate_contracts.realtime import RealtimeCursor


@pytest.mark.parametrize('action', ['resume','full_access','revoked_stop','partial_resume'])
async def test_composed_cold_control_keeps_original_reply_and_delivery_baseline(database,tmp_path,monkeypatch,action,polling_composition):
    from app.services.durable_runtime import compose_product_runtime
    sessions,admin=database
    await admin.execute("UPDATE tasks SET approval_profile='strict'")
    await record(sessions)
    builds=[]
    def model(*_):
        batches=([[ToolCallBlock(id='write',name='Write',input=json.dumps({
            'file_path':'/workspace/output/result.txt','content':'approved once'}))]]
            if not builds else [[TextBlock(text='finished')]])
        if not builds and action=='partial_resume':
            batches[0].append(ToolCallBlock(id='write2',name='Write',input=json.dumps({
                'file_path':'/workspace/output/result2.txt','content':'second approved once'})))
        builds.append(batches)
        return ScriptModel(batches)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',model)
    async def isolated(*_):
        return {'isolation':'bubblewrap','containment':'pid_namespace','instance_id':'','evidence':{}}
    monkeypatch.setattr('workmate_runtime.sandbox.bubblewrap.BubblewrapSandboxProvider.verify_task_isolation',isolated)
    if os.getenv('WORKMATE_TEST_REAL_SANDBOX') != '1':
        from workmate_runtime.workspaces import WorkmateClearedEnvBubblewrapBackend
        # Host lifecycle tests do not assert OS isolation; the container suite does.
        async def fixture_write(backend,path,data):
            target=Path(backend._host_workdir)/Path(path).relative_to('/workspace')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data)
        monkeypatch.setattr(WorkmateClearedEnvBubblewrapBackend,'write_file',fixture_write)
    settings=RuntimeSettings(reme_enabled=False)
    # Use the actual display Writer and PG snapshots. Transport itself is an
    # in-memory fixture here; the Streams variant injects a real Redis log.
    class DisplayLog(MemoryLog):
        def __init__(self):
            super().__init__(sessions, assert_idle=False)
            self.events = []
        async def append(self, ref, envelope):
            self.events.append(envelope)
            return SimpleNamespace(cursor=RealtimeCursor(conversation_id=ref.conversation_id,
                generation=ref.generation, stream_id=f'{len(self.events)}-0'))
    display_log = DisplayLog()
    workers=[compose_worker(sessions.kw['bind'],settings,storage_node_id='node',
        workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace,drain_seconds=0,
        realtime_log=display_log) for _ in range(2)]
    product=compose_product_runtime(sessions,poll_interval=.01)
    stops=[asyncio.Event() for _ in range(3)]
    running=[asyncio.create_task(product.serve(stops[0])),asyncio.create_task(workers[0].serve(stops[1]))]
    errors=[]
    for worker in workers:
        original=worker.service.controller._supervise
        async def observed(*args,_original=original,**kwargs):
            try:
                return await _original(*args,**kwargs)
            except Exception as error:
                import traceback
                errors.append(''.join(traceback.format_exception(error)))
                import logging
                logging.getLogger(__name__).error('Composed controller failure', exc_info=True)
                raise
        monkeypatch.setattr(worker.service.controller,'_supervise',observed)
    try:
        async def suspended():
            assert not workers[0].service.failures,(workers[0].service.failures,errors)
            assert not product.failures,product.failures
            return await admin.fetchval("SELECT count(*) FROM runtime_suspensions WHERE status='released'") == 1
        await wait_for(suspended)
        baseline=await admin.fetchrow('SELECT * FROM runtime_delivery_baselines')
        execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
        original_reply=execution['reply_id']
        keep=next((tmp_path/'workspaces').glob('*/working'))/'keep.txt'
        keep.write_text('working state survives')
        stops[1].set()
        await running[1]
        assert not workers[0].workspaces._ownership
        assert await admin.fetchval('SELECT owner_id FROM runtime_sessions') is None
        state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
        pending=state['pending_interactions'][0]
        async def allow(*_): return True
        if action=='revoked_stop':
            await admin.execute('UPDATE scopes SET owner_user_id=NULL')
            command=Command(command_id='stop',kind='StopTask',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=original_reply,expected_epoch=0,payload={})
            async with sessions.begin() as db:
                await accept_stop(db,command,actor_user_id='u')
        else:
            command=Command(command_id='resume',kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=original_reply,expected_epoch=execution['execution_epoch'],
                interaction_id=pending['interaction_id'],request_version=pending['request_version'],
                payload={'decision_version':'cold-decision','snapshot_id':execution['snapshot_id'],
                    'state_version':execution['state_version'],'full_access':action=='full_access',
                    'decisions':[{'tool_call_id':'write','approved':True}]})
            async with sessions.begin() as db:
                await (accept_full_access if action=='full_access' else accept_resume)(db,command,
                    actor_user_id='u',authorize_resume=allow)
        running.append(asyncio.create_task(workers[1].serve(stops[2])))
        if action=='partial_resume':
            async def suspended_again():
                assert not workers[1].service.failures,(workers[1].service.failures,errors)
                assert not product.failures,product.failures
                return await admin.fetchval("SELECT count(*) FROM runtime_suspensions WHERE status='released'") == 2
            await wait_for(suspended_again)
            next_execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
            next_state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
            next_pending=next_state['pending_interactions'][0]
            next_command=Command(command_id='resume2',kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=original_reply,expected_epoch=next_execution['execution_epoch'],
                interaction_id=next_pending['interaction_id'],request_version=next_pending['request_version'],
                payload={'decision_version':'second-cold-decision','snapshot_id':next_execution['snapshot_id'],
                    'state_version':next_execution['state_version'],'decisions':[{'tool_call_id':'write2','approved':True}]})
            async with sessions.begin() as db:
                await accept_resume(db,next_command,actor_user_id='u',authorize_resume=allow)
        async def finished():
            assert not workers[1].service.failures,(workers[1].service.failures,errors)
            assert not product.failures,product.failures
            return await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
        await wait_for(finished)
        assert dict(await admin.fetchrow('SELECT * FROM runtime_delivery_baselines')) == dict(baseline)
        assert await admin.fetchval('SELECT execution_epoch FROM runtime_task_executions') == (3 if action=='partial_resume' else 2)
        assert await admin.fetchval('SELECT reply_id FROM runtime_task_executions') == original_reply
        assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures') == 1
        assert keep.read_text()=='working state survives'
        if action=='revoked_stop':
            assert await admin.fetchval('SELECT status FROM tasks')=='cancelled'
            assert len(builds)==1
        else:
            assert await admin.fetchval('SELECT status FROM tasks')=='completed'
            assert len(list((tmp_path/'workspaces').rglob('result.txt')))==1
            assert await admin.fetchval('SELECT count(*) FROM runtime_sandbox_attestations')==(3 if action=='partial_resume' else 2)
            assert await admin.fetchval('SELECT count(*) FROM artifacts')==(2 if action=='partial_resume' else 1)
    finally:
        for stop in stops: stop.set()
        await asyncio.wait_for(asyncio.gather(*running),15)
        for worker in workers: await worker.workspaces.close()
