"""Opt-in actual local Kubernetes/PVC recovery, with local immutable object fixture.

Requires explicit WORKMATE_TEST_K8S_CONFIG, WORKMATE_TEST_K8S_IMAGE preloaded in the cluster,
and WORKMATE_TEST_K8S_NAMESPACE beginning workmate-checkpoint-. Never uses a default
namespace/context. This test owns and removes its labelled temporary namespace.
"""
import asyncio
import os
import uuid

import pytest

from workmate_product.config.sandbox import parse_sandbox_settings, sandbox_environment
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.sandbox.base import SandboxWorkspaceRequest
from workmate_runtime.sandbox.remote import RemoteSandboxProvider
from workmate_runtime.transport.postgres import ExecutionOwner
from test_remote_checkpoint_docker_integration import LocalImmutableObjects


@pytest.mark.skipif(not all(os.environ.get(key) for key in (
    'WORKMATE_TEST_K8S_CONFIG', 'WORKMATE_TEST_K8S_IMAGE', 'WORKMATE_TEST_K8S_NAMESPACE')),
    reason='Opt in with an explicit disposable Kubernetes cluster, image and namespace')
@pytest.mark.asyncio
async def test_real_kubernetes_checkpoint_fresh_pvc_and_task_uid(tmp_path, monkeypatch):
    from kubernetes_asyncio import client, config
    from kubernetes_asyncio.client.rest import ApiException
    from workmate_runtime.delivery.remote_workspace_checkpoint import RemoteWorkspaceCheckpoint
    from workmate_runtime.delivery.workspace_checkpoint import CheckpointError

    namespace = os.environ['WORKMATE_TEST_K8S_NAMESPACE']
    assert namespace.startswith('workmate-checkpoint-') and len(namespace) <= 63
    kubeconfig = os.path.abspath(os.environ['WORKMATE_TEST_K8S_CONFIG'])
    assert os.path.isfile(kubeconfig)
    image = os.environ['WORKMATE_TEST_K8S_IMAGE']
    api_client = await config.new_client_from_config(config_file=kubeconfig)
    api = client.CoreV1Api(api_client)
    namespace_uid = None
    namespace_owner = uuid.uuid4().hex
    workspaces, owners = [], []
    registry = TaskResourceRegistry(cleanup_timeout=30)
    store = LocalImmutableObjects(tmp_path / 'objects')
    monkeypatch.setenv('WORKMATE_SANDBOX_PROVIDER', 'kubernetes')
    monkeypatch.setenv('WORKMATE_SANDBOX_KUBECONFIG', kubeconfig)
    provider = RemoteSandboxProvider(sandbox_environment(parse_sandbox_settings(dict(provider='kubernetes',
        namespace=namespace, image=image, storage_size='128Mi'))))

    async def absent(read, name):
        async with asyncio.timeout(90):
            while True:
                try:
                    await read(name, namespace)
                except ApiException as error:
                    if error.status == 404:
                        return
                    raise
                await asyncio.sleep(0.5)

    async def make(role):
        workspace = provider.create_workspace(SandboxWorkspaceRequest('cp-' + role + '-' + uuid.uuid4().hex[:12]))
        # Use the explicitly preloaded candidate, never fetch a registry image.
        workspace._image_pull_policy = 'Never'
        workspaces.append(workspace)
        await provider.initialize_workspace(workspace)
        pod = await api.read_namespaced_pod(workspace._pod_name, namespace)
        assert pod.metadata.labels['agentscope.workspace.id'] == workspace.workspace_id
        assert pod.metadata.uid == workspace._workmate_instance_id
        assert pod.spec.automount_service_account_token is False
        assert not any(volume.projected for volume in pod.spec.volumes)
        pvc = await api.read_namespaced_persistent_volume_claim(workspace._pod_name, namespace)
        assert pvc.metadata.labels['agentscope.workspace.id'] == workspace.workspace_id
        token = await workspace._workmate_control.exec_shell([workspace._workmate_control.python, '-I', '-c',
            'import os;assert not os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")'])
        assert token.exit_code == 0
        return workspace, pod.metadata.uid, pvc.metadata.uid

    async def bind(workspace, role):
        owner = ExecutionOwner(workspace.workspace_id, 'task-' + role, namespace, 1)
        owners.append(owner)
        registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
        backend = provider.bind_backend(workspace, owner, registry)
        evidence = await provider.verify_task_isolation(backend)
        assert evidence['evidence']['uids'] == [62000] * 3
        return owner, backend

    def checkpoint(workspace):
        return RemoteWorkspaceCheckpoint(workspace._workmate_control, workspace.workdir, store,
            provider='kubernetes', max_files=20, max_bytes=1024*1024, max_file_bytes=512*1024)

    try:
        # Creation is the ownership boundary. A name collision (409) fails
        # before recording ownership; never adopt or delete existing resources.
        ns = await api.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(
            name=namespace, labels={'workmate.test.owner': namespace_owner})))
        assert ns.metadata.labels.get('workmate.test.owner') == namespace_owner
        namespace_uid = ns.metadata.uid
        await provider.probe()
        source, source_pod_uid, source_pvc_uid = await make('source')
        owner, backend = await bind(source, 'source')
        expected = {'input/request.txt': '原始输入'.encode(),
                    'working/binary.dat': bytes(range(256))*513,
                    'working/run.sh': b'#!/bin/sh\nprintf recovered-kubernetes',
                    'output/report.txt': '成果'.encode()}
        for path, content in expected.items():
            await backend.write_file(source.workdir + '/' + path, content)
        result = await backend.exec_shell(['chmod', '700', source.workdir + '/working/run.sh'])
        assert result.exit_code == 0
        assert await registry.verify_preparation_cleanup(owner)
        assert await backend.quiescent()
        manifest = await checkpoint(source).capture()
        assert {entry['path'] for entry in manifest['files']} == set(expected)
        await provider.close_workspace(source)
        await absent(api.read_namespaced_pod, source._pod_name)
        pvc = await api.read_namespaced_persistent_volume_claim(source._pod_name, namespace)
        assert pvc.metadata.uid == source_pvc_uid
        assert pvc.metadata.labels['agentscope.workspace.id'] == source.workspace_id
        await api.delete_namespaced_persistent_volume_claim(source._pod_name, namespace,
            body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=source_pvc_uid)))
        await absent(api.read_namespaced_persistent_volume_claim, source._pod_name)

        target, target_pod_uid, target_pvc_uid = await make('target')
        assert target_pod_uid != source_pod_uid and target_pvc_uid != source_pvc_uid
        await checkpoint(target).restore(manifest)
        with pytest.raises(CheckpointError):
            await checkpoint(target).restore(manifest)
        owner, backend = await bind(target, 'restored')
        for path, content in expected.items():
            assert await backend.read_file(target.workdir + '/' + path) == content
        result = await backend.exec_shell([target.workdir + '/working/run.sh'])
        assert result.exit_code == 0 and result.stdout == b'recovered-kubernetes'
        assert await registry.verify_preparation_cleanup(owner)
        assert await backend.quiescent()
        await provider.close_workspace(target)
    finally:
        errors = []
        for owner in owners:
            try:
                await registry.verify_preparation_cleanup(owner)
            except Exception as error:
                errors.append(error)
        for workspace in workspaces:
            if getattr(workspace, '_api_client', None) is not None:
                try:
                    await workspace._api_client.close()
                except Exception as error:
                    errors.append(error)
        try:
            if namespace_uid is not None:
                ns = await api.read_namespace(namespace)
                assert ns.metadata.uid == namespace_uid
                assert ns.metadata.labels.get('workmate.test.owner') == namespace_owner
                await api.delete_namespace(namespace, body=client.V1DeleteOptions(
                    preconditions=client.V1Preconditions(uid=namespace_uid)))
                async with asyncio.timeout(120):
                    while True:
                        try:
                            await api.read_namespace(namespace)
                        except ApiException as error:
                            if error.status == 404:
                                break
                            raise
                        await asyncio.sleep(1)
        except Exception as error:
            errors.append(error)
        finally:
            await api_client.close()
        if errors:
            raise RuntimeError(f'Test-owned Kubernetes cleanup failed: {errors!r}')
