"""Opt-in HTTP admission and real Core cold approval on actual Docker/Kubernetes.

Controlled model only. Sandbox identity, resource cleanup, persistence, queues,
execution authority, projection and Gateway run through production paths.
"""
import asyncio
from functools import wraps
import json
import os
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from agentscope.message import TextBlock, ToolCallBlock
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from test_durable_projection import projection_database  # noqa: F401
from test_native_admission import admission_database  # noqa: F401
from test_native_agents import native_agents  # noqa: F401
from test_dual_gateway_http import Gateway
from test_core_engine import ScriptModel
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from workmate_runtime.sandbox.remote import RemoteSandboxProvider
from workmate_product.config.sandbox import parse_sandbox_settings, sandbox_environment
from workmate_product.coordination.command_queue import RedisCommandQueue
from workmate_product.realtime.composition import create_realtime_log


@pytest.mark.parametrize(('provider_name', 'decision'), [
    (provider, decision) for provider in ('docker', 'kubernetes')
    for decision in ('approve', 'deny', 'stop')] + [('docker', 'storage_lost')])
async def test_real_remote_approval_release_and_cold_control(
        admission_database, native_agents, tmp_path, monkeypatch, provider_name, decision):
    if not os.getenv('WORKMATE_TEST_REDIS_URL'):
        pytest.skip('requires isolated Redis')
    if provider_name == 'docker' and not os.getenv('WORKMATE_TEST_SANDBOX_IMAGE'):
        pytest.skip('requires existing Docker sandbox image')
    if provider_name == 'kubernetes' and not all(os.getenv(k) for k in ('WORKMATE_TEST_K8S_CONFIG', 'WORKMATE_TEST_K8S_IMAGE')):
        pytest.skip('requires explicit disposable Kubernetes cluster and preloaded image')
    sessions, admin = admission_database
    await admin.execute("DELETE FROM runtime_task_executions WHERE task_id='t'; "
        "DELETE FROM runtime_sessions WHERE id='s'; "
        "UPDATE conversations SET active_product_task_id=NULL WHERE id='c'; "
        "UPDATE tasks SET status='cancelled' WHERE id='t'")
    schema = await admin.fetchval('SELECT current_schema()')
    namespace = 'remote-approval-' + uuid4().hex
    k8s_namespace = 'workmate-approval-' + uuid4().hex[:20]
    resources, source_ids, workers, running, shutdowns = [], [], [], [], []
    k8s_client = k8s_api = k8s_uid = None
    redis = Redis.from_url(os.environ['WORKMATE_TEST_REDIS_URL'], decode_responses=True,
        retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
    config = tmp_path / 'gateway.yaml'
    config.write_text('product:\n  workspace_assistant:\n    enabled: false\n'
        '  models:\n    default: test\n    profiles:\n      test:\n        type: openai\n'
        '        model: fixture-model\n        api_key_env: FIXTURE_MODEL_KEY\n'
        'command_queue:\n  poll_interval_seconds: 0.05\n')
    monkeypatch.setenv('FIXTURE_MODEL_KEY', 'local-fixture-only')
    responses = iter([[ToolCallBlock(id='remote-write', name='Write', input=json.dumps({
        'file_path': '/workspace/output/result.txt', 'content': 'approved exactly once'}))],
        [TextBlock(text='Decision handled')]])
    class Model(ScriptModel):
        async def _call_api(self, *args, **kwargs):
            self.responses = iter([next(responses)])
            return await super()._call_api(*args, **kwargs)
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model', lambda *_: Model([]))
    from workmate_runtime.workspaces import WorkmateSandboxWrite
    original_write = WorkmateSandboxWrite.call
    writes = []
    write_identities = []
    @wraps(original_write)
    async def observed_write(self, **kwargs):
        writes.append(kwargs)
        state = kwargs['_agent_state']
        write_identities.append((state.session_id, state.reply_id))
        return await original_write(self, **kwargs)
    monkeypatch.setattr(WorkmateSandboxWrite, 'call', observed_write)
    if provider_name == 'docker':
        from agentscope.workspace import DockerWorkspace
        async def local_image(workspace):
            workspace._image_tag = os.environ['WORKMATE_TEST_SANDBOX_IMAGE']
        monkeypatch.setattr(DockerWorkspace, '_build_or_reuse_image', local_image)
        policy = parse_sandbox_settings({'provider': 'docker'})
    else:
        monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER', 'kubernetes')
        monkeypatch.setenv('WORKMATE_SANDBOX_KUBECONFIG', os.path.abspath(os.environ['WORKMATE_TEST_K8S_CONFIG']))
        policy = sandbox_environment(parse_sandbox_settings(dict(provider='kubernetes',
            namespace=k8s_namespace, image=os.environ['WORKMATE_TEST_K8S_IMAGE'], storage_size='128Mi')))
    settings = RuntimeSettings(reme_enabled=False, sandbox=policy,
        coordination_redis_url=os.environ['WORKMATE_TEST_REDIS_URL'], coordination_namespace=namespace)
    gateway = Gateway(tmp_path, schema, namespace, config, 0)

    def provider():
        result = RemoteSandboxProvider(policy)
        original = result.initialize_workspace
        async def initialize(workspace):
            if provider_name == 'docker' and not resources:
                import aiodocker
                async with aiodocker.Docker() as docker:
                    volume = await docker.volumes.create({'Name':'workmate-'+workspace.workspace_id,
                        'Labels':{'workmate.test.owner':namespace}})
                    assert (await volume.show())['Labels']['workmate.test.owner'] == namespace
            elif provider_name == 'kubernetes':
                workspace._image_pull_policy = 'Never'
            resources.append(workspace)
            try:
                await original(workspace)
            except BaseException:
                import logging
                logging.getLogger(__name__).exception('Real provider initialization failed')
                raise
            source_ids.append(workspace._workmate_instance_id)
            # Seed once as actual Task UID before any Core reply; the continuation
            # must reuse these exact persisted bytes after the old handle closes.
            seed = await workspace._workmate_control.exec_shell([workspace._workmate_control.python, '-I', '-c',
                'import os,pathlib,sys;os.setgid(62000);os.setuid(62000);root=pathlib.Path(sys.argv[1]);'
                'files={"input/original.txt":b"original-input","working/keep.bin":bytes(range(256)), '
                '"sessions/fixture-offload.bin":b"session-offload-bytes","data/fixture-offload.bin":b"tool-offload-bytes"};'
                '[(root/name).write_bytes(content) for name,content in files.items()] if sys.argv[2]=="seed" else None;'
                'assert all((root/name).read_bytes()==content for name,content in files.items())',
                workspace.workdir, 'seed' if len(resources)==1 else 'verify'])
            assert seed.exit_code == 0
        result.initialize_workspace = initialize
        return result

    def start_worker():
        worker = compose_worker(sessions.kw['bind'], settings, storage_node_id='node',
            workspace_dir=tmp_path/'workspaces', artifact_dir=tmp_path/'artifacts',
            max_file_bytes=1024, max_files=3, sandbox_provider=provider(), drain_seconds=0,
            command_queue=RedisCommandQueue(redis, namespace=namespace),
            realtime_log=create_realtime_log(redis, namespace=namespace, policy=settings.realtime))
        shutdown = asyncio.Event()
        workers.append(worker); shutdowns.append(shutdown)
        running.append(asyncio.create_task(worker.serve(shutdown)))
        return worker

    async def wait_value(sql, task_id, expected, timeout=120):
        async with asyncio.timeout(timeout):
            while True:
                assert not workers[-1].service.failures, workers[-1].service.failures
                assert not running[-1].done(), 'Worker exited unexpectedly'
                value = await admin.fetchval(sql, task_id)
                if value == expected:
                    return
                await asyncio.sleep(.1)

    try:
        if provider_name == 'kubernetes':
            from kubernetes_asyncio import client, config as kube_config
            k8s_client = await kube_config.new_client_from_config(config_file=policy.kubeconfig)
            k8s_api = client.CoreV1Api(k8s_client)
            ns = await k8s_api.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(
                name=k8s_namespace, labels={'workmate.test.owner':namespace})))
            assert ns.metadata.labels['workmate.test.owner'] == namespace
            k8s_uid = ns.metadata.uid
        await gateway.start()
        start_worker()
        headers = {'Authorization':'Bearer '+jwt.encode({'sub':'u','ver':0,'iat':int(time.time()),
            'exp':int(time.time())+900}, 'local-dual-gateway-signing-key-0123456789', algorithm='HS256')}
        async with httpx.AsyncClient(base_url=gateway.url, headers=headers, timeout=30) as http:
            created = await http.post('/api/tasks/conversations/new/tasks',
                json={'message':'Write approved fixture file', 'approval_profile':'strict'})
            assert created.status_code == 202, created.text
            task_id = created.json()['task']['id']
            await wait_value('SELECT status FROM tasks WHERE id=$1', task_id, 'awaiting_permission')
            await wait_value('SELECT status FROM runtime_suspensions WHERE task_id=$1 ORDER BY created_at DESC LIMIT 1',
                task_id, 'released')
            reply = await admin.fetchval('SELECT run_id FROM tasks WHERE id=$1', task_id)
            assert await admin.fetchval('SELECT owner_id FROM runtime_sessions WHERE active_task_id=$1', task_id) is None
            original_session = await admin.fetchval('SELECT id FROM runtime_sessions WHERE active_task_id=$1', task_id)
            snapshot_id = await admin.fetchval('SELECT snapshot_id FROM runtime_suspensions WHERE task_id=$1', task_id)
            original_snapshot = await admin.fetchval('SELECT payload::text FROM runtime_snapshots WHERE id=$1', snapshot_id)
            pending_before = (await http.get(f'/api/tasks/{task_id}/pending-confirm')).json()
            assert pending_before['toolCalls'][0]['id'] == 'remote-write'
            original_input = pending_before['toolCalls'][0]['input']
            assert (json.loads(original_input) if isinstance(original_input, str) else original_input) == {
                'file_path':'/workspace/output/result.txt', 'content':'approved exactly once'}
            assert writes == []
            assert len(resources) == 1
            old_workspace = resources[0]
            old_instance = source_ids[0]
            # A committed release must correspond to a destroyed old compute
            # instance, while the same named persistent volume/PVC survives.
            if provider_name == 'docker':
                import aiodocker
                async with aiodocker.Docker() as docker:
                    with pytest.raises(aiodocker.exceptions.DockerError) as gone:
                        await docker.containers.get(old_instance)
                    assert gone.value.status == 404
                    volume = await docker.volumes.get('workmate-' + old_workspace.workspace_id)
                    assert (await volume.show())['Labels']['workmate.test.owner'] == namespace
                    if decision == 'storage_lost':
                        await volume.delete()
            else:
                from kubernetes_asyncio.client.rest import ApiException
                async with asyncio.timeout(90):
                    while True:
                        try:
                            pod = await k8s_api.read_namespaced_pod(old_workspace._pod_name, k8s_namespace)
                            assert pod.metadata.uid == old_instance
                        except ApiException as error:
                            if error.status == 404:
                                break
                            raise
                        await asyncio.sleep(.5)
                pvc = await k8s_api.read_namespaced_persistent_volume_claim(old_workspace._pod_name, k8s_namespace)
                assert pvc.metadata.labels['agentscope.workspace.id'] == old_workspace.workspace_id
            shutdowns[0].set()
            await asyncio.wait_for(running[0], 60)
            start_worker()
            if decision == 'stop':
                response = await http.post(f'/api/tasks/{task_id}/stop')
                assert response.status_code == 200, response.text
                expected_status = 'cancelled'
            else:
                card = (await http.get(f'/api/tasks/{task_id}/pending-confirm')).json()
                assert card['toolCalls'] == pending_before['toolCalls']
                payload = {'decision_version':card['decisionVersion'],
                    'decisions':[{'tool_call_id':'remote-write','approved':decision in {'approve', 'storage_lost'}}]}
                invalid = await http.post(f'/api/tasks/{task_id}/confirm', json={**payload,
                    'decision_version':'stale-decision'})
                assert invalid.status_code in {400,409,422}, invalid.text
                response = await http.post(f'/api/tasks/{task_id}/confirm', json=payload)
                assert response.status_code == 200, response.text
                repeated = await http.post(f'/api/tasks/{task_id}/confirm', json=payload)
                assert repeated.status_code in {200,409}, repeated.text
                expected_status = 'failed' if decision == 'deny' else 'completed'
            if decision == 'storage_lost':
                async with asyncio.timeout(60):
                    while True:
                        status = await admin.fetchval('SELECT status FROM tasks WHERE id=$1', task_id)
                        if status in {'failed', 'quarantined'}:
                            break
                        await asyncio.sleep(.1)
                assert writes == []
                assert await admin.fetchval('SELECT run_id FROM tasks WHERE id=$1', task_id) == reply
                import aiodocker
                async with aiodocker.Docker() as docker:
                    with pytest.raises(aiodocker.exceptions.DockerError) as absent:
                        await docker.volumes.get('workmate-' + old_workspace.workspace_id)
                    assert absent.value.status == 404
                return
            await wait_value('SELECT status FROM tasks WHERE id=$1', task_id, expected_status)
            if decision == 'deny':
                finished = json.loads(await admin.fetchval("SELECT payload FROM task_events WHERE task_id=$1 AND event_type='run_finished' ORDER BY created_at DESC LIMIT 1", task_id))
                assert finished['completionGate'] == 'terminal_tool_failure'
                assert finished['reason'] == 'completed'
                assert await admin.fetchval("SELECT count(*) FROM task_events WHERE task_id=$1 AND event_type='tool_result' AND payload->>'status'='denied'", task_id) == 1
            assert await admin.fetchval('SELECT run_id FROM tasks WHERE id=$1', task_id) == reply
            assert await admin.fetchval('SELECT payload::text FROM runtime_snapshots WHERE id=$1', snapshot_id) == original_snapshot
            assert await admin.fetchval('SELECT session_id FROM runtime_task_executions WHERE task_id=$1', task_id) == original_session
            assert await admin.fetchval('SELECT count(*) FROM runtime_commands WHERE task_id=$1 AND session_id<>$2', task_id, original_session) == 0
            assert len(writes) == (1 if decision == 'approve' else 0)
            if writes:
                assert {key: value for key, value in writes[0].items() if key != '_agent_state'} == (json.loads(original_input) if isinstance(original_input, str) else original_input)
                assert write_identities == [(original_session, reply)]
            assert len(resources) == 2 and resources[1] is not old_workspace
            assert source_ids[1] != old_instance
            assert resources[1].workspace_id == old_workspace.workspace_id
            control = resources[1]._workmate_control
            result = await control.exec_shell([control.python, '-I', '-c',
                'import pathlib,sys;p=pathlib.Path(sys.argv[1]);assert (p/"input/original.txt").read_bytes()==b"original-input";'
                'q=p/"output/result.txt";assert q.read_bytes()==b"approved exactly once" if sys.argv[2]=="approve" else not q.exists()',
                resources[1].workdir, decision])
            assert result.exit_code == 0, result.stderr
            assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE task_id=$1 AND kind='ResumeInteraction'",
                task_id) == (0 if decision=='stop' else 1)
    finally:
        for event in shutdowns:
            event.set()
        results = await asyncio.gather(*(asyncio.wait_for(task, 60) for task in running), return_exceptions=True)
        await gateway.close()
        keys = [key async for key in redis.scan_iter(match=f'workmate:{namespace}:*')]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        errors = [error for error in results if isinstance(error, BaseException)]
        if provider_name == 'docker':
            import aiodocker
            async with aiodocker.Docker() as docker:
                for workspace_id in {w.workspace_id for w in resources}:
                    name = 'workmate-' + workspace_id
                    try:
                        try:
                            container = await docker.containers.get(name)
                        except aiodocker.exceptions.DockerError as error:
                            if error.status != 404: raise
                        else:
                            assert (await container.show())['Config']['Labels']['workmate.workspace'] == workspace_id
                            await container.delete(force=True)
                        try:
                            volume = await docker.volumes.get(name)
                        except aiodocker.exceptions.DockerError as error:
                            if error.status != 404: raise
                        else:
                            assert (await volume.show())['Labels']['workmate.test.owner'] == namespace
                            await volume.delete()
                    except Exception as error:
                        errors.append(error)
        elif k8s_client is not None:
            try:
                if k8s_uid is not None:
                    ns = await k8s_api.read_namespace(k8s_namespace)
                    assert ns.metadata.uid == k8s_uid and ns.metadata.labels['workmate.test.owner'] == namespace
                    await k8s_api.delete_namespace(k8s_namespace, body=client.V1DeleteOptions(
                        preconditions=client.V1Preconditions(uid=k8s_uid)))
                    from kubernetes_asyncio.client.rest import ApiException
                    async with asyncio.timeout(120):
                        while True:
                            try:
                                await k8s_api.read_namespace(k8s_namespace)
                            except ApiException as error:
                                if error.status == 404:
                                    break
                                raise
                            await asyncio.sleep(1)
            except Exception as error:
                errors.append(error)
            finally:
                await k8s_client.close()
        if errors:
            raise BaseExceptionGroup('Owned approval fixture cleanup failed', errors)
