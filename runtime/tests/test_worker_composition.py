"""Durable scheduler → real Product facts → Core → immutable delivery capture."""
from polling_worker import polling_composition
import asyncio
import json
import pytest
from agentscope.message import TextBlock
from test_execution_context import database,record
from test_worker_workspaces import StubWorkspace
from test_core_engine import ScriptModel
from workmate_runtime.config import RuntimeSettings


async def test_composed_worker_completes_native_reply_and_capture(database,tmp_path,monkeypatch,polling_composition):
    from workmate_runtime.execution.composition import compose_worker
    sessions,admin=database
    await record(sessions)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',
        lambda *_:ScriptModel([[TextBlock(text='native reply')]]))
    async def no_attestation(*_):
        return None  # No tools or FullAccess in this fixture.
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',no_attestation)
    app=compose_worker(sessions.kw['bind'],RuntimeSettings(reme_enabled=False),
        storage_node_id='node',workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace,drain_seconds=0)
    shutdown=asyncio.Event()
    running=asyncio.create_task(app.serve(shutdown))
    try:
        async with asyncio.timeout(15):
            while not await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures'):
                assert not app.service.failures,app.service.failures
                await asyncio.sleep(.02)
        assert await admin.fetchval('SELECT status FROM runtime_sessions')=='completed'
        snapshot=json.loads(await admin.fetchval('SELECT payload FROM runtime_snapshots'))
        assert 'native reply' in json.dumps(snapshot)
        assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='cmd'")=='completed'
        assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
        assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    finally:
        shutdown.set()
        await asyncio.wait_for(running,10)
    assert not app.service.failures


async def test_composed_resume_authorization_joins_locked_transport_transaction(database,tmp_path,polling_composition):
    from test_execution_context import claim
    from workmate_runtime.execution.composition import compose_worker
    sessions,admin=database
    await record(sessions)
    await claim(admin)
    app=compose_worker(sessions.kw['bind'],RuntimeSettings(reme_enabled=False),
        storage_node_id='node',workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace)
    try:
        async with asyncio.timeout(5):
            async with app.service.pool.acquire() as db:
                async with db.transaction():
                    await db.fetchrow("SELECT * FROM runtime_sessions WHERE id='s' FOR UPDATE")
                    authorize=app.service.bindings.authorize_resume
                    assert await authorize(db,'u','a','t') is True
                    assert await authorize(db,'other','a','t') is False
                    await db.execute("UPDATE users SET status='disabled' WHERE id='u'")
                    assert await authorize(db,'u','a','t') is False
                    assert await db.fetchval("SELECT status FROM users WHERE id='u'")=='disabled'
        assert await admin.fetchval("SELECT status FROM users WHERE id='u'")=='disabled'
    finally:
        await app.workspaces.close()


