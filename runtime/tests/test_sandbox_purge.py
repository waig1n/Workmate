"""Provider deletion refuses live compute and does not provision replacements."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import os
import uuid
import pytest
from workmate_runtime.sandbox.purge import purge_provider_storage
from workmate_runtime.sandbox.base import WorkspaceBindingError


@pytest.mark.parametrize('provider', ['bubblewrap', 'local'])
async def test_local_storage_needs_no_provider_api(provider):
    await purge_provider_storage(SimpleNamespace(name=provider), 'a'*64)


@pytest.mark.parametrize('resource', ['', '../other', 'a'*63, 'A'*64])
async def test_invalid_identity_is_rejected(resource):
    with pytest.raises(WorkspaceBindingError):
        await purge_provider_storage(SimpleNamespace(name='docker'), resource)


@pytest.mark.parametrize('exists', [True, False])
async def test_docker_refuses_existing_container_and_deletes_volume_without_force(monkeypatch, exists):
    from aiodocker.exceptions import DockerError
    volume = SimpleNamespace(delete=AsyncMock())
    lookup = AsyncMock(return_value=object()) if exists else AsyncMock(side_effect=DockerError(404, {}))
    client = SimpleNamespace(containers=SimpleNamespace(get=lookup),
        volumes=SimpleNamespace(get=AsyncMock(return_value=volume)))
    class Context:
        async def __aenter__(self): return client
        async def __aexit__(self, *args): pass
    monkeypatch.setattr('aiodocker.Docker', Context)
    if exists:
        with pytest.raises(WorkspaceBindingError):
            await purge_provider_storage(SimpleNamespace(name='docker'), 'a'*64)
        volume.delete.assert_not_called()
    else:
        await purge_provider_storage(SimpleNamespace(name='docker'), 'a'*64)
        volume.delete.assert_awaited_once_with()


@pytest.mark.parametrize('case', ['pod', 'foreign', 'uid_missing', 'delete', 'missing'])
async def test_kubernetes_requires_absent_pod_and_matching_pvc_label(monkeypatch, case):
    from kubernetes_asyncio.client.rest import ApiException
    missing = ApiException(status=404)
    pvc = SimpleNamespace(metadata=SimpleNamespace(uid=None if case=='uid_missing' else 'exact-uid', labels={
        'agentscope.workspace.id': 'foreign' if case=='foreign' else 'a'*64}))
    api = SimpleNamespace(
        read_namespaced_pod=AsyncMock(return_value=object()) if case=='pod' else AsyncMock(side_effect=missing),
        read_namespaced_persistent_volume_claim=AsyncMock(side_effect=[missing] if case=='missing' else [pvc, missing]),
        delete_namespaced_persistent_volume_claim=AsyncMock())
    class Context:
        async def __aenter__(self): return object()
        async def __aexit__(self, *args): pass
    monkeypatch.setattr('kubernetes_asyncio.client.ApiClient', Context)
    monkeypatch.setattr('kubernetes_asyncio.client.CoreV1Api', lambda _: api)
    monkeypatch.setattr('kubernetes_asyncio.config.load_kube_config', AsyncMock())
    provider = SimpleNamespace(name='kubernetes', settings=SimpleNamespace(kubeconfig='fixture', namespace='ns'))
    if case in {'pod', 'foreign', 'uid_missing'}:
        with pytest.raises(WorkspaceBindingError):
            await purge_provider_storage(provider, 'a'*64)
        api.delete_namespaced_persistent_volume_claim.assert_not_called()
    else:
        await purge_provider_storage(provider, 'a'*64)
        if case=='delete':
            call = api.delete_namespaced_persistent_volume_claim.call_args
            assert call.kwargs['body'].preconditions.uid == 'exact-uid'
        else:
            api.delete_namespaced_persistent_volume_claim.assert_not_called()


@pytest.mark.skipif(os.getenv('WORKMATE_TEST_DOCKER_PURGE') != '1', reason='requires opt-in local Docker')
async def test_real_docker_purge_removes_only_test_owned_volume():
    import aiodocker
    from aiodocker.exceptions import DockerError
    resource = uuid.uuid4().hex + uuid.uuid4().hex
    name = 'workmate-' + resource
    async with aiodocker.Docker() as client:
        volume = await client.volumes.create({'Name': name})
        try:
            await purge_provider_storage(SimpleNamespace(name='docker'), resource)
            with pytest.raises(DockerError) as error:
                await client.volumes.get(name)
            assert error.value.status == 404
            await purge_provider_storage(SimpleNamespace(name='docker'), resource)
        finally:
            try:
                await volume.delete()
            except DockerError as error:
                if error.status != 404:
                    raise


async def test_unimplemented_cloud_purge_fails_closed():
    with pytest.raises(WorkspaceBindingError, match='not implemented'):
        await purge_provider_storage(SimpleNamespace(name='e2b'), 'a'*64)
