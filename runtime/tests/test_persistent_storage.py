"""Persistent compute replacement must not silently replace workspace storage."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from workmate_runtime.sandbox.base import WorkspaceBindingError, SandboxWorkspaceRequest
from workmate_runtime.sandbox.placement import workspace_request


def proof(provider='docker'):
    return {'version':1,'provider':provider,'resource_id':'w','marker':'a'*64,
            'storage':({'name':'workmate-w','driver':'local','created_at':'2026-10-10T00:00:00Z'}
                       if provider=='docker' else {'name':'as-ws-w','namespace':'ns','uid':'pvc-uid'})}


def test_kubernetes_placement_fits_server_label_and_stays_stable(tmp_path):
    import re
    first = workspace_request(tmp_path, 'f' * 64, None, 'kubernetes')
    assert len(first.workspace_id) <= 63
    assert re.fullmatch(r'[a-z0-9]+', first.workspace_id)
    second = workspace_request(tmp_path, 'f' * 64, None, 'kubernetes')
    assert second.workspace_id == first.workspace_id


@pytest.mark.parametrize('field,value',[('version',2),('marker','bad'),('provider','e2b'),('resource_id','../bad')])
def test_rejects_invalid_storage_proof(field,value):
    from workmate_runtime.sandbox.persistent_storage import validate_storage_proof
    value_proof=proof();value_proof[field]=value
    with pytest.raises(WorkspaceBindingError):validate_storage_proof(value_proof)


async def test_docker_missing_volume_cannot_recreate():
    from workmate_runtime.sandbox.persistent_storage import read_storage_identity
    client=SimpleNamespace(volumes=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError('404'))))
    w=SimpleNamespace(_client=client,workspace_id='w',_workmate_provider='docker')
    with pytest.raises(WorkspaceBindingError):await read_storage_identity(w)
    assert not hasattr(client.volumes,'create')


@pytest.mark.parametrize('changed',['created_at','driver','name'])
async def test_docker_replaced_volume_rejected(changed):
    from workmate_runtime.sandbox.persistent_storage import verify_storage_identity
    expected=proof();metadata={'Name':'workmate-w','Driver':'local','CreatedAt':'2026-10-10T00:00:00Z'}
    metadata[{'created_at':'CreatedAt','driver':'Driver','name':'Name'}[changed]]='different'
    volume=SimpleNamespace(show=AsyncMock(return_value=metadata))
    w=SimpleNamespace(_client=SimpleNamespace(volumes=SimpleNamespace(get=AsyncMock(return_value=volume))),workspace_id='w',_workmate_provider='docker')
    with pytest.raises(WorkspaceBindingError):await verify_storage_identity(w,expected)


@pytest.mark.parametrize('uid,deleting',[('replacement',None),('pvc-uid','now')])
async def test_kubernetes_replacement_or_deleting_pvc_rejected(uid,deleting):
    from workmate_runtime.sandbox.persistent_storage import verify_storage_identity
    pvc=SimpleNamespace(metadata=SimpleNamespace(name='as-ws-w',namespace='ns',uid=uid,deletion_timestamp=deleting))
    w=SimpleNamespace(_v1=SimpleNamespace(read_namespaced_persistent_volume_claim=AsyncMock(return_value=pvc)),
        _namespace='ns',_pod_name='as-ws-w',workspace_id='w',_workmate_provider='kubernetes')
    with pytest.raises(WorkspaceBindingError):await verify_storage_identity(w,proof('kubernetes'))


async def test_marker_mismatch_never_rewritten():
    from workmate_runtime.sandbox.persistent_storage import check_marker
    backend=SimpleNamespace(python='/usr/bin/python3',exec_shell=AsyncMock(return_value=SimpleNamespace(exit_code=1,stdout=b'')))
    with pytest.raises(WorkspaceBindingError):await check_marker(backend,'/workspace',proof()['marker'],create=False)
    command=backend.exec_shell.call_args.args[0]
    assert command[-1]=='verify'
    assert backend.exec_shell.await_count==1


def test_placement_persists_and_compares_proof(tmp_path):
    from workmate_runtime.sandbox.persistent_storage import save_placement_proof
    first=workspace_request(tmp_path,'logical',tmp_path/'files','docker')
    stored=proof();stored['resource_id']=first.workspace_id;stored['storage']['name']='workmate-'+first.workspace_id
    save_placement_proof(first.placement_path,stored)
    again=workspace_request(tmp_path,'logical',tmp_path/'files','docker',expected_storage_proof=stored)
    assert again.expected_storage_proof==stored
    changed=deepcopy(stored);changed['marker']='b'*64
    with pytest.raises(WorkspaceBindingError):workspace_request(tmp_path,'logical',tmp_path/'files','docker',expected_storage_proof=changed)
    assert workspace_request(tmp_path,'logical',tmp_path/'files','docker').expected_storage_proof==stored


async def test_docker_preflight_prevents_container_creation_on_missing_original_volume(monkeypatch):
    from workmate_runtime.sandbox.native_workspaces import persistent_docker
    containers=SimpleNamespace(create=AsyncMock())
    class Base:
        _workmate_expected_storage_proof=proof()
        _workmate_provider='docker'
        workspace_id='w'
        _client=SimpleNamespace(containers=containers,volumes=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError('404'))))
    with pytest.raises(WorkspaceBindingError):await persistent_docker(Base)()._create_and_start_container()
    containers.create.assert_not_awaited()


async def test_kubernetes_cold_ensure_never_calls_create_pvc():
    from workmate_runtime.sandbox.native_workspaces import secure_kubernetes
    class Base:
        _workmate_expected_storage_proof=proof('kubernetes')
        _workmate_provider='kubernetes'
        workspace_id='w'
        _namespace='ns'
        _pod_name='as-ws-w'
        _v1=SimpleNamespace(read_namespaced_persistent_volume_claim=AsyncMock(side_effect=RuntimeError('404')))
        async def _ensure_pvc(self):pytest.fail('must not call create-on-missing upstream')
    with pytest.raises(WorkspaceBindingError):await secure_kubernetes(Base)()._ensure_pvc()


async def test_kubernetes_cold_rejects_any_existing_pod():
    from workmate_runtime.sandbox.native_workspaces import secure_kubernetes
    class Base:
        _workmate_expected_storage_proof=proof('kubernetes')
        _namespace='ns'
        _pod_name='as-ws-w'
        _v1=SimpleNamespace(read_namespaced_pod=AsyncMock(return_value=object()))
        async def _create_pod(self):pytest.fail('cannot replace an unproved existing Pod')
    with pytest.raises(WorkspaceBindingError,match='still exists'):await secure_kubernetes(Base)()._ensure_pod()


async def test_initial_and_reused_marker_actions_remain_distinct(monkeypatch):
    from workmate_runtime.sandbox import persistent_storage as module
    w=SimpleNamespace(_workmate_provider='docker',workspace_id='w',_workmate_control=object(),workdir='/workspace',_workmate_placement_path=None)
    monkeypatch.setattr(module,'read_storage_identity',AsyncMock(return_value=proof()['storage']))
    check=AsyncMock();monkeypatch.setattr(module,'check_marker',check)
    first=await module.initialize_storage_proof(w)
    assert check.call_args.kwargs=={'create':True}
    w._workmate_expected_storage_proof=first
    second=await module.initialize_storage_proof(w)
    assert first==second
    assert check.call_args.kwargs=={'create':False}


def test_placement_proof_replacement_never_changes_file(tmp_path):
    from workmate_runtime.sandbox.persistent_storage import save_placement_proof
    request=workspace_request(tmp_path,'logical',tmp_path/'files','docker')
    initial=proof();initial['resource_id']=request.workspace_id;initial['storage']['name']='workmate-'+request.workspace_id
    save_placement_proof(request.placement_path,initial)
    before=request.placement_path.read_bytes()
    altered=deepcopy(initial);altered['marker']='b'*64
    with pytest.raises(WorkspaceBindingError):save_placement_proof(request.placement_path,altered)
    assert request.placement_path.read_bytes()==before


def test_untrusted_placement_mode_is_rejected(tmp_path):
    request=workspace_request(tmp_path,'logical',tmp_path/'files','docker')
    request.placement_path.chmod(0o666)
    with pytest.raises(WorkspaceBindingError,match='Unsafe'):
        workspace_request(tmp_path,'logical',tmp_path/'files','docker')


async def test_provider_verification_rejects_other_durable_marker(monkeypatch):
    from workmate_runtime.sandbox.remote import RemoteSandboxProvider
    provider=RemoteSandboxProvider(SimpleNamespace(provider='docker'))
    provider.capture_persistent_storage=AsyncMock(return_value=proof())
    expected=proof();expected['marker']='b'*64
    with pytest.raises(WorkspaceBindingError,match='durable'):
        await provider.verify_persistent_storage(object(),expected)


@pytest.mark.parametrize('provider_name',['docker','kubernetes'])
async def test_failed_original_storage_preflight_closes_only_client(provider_name):
    from workmate_runtime.sandbox.native_workspaces import persistent_docker, secure_kubernetes
    from workmate_runtime.sandbox.remote import RemoteSandboxProvider
    client=SimpleNamespace(close=AsyncMock(),volumes=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError('404'))))
    api=SimpleNamespace(read_namespaced_persistent_volume_claim=AsyncMock(side_effect=RuntimeError('404')),
                        delete_namespaced_pod=AsyncMock())
    class Base:
        _workmate_expected_storage_proof=proof(provider_name)
        _workmate_provider=provider_name
        _workmate_provision_attempted=True
        _workmate_control=None
        _workmate_instance_id=None
        workspace_id='w'
        _client=client
        _api_client=client
        _container=None
        _v1=api
        _namespace='ns'
        _pod_name='as-ws-w'
        _backend=None
        is_alive=False
    workspace=(persistent_docker(Base) if provider_name=='docker' else secure_kubernetes(Base))()
    with pytest.raises(WorkspaceBindingError):
        if provider_name=='docker':await workspace._create_and_start_container()
        else:await workspace._ensure_pvc()
    await RemoteSandboxProvider(SimpleNamespace(provider=provider_name)).close_workspace(workspace)
    client.close.assert_awaited_once()
    api.delete_namespaced_pod.assert_not_awaited()


async def test_create_timeout_is_not_a_clean_preflight_failure(monkeypatch):
    from workmate_runtime.sandbox.native_workspaces import persistent_docker
    from workmate_runtime.sandbox.remote import RemoteSandboxProvider
    monkeypatch.setattr('workmate_runtime.sandbox.persistent_storage.verify_storage_identity',AsyncMock(return_value=proof()))
    client=SimpleNamespace(close=AsyncMock(),containers=SimpleNamespace(create=AsyncMock(side_effect=TimeoutError('unknown create result'))))
    class Base:
        _workmate_expected_storage_proof=proof()
        _workmate_provider='docker'
        _workmate_provision_attempted=True
        _workmate_control=None
        _client=client
        _container=None
        _image_tag='image'
        workspace_id='w'
        workdir='/workspace'
    workspace=persistent_docker(Base)()
    with pytest.raises(TimeoutError):await workspace._create_and_start_container()
    with pytest.raises(WorkspaceBindingError,match='unresolved'):
        await RemoteSandboxProvider(SimpleNamespace(provider='docker')).close_workspace(workspace)
    client.close.assert_not_awaited()


@pytest.mark.parametrize('phase',['existing_pod','create_timeout'])
async def test_kubernetes_post_preflight_unknown_compute_stays_blocked(phase,monkeypatch):
    from workmate_runtime.sandbox.native_workspaces import secure_kubernetes
    from workmate_runtime.sandbox.remote import RemoteSandboxProvider
    from kubernetes_asyncio.client.rest import ApiException
    monkeypatch.setattr('workmate_runtime.sandbox.persistent_storage.verify_storage_identity',AsyncMock(return_value=proof('kubernetes')))
    client=SimpleNamespace(close=AsyncMock())
    api=SimpleNamespace(read_namespaced_pod=AsyncMock(return_value=object()),
                        create_namespaced_pod=AsyncMock(side_effect=TimeoutError('unknown creation')))
    if phase=='create_timeout':api.read_namespaced_pod.side_effect=ApiException(status=404)
    class Base:
        _workmate_expected_storage_proof=proof('kubernetes')
        _workmate_provider='kubernetes'
        _workmate_provision_attempted=True
        _workmate_control=None
        _workmate_instance_id=None
        _backend=None
        _api_client=client
        _namespace='ns'
        _pod_name='as-ws-w'
        _v1=api
        async def _create_pod(self):
            await self._v1.create_namespaced_pod('ns',SimpleNamespace(
                metadata=SimpleNamespace(name='as-ws-w',namespace='ns'),spec=SimpleNamespace()))
    workspace=secure_kubernetes(Base)()
    await workspace._ensure_pvc()
    with pytest.raises((WorkspaceBindingError,TimeoutError)):await workspace._ensure_pod()
    with pytest.raises(WorkspaceBindingError,match='unresolved'):
        await RemoteSandboxProvider(SimpleNamespace(provider='kubernetes')).close_workspace(workspace)
    client.close.assert_not_awaited()
