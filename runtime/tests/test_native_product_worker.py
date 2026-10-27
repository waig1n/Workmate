"""Real HTTP admission → native Worker → Product projection and durable replay.

Model and OS sandbox are fixtures; PostgreSQL, Redis and both role compositions are real.
No project deployment or external model is contacted.
"""
from test_realtime_redis_log import log  # noqa:F401
import asyncio
from types import SimpleNamespace
from pathlib import Path
import pytest
from httpx import AsyncClient,ASGITransport
from app.auth import get_current_user,get_current_user_id,get_access_decision
from app.db import get_db
from workmate_product.persistence.models import User
import json
from app.services.access_decision import AccessDecisionService
from test_native_admission import admission_database  # noqa:F401
from test_durable_projection import projection_database  # noqa:F401
from test_native_agents import native_agents  # noqa:F401


@pytest.mark.parametrize('initial_security', ['standard', 'full_access', 'rejected'])
async def test_http_product_lifespan_runs_native_worker_to_delivery(admission_database,native_agents,tmp_path,monkeypatch,initial_security,log):
    from agentscope.message import TextBlock, ThinkingBlock
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.execution.composition import compose_worker
    from test_worker_workspaces import StubWorkspace
    from test_core_engine import ScriptModel
    import app.main as entry
    sessions,admin=admission_database
    # Existing fixture-only parked conversation is outside the submitted chain.
    await admin.execute("DELETE FROM runtime_task_executions WHERE task_id='t'; DELETE FROM runtime_sessions WHERE id='s'; UPDATE conversations SET active_product_task_id=NULL WHERE id='c'; UPDATE tasks SET status='cancelled' WHERE id='t'")
    engine=sessions.kw['bind']
    settings=SimpleNamespace(runtime_base_url='http://worker',runtime_internal_token='fixture-token',runtime_timeout_secs=10,
        workspace_assistant_enabled=False,worker_storage_node_id='node',github_mcp_hub_enabled=False,
        worker_nodes=[],coordination_redis_url=__import__('os').environ['WORKMATE_TEST_REDIS_URL'],
        coordination_namespace=log.namespace,
        artifact_storage_dir=str(tmp_path/'artifacts'),
        skillhub_cn_enabled=False,skillhub_cn_base_url='https://api.skillhub.cn',
        skillhub_cn_request_timeout_secs=30.0,skillhub_cn_max_retries=2,
        task_max_outstanding=64,task_max_outstanding_per_user=8,
        task_max_pending_approval=256,task_max_pending_approval_per_user=32,approval_timeout_secs=86400)
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.config.command_queue import CommandQueuePolicy
    settings.realtime = RealtimePolicy(snapshot_interval_seconds=.1)
    settings.command_queue = CommandQueuePolicy(poll_interval_seconds=.05)
    monkeypatch.setattr(entry,'get_settings',lambda:settings)
    monkeypatch.setattr('app.api.routers.artifacts.get_settings', lambda: settings)
    monkeypatch.setattr(entry,'get_sessionmaker',lambda:sessions)
    monkeypatch.setattr(entry,'_warn_insecure_settings',lambda:None)
    async def noop(*args): pass
    monkeypatch.setattr(entry,'ensure_bootstrap',noop)
    monkeypatch.setattr(entry,'dispose_engine',noop)
    class NoChat:
        def __init__(self,*args,**kwargs): pass
        def __getattr__(self,name): raise AssertionError('Old runtime accessed: '+name)
    monkeypatch.setattr(entry,'KnowledgeServiceClient',NoChat)
    from agentscope.model import ChatResponse
    model_calls = []
    body_entered, body_release = asyncio.Event(), asyncio.Event()
    class StreamingModel(ScriptModel):
        async def _call_api(self,*args,**kwargs):
            model_calls.append('called')
            output = Path(StubWorkspace.made[-1].host_workdir) / 'output' / 'report.md'
            output.write_text('report version ' + str(len(selected_profiles)))
            async def chunks():
                content=[ThinkingBlock(thinking='native reasoning'), TextBlock(text='native HTTP reply')]
                yield ChatResponse(content=content,is_last=False)
                body_entered.set()
                await body_release.wait()
                from agentscope.model import ChatUsage
                yield ChatResponse(content=content,is_last=True,usage=ChatUsage(input_tokens=1000,output_tokens=500,time=.1))
            return chunks()
    selected_profiles = []
    def model_factory(settings, selected):
        selected_profiles.append(selected.profile_name)
        model = StreamingModel([])
        model.model = selected.model
        return model
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model', model_factory)
    from app.config_file import ModelsFileConfig
    profiles = ModelsFileConfig.model_validate({'default':'test', 'profiles':{
        name:{'type':'openai', 'model':name + '-fixture', 'api_key_env':'FIXTURE_MODEL_KEY'}
        for name in ('test', 'second')}})
    monkeypatch.setattr('app.services.execution_admission.models_file_config', lambda: profiles)
    monkeypatch.setattr('app.services.usage.model_profiles', lambda: profiles.profiles)
    from app.config_file import UsageFileConfig
    prices = UsageFileConfig(currency='USD', prices={name:{'input_per_k':'1','output_per_k':'2'} for name in ('test','second')})
    monkeypatch.setattr('app.services.usage.load_file_config', lambda: SimpleNamespace(product=SimpleNamespace(usage=prices)))
    monkeypatch.setattr('app.api.routers.conversations.model_profiles', lambda: profiles.profiles)
    from workmate_runtime.assembly.sandbox_proof import attest_task_sandbox
    import os
    async def sandbox_proof(binding,owner,sessions):
        original = binding.backend.exec_shell
        async def probe(*args,**kwargs):
            namespace = os.readlink('/proc/self/ns/pid') if initial_security == 'rejected' else 'pid:[1]'
            return SimpleNamespace(exit_code=0,stdout=namespace.encode())
        binding.backend.exec_shell = probe
        try:
            await attest_task_sandbox(binding,owner,sessions)
        finally:
            binding.backend.exec_shell = original
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',sandbox_proof)
    web=entry.build_app()
    async def db_dependency():
        async with sessions() as db: yield db
    async def user_dependency():
        async with sessions() as db: return await db.get(User,'u')
    async def access_dependency():
        async with sessions() as db: yield AccessDecisionService(db)
    web.dependency_overrides.update({get_db:db_dependency,get_current_user:user_dependency,
        get_current_user_id:lambda:'u',get_access_decision:access_dependency})
    # Both roles must read the same object store; each owns its own DB sessions.
    monkeypatch.setattr('workmate_product.skill_storage.store_from_settings',
        lambda settings: sessions.kw['info']['skill_blob_store'])
    from workmate_product.coordination.command_queue import RedisCommandQueue
    from workmate_product.realtime.composition import create_realtime_log
    queue = RedisCommandQueue(log.client, namespace=log.namespace)
    realtime_log = create_realtime_log(log.client, namespace=log.namespace, policy=settings.realtime)
    workers = [compose_worker(engine,RuntimeSettings(reme_enabled=False,realtime=settings.realtime),storage_node_id='node',
        workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace,drain_seconds=0,
        command_queue=queue,realtime_log=realtime_log) for _ in range(2)]
    worker = workers[0]
    # Run the real immutable package installer under a controlled local backend.
    # This verifies bytes/materialization, not OS sandbox isolation.
    from workmate_runtime.assembly.skills import _MATERIALIZE_SCRIPT
    import sys
    destinations = []
    def skill_backend(constructor, *args):
        backend = constructor(*args)
        async def execute(command, raw):
            assert command[2:4] == ['-c', _MATERIALIZE_SCRIPT]
            destination = tmp_path / 'materialized' / command[5].removeprefix('/workspace/')
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', command[3],
                command[4], str(destination), command[6], stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, error = await process.communicate(raw)
            assert process.returncode == 0, error
            destinations.append(destination)
            return SimpleNamespace(exit_code=process.returncode)
        backend._exec_with_input = execute
        return backend
    from functools import partial
    for instance in workers:
        monkeypatch.setattr(instance.workspaces, '_task_backend', partial(skill_backend, instance.workspaces._task_backend))
    shutdown=asyncio.Event()
    running=[]
    async def replay(cid):
        from app.services.conversation_business_state import ConversationBusinessState
        handle = await web.state.product_stream.open(cid, authority=ConversationBusinessState(sessions, 'u', token_version=0))
        frames=[]
        try:
            async for frame in handle.frames():
                frames.append(frame)
                if 'REPLAY_DONE' in frame: break
        finally:
            await handle.aclose()
        return frames
    try:
        async with web.router.lifespan_context(web):
            running=[asyncio.create_task(instance.serve(shutdown)) for instance in workers]
            await asyncio.gather(*(instance.service.ready.wait() for instance in workers))
            async with AsyncClient(transport=ASGITransport(app=web),base_url='http://product') as client:
                installed = await client.get('/api/skills', params={'source':'builtin', 'q':'agent-bootstrap'})
                assert installed.status_code == 200, installed.text
                skill = next(item for item in installed.json()['items'] if item['name'] == 'agent-bootstrap')
                share_url = '/api/sharing/skills/' + skill['id']
                preview = await client.post(share_url + '/preview', json={'target_scope_id':'scope'})
                assert preview.status_code == 200, preview.text
                shared = await client.post(share_url, json={'target_scope_id':'scope',
                    'confirmation':preview.json()['confirmation']})
                assert shared.status_code == 200, shared.text
                created = await client.post('/api/agents', json={
                    'scope_id':'scope', 'slug':'worker-agent', 'display_name':'Worker Agent',
                    'skill_binding_ids':[skill['id']]})
                assert created.status_code == 201, created.text
                conversation = await client.post('/api/conversations', json={'agent_id':created.json()['id']})
                assert conversation.status_code == 201, conversation.text
                cid = conversation.json()['id']
                response=await client.post(f'/api/tasks/conversations/{cid}/tasks',json={'message':'hello', 'approval_profile': 'standard' if initial_security == 'standard' else 'full_access'})
                assert response.status_code==202,response.text
                tid=response.json()['task']['id']
                if initial_security != 'rejected':
                    async with asyncio.timeout(20):
                        await body_entered.wait()
                        while True:
                            partial = await admin.fetchval("SELECT content FROM task_messages WHERE task_id=$1 AND snapshot_phase='partial'",tid)
                            if partial and 'native HTTP reply' in str(partial) and 'native reasoning' in str(partial): break
                            await asyncio.sleep(.02)
                    # No browser handshake has initialized this first Task's log.
                    assert await admin.fetchval('SELECT count(*) FROM conversation_event_logs WHERE conversation_id=$1',cid) == 1
                    # The lifespan BusinessPublisher, not an SSE subscriber, publishes RUN_STARTED.
                    async with asyncio.timeout(10):
                        while not await admin.fetchval("SELECT count(*) FROM realtime_business_outbox o JOIN product_events e ON e.id=o.event_id WHERE o.conversation_id=$1 AND o.status='published' AND e.payload->>'type'='RUN_STARTED'",cid):
                            await asyncio.sleep(.02)
                    from workmate_contracts.realtime import RealtimeCursor
                    ref = await web.state.product_stream.manager.ensure(cid)
                    page = await realtime_log.read(ref, RealtimeCursor(conversation_id=cid, generation=ref.generation, stream_id='0-0'))
                    assert any(e.envelope.payload.get('type') == 'RUN_STARTED' for e in page.entries)
                    assert {'TEXT_MESSAGE_CONTENT','REASONING_MESSAGE_CONTENT'} <= {e.envelope.payload['type'] for e in page.entries if e.envelope.text}
                body_release.set()
                async with asyncio.timeout(20):
                    while await admin.fetchval('SELECT active_product_task_id FROM conversations WHERE id=$1', cid) is not None:
                        assert not any(instance.service.failures for instance in workers), [instance.service.failures for instance in workers]
                        assert not web.state.product_projection.failures,web.state.product_projection.failures
                        await asyncio.sleep(.02)
                task=await client.get('/api/tasks/'+tid)
                if initial_security == 'rejected':
                    assert task.json()['status'] == 'failed', task.text
                    assert task.json()['run_id'] is None
                    assert not model_calls and not destinations
                    assert await admin.fetchval('SELECT count(*) FROM runtime_sandbox_attestations WHERE task_id=$1',tid) == 0
                    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'") == 0
                    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions WHERE conversation_id=$1',cid) is None
                    frames=await replay(cid)
                    assert 'sandbox_verification_failed' in ''.join(frames)
                    assert '"status":"failed"' in ''.join(frames)
                    # The proven empty delivery permits another Task in this Session.
                    initial_security = 'standard'
                    retried = await client.post(f'/api/tasks/conversations/{cid}/tasks', json={'message':'retry'})
                    assert retried.status_code == 202, retried.text
                    async with asyncio.timeout(20):
                        while await admin.fetchval('SELECT active_product_task_id FROM conversations WHERE id=$1',cid) is not None:
                            assert not any(instance.service.failures for instance in workers), [instance.service.failures for instance in workers]
                            assert not web.state.product_projection.failures,web.state.product_projection.failures
                            await asyncio.sleep(.02)
                    assert (await client.get('/api/tasks/' + retried.json()['task']['id'])).json()['status'] == 'completed'
                    return
                assert task.json()['status']=='completed'
                assert await admin.fetchval('SELECT snapshot_phase FROM task_messages WHERE task_id=$1 AND snapshot_version>0',tid) == 'final'
                usage = json.loads(await admin.fetchval('SELECT token_usage FROM tasks WHERE id=$1',tid))
                assert usage['input_tokens'] == 1000 and usage['output_tokens'] == 500
                assert await admin.fetchval('SELECT usage_cost FROM tasks WHERE id=$1',tid) == 2
                enabled = await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'")
                assert enabled == int(initial_security == 'full_access')
                artifacts = await client.get('/api/artifacts', params={'task_id': tid})
                assert artifacts.status_code == 200, artifacts.text
                assert len(artifacts.json()) == 1
                artifact_id = artifacts.json()[0]['id']
                downloaded = await client.get(f'/api/artifacts/{artifact_id}/download')
                assert downloaded.status_code == 200, downloaded.text
                assert downloaded.text == 'report version 1'
                assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id=$1 AND kind='StartTask'",tid)==1
                assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions WHERE conversation_id=$1', cid) is None
                frames=await replay(cid)
                assert 'native HTTP reply' in ''.join(frames),frames
                assert 'native reasoning' in ''.join(frames)
                runtime_types = await admin.fetch("SELECT payload->>'type' AS type FROM runtime_events WHERE task_id=$1",tid)
                assert not any(row['type'].startswith(('TEXT_BLOCK_', 'THINKING_BLOCK_')) for row in runtime_types)
                product_types = await admin.fetch("SELECT payload->>'type' AS type FROM product_events WHERE conversation_id=$1",cid)
                assert not any(row['type'].startswith(('TEXT_MESSAGE_', 'REASONING_MESSAGE_')) for row in product_types)
                assert not hasattr(web.state,'run_registry')
                assert len(destinations) == 1
                assert 'agent-bootstrap' in (destinations[0] / 'SKILL.md').read_text()
                first_spec = await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1', tid)
                switched = await client.patch(f'/api/conversations/{cid}/model', json={'model_profile':'second'})
                assert switched.status_code == 200, switched.text
                next_task = await client.post(f'/api/tasks/conversations/{cid}/tasks', json={'message':'continue'})
                assert next_task.status_code == 202, next_task.text
                next_id = next_task.json()['task']['id']
                async with asyncio.timeout(20):
                    while await admin.fetchval('SELECT active_product_task_id FROM conversations WHERE id=$1', cid) is not None:
                        assert not worker.service.failures, worker.service.failures
                        assert not web.state.product_projection.failures, web.state.product_projection.failures
                        await asyncio.sleep(.02)
                assert (await client.get('/api/tasks/' + next_id)).json()['status'] == 'completed'
                assert selected_profiles == ['test', 'second']
                # A later Workspace rewrite cannot change the first delivered version.
                previous = await client.get(f'/api/artifacts/{artifact_id}/download')
                assert previous.status_code == 200 and previous.text == 'report version 1'
                latest = (await client.get('/api/artifacts', params={'task_id': next_id})).json()
                assert len(latest) == 1
                assert (await client.get(f"/api/artifacts/{latest[0]['id']}/download")).text == 'report version 2'
                assert await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1', tid) == first_spec
                retired = await client.delete('/api/conversations/' + cid)
                assert retired.status_code == 204, retired.text
                async with asyncio.timeout(10):
                    while await admin.fetchval('SELECT completed_at FROM conversation_retirements WHERE conversation_id=$1', cid) is None:
                        # Another replica may temporarily lack cleanup ownership;
                        # the durable completion identifies the successful owner.
                        await asyncio.sleep(.02)
                assert await admin.fetchval('SELECT completed_by FROM conversation_retirements WHERE conversation_id=$1', cid) in {instance.service.worker_id for instance in workers}
                assert (await client.get('/api/conversations/' + cid)).status_code == 404
                assert (await client.get('/api/tasks/' + tid)).status_code == 404
                assert (await client.get(f'/api/artifacts/{artifact_id}/download')).status_code == 404
                assert await admin.fetchval('SELECT payload FROM runtime_execution_specs WHERE task_id=$1', tid) == first_spec
                assert all(destination.exists() for destination in destinations)
    finally:
        shutdown.set()
        body_release.set()
        if running: await asyncio.wait_for(asyncio.gather(*running),10)
        else: await asyncio.gather(*(instance.workspaces.close() for instance in workers))