@pytest.mark.parametrize('action',['resume','revoked_resume','revoked_stop'])
async def test_composed_permission_control_and_product_background_delivery(database,tmp_path,monkeypatch,action,polling_composition):
    """Actual scheduler, assembler, Core, joined auth pool and Product consumer."""
    from agentscope.message import ToolCallBlock
    from sqlalchemy import text
    from workmate_contracts import Command
    from workmate_product.application.control_commands import accept_resume,accept_stop
    from workmate_product.application.authorization import AccessDecisionService
    from workmate_runtime.execution.composition import compose_worker
    from app.services.durable_runtime import compose_product_runtime
    sessions,admin=database
    await admin.execute("UPDATE tasks SET approval_profile='strict'")
    await record(sessions)
    builds=[]
    def model(*_):
        responses=([[ToolCallBlock(id='write',name='Write',input=json.dumps({
            'file_path':'/workspace/output/result.txt','content':'approved result'}))]]
            if not builds else [[TextBlock(text='Saved approved result')]])
        builds.append(responses)
        return ScriptModel(responses)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',model)
    async def no_attestation(*_):
        return None  # Strict execution; this test cannot grant Full Access.
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',no_attestation)
    worker=compose_worker(sessions.kw['bind'],RuntimeSettings(reme_enabled=False),
        storage_node_id='node',workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace,drain_seconds=0)
    errors=[]
    supervise=worker.service.controller._supervise
    async def observed(*args,**kwargs):
        try:
            return await supervise(*args,**kwargs)
        except Exception as error:
            import traceback
            errors.append(''.join(traceback.format_exception(error)))
            import logging
            logging.getLogger(__name__).error('Composed controller failure', exc_info=True)
            raise
    monkeypatch.setattr(worker.service.controller,'_supervise',observed)
    product=compose_product_runtime(sessions,poll_interval=.01)
    shutdown=asyncio.Event()
    tasks=[asyncio.create_task(worker.serve(shutdown)),asyncio.create_task(product.serve(shutdown))]
    try:
        async with asyncio.timeout(15):
            while await admin.fetchval('SELECT status FROM tasks')!='awaiting_permission':
                assert not worker.service.failures,(worker.service.failures,errors)
                assert not product.failures,product.failures
                await asyncio.sleep(.01)
        execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
        state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
        pending=state['pending_interactions'][0]
        original_reply=execution['reply_id']
        assert not list((tmp_path/'workspaces').rglob('result.txt'))
        async def allowed(db,actor,agent):
            return await AccessDecisionService(db).can_use_agent(actor,agent)
        if action=='revoked_stop':
            await admin.execute('UPDATE scopes SET owner_user_id=NULL')
            command=Command(command_id='stop',kind='StopTask',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=original_reply,expected_epoch=execution['execution_epoch'],payload={})
            async with sessions.begin() as db:
                await accept_stop(db,command,actor_user_id='u')
        else:
            command=Command(command_id='resume',kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=original_reply,expected_epoch=execution['execution_epoch'],
                interaction_id=pending['interaction_id'],request_version=pending['request_version'],
                payload={'decision_version':'approve-write','snapshot_id':execution['snapshot_id'],
                    'state_version':execution['state_version'],'decisions':[{'tool_call_id':'write','approved':True}]})
            async with sessions.begin() as db:
                await accept_resume(db,command,actor_user_id='u',authorize_resume=allowed)
                if action=='revoked_resume':
                    # Worker can observe the accepted command only together with
                    # the committed revocation, eliminating a test scheduling race.
                    await db.execute(text('UPDATE scopes SET owner_user_id=NULL'))
        async with asyncio.timeout(15):
            while True:
                if action=='revoked_resume':
                    if worker.service.failures:
                        break
                elif await admin.fetchval('SELECT active_product_task_id FROM conversations') is None:
                    break
                assert not product.failures,product.failures
                if action!='revoked_resume':
                    assert not worker.service.failures,(worker.service.failures,errors)
                await asyncio.sleep(.01)
        assert await admin.fetchval('SELECT reply_id FROM runtime_task_executions')==original_reply
        if action=='revoked_resume':
            assert worker.service.failures=={'s':'ReconciliationRequired'}
            assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='accepted'
            assert await admin.fetchval('SELECT status FROM runtime_sessions')=='awaiting_interaction'
            assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
            assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
            assert not list((tmp_path/'workspaces').rglob('result.txt'))
            assert len(builds)==1  # Rejection happened before model/tool preparation.
        else:
            assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None
            assert await admin.fetchval('SELECT status FROM tasks')==('completed' if action=='resume' else 'cancelled')
            assert await admin.fetchval('SELECT count(*) FROM runtime_delivery_captures')==1
            assert await admin.fetchval('SELECT count(*) FROM artifacts')==(1 if action=='resume' else 0)
            assert await admin.fetchval('SELECT status FROM runtime_commands WHERE id=$1',command.command_id)=='completed'
            if action=='resume':
                files=list((tmp_path/'workspaces').rglob('result.txt'))
                assert len(files)==1 and files[0].read_text()=='approved result'
            else:
                assert not list((tmp_path/'workspaces').rglob('result.txt'))
    finally:
        shutdown.set()
        await asyncio.wait_for(asyncio.gather(*tasks),10)


@pytest.mark.parametrize('failed', [False, True])
async def test_finalize_releases_checkpoint_reference_only_after_success(database, tmp_path, polling_composition, failed):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from workmate_runtime.execution.composition import compose_worker
    sessions, _ = database
    app = compose_worker(sessions.kw['bind'], RuntimeSettings(reme_enabled=False), storage_node_id='node',
        workspace_dir=tmp_path/'workspace', artifact_dir=tmp_path/'artifacts', max_file_bytes=100,
        max_files=3, workspace_factory=StubWorkspace)
    owner = 'fixture-owner'
    result = SimpleNamespace(owner=owner)
    app.delivery._bindings[owner] = object()
    app.delivery._checkpoint_workspaces[owner] = object()
    app.delivery.capture = AsyncMock(side_effect=RuntimeError('delivery incomplete') if failed else None)
    app.workspaces.release_task = AsyncMock()
    if failed:
        with pytest.raises(RuntimeError, match='delivery incomplete'):
            await app.service.bindings.finalize_result(result)
        assert owner in app.delivery._checkpoint_workspaces
        assert owner in app.delivery._bindings
        app.workspaces.release_task.assert_not_awaited()
    else:
        await app.service.bindings.finalize_result(result)
        assert owner not in app.delivery._checkpoint_workspaces
        assert owner not in app.delivery._bindings
