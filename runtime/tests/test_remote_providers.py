"""Pinned upstream constructors are wired directly, without AgentScope App managers."""
import pytest
from types import SimpleNamespace

from workmate_product.config.sandbox import parse_sandbox_settings
from workmate_runtime.sandbox.base import SandboxWorkspaceRequest, WorkspaceBindingError
from workmate_runtime.sandbox.factory import create_sandbox_provider
from workmate_runtime.sandbox.placement import workspace_request


@pytest.mark.parametrize('name,workspace_type',[
    ('docker','DockerWorkspace'),('kubernetes','K8sWorkspace'),('e2b','E2BWorkspace'),
    ('daytona','DaytonaWorkspace'),('opensandbox','OpenSandboxWorkspace')])
def test_native_provider_constructs_without_app_or_provisioning(name,workspace_type,tmp_path):
    import agentscope.workspace as workspaces
    provider = create_sandbox_provider(parse_sandbox_settings({'provider':name}))
    workspace = provider.create_workspace(SandboxWorkspaceRequest('test-sandbox'))
    assert isinstance(workspace,getattr(workspaces,workspace_type))
    assert workspace.host_workdir is None
    assert not workspace.is_alive
    assert workspace._workmate_provider==name


def test_provider_placement_is_persistent_and_cannot_switch(tmp_path):
    first = workspace_request(tmp_path,'logical',tmp_path/'files','docker')
    second = workspace_request(tmp_path,'logical',tmp_path/'files','docker')
    assert first.workspace_id == second.workspace_id != 'logical'
    with pytest.raises(WorkspaceBindingError,match='cannot change'):
        workspace_request(tmp_path,'logical',tmp_path/'files','e2b')


def test_legacy_local_workspace_cannot_silently_become_remote(tmp_path):
    workdir=tmp_path/'workspace'
    workdir.mkdir()
    (workdir/'existing.txt').write_text('previous conversation')
    with pytest.raises(WorkspaceBindingError,match='explicit migration'):
        workspace_request(tmp_path,'legacy',workdir,'docker')
    assert workspace_request(tmp_path,'legacy',workdir,'bubblewrap').workspace_id=='legacy'


@pytest.mark.parametrize('name',['docker','kubernetes','e2b','daytona','opensandbox'])
async def test_missing_sdk_fails_startup_without_fallback(name,monkeypatch):
    monkeypatch.setattr('workmate_runtime.sandbox.remote.importlib.util.find_spec',lambda _:None)
    provider=create_sandbox_provider(parse_sandbox_settings({'provider':name}))
    with pytest.raises(WorkspaceBindingError,match='SDK'):
        await provider.probe()


async def test_unknown_remote_provision_or_gateway_outcome_cannot_release_clean():
    provider=create_sandbox_provider(parse_sandbox_settings({'provider':'docker'}))
    for workspace in (
        SimpleNamespace(_workmate_provision_attempted=True,_workmate_control=None),
        SimpleNamespace(_workmate_gateway_starting=True,_workmate_control=object()),
    ):
        with pytest.raises(WorkspaceBindingError,match='unresolved'):
            await provider.close_workspace(workspace)


@pytest.mark.parametrize('blocked', [False, True])
async def test_kubernetes_close_waits_for_absence_after_bounded_grace(blocked):
    from unittest.mock import AsyncMock
    provider = create_sandbox_provider(parse_sandbox_settings({'provider':'kubernetes'}))
    calls = []
    async def step(name): calls.append(name)
    gateway = SimpleNamespace(stop=AsyncMock(side_effect=lambda: None), aclose=AsyncMock())
    async def stop(): await step('stop')
    async def close_gateway(): await step('gateway-close')
    async def delete(*args, **kwargs):
        assert calls == ['stop', 'gateway-close']
        assert kwargs == {'body': {
            'apiVersion': 'v1', 'kind': 'DeleteOptions', 'gracePeriodSeconds': 5, 'preconditions': {'uid': 'owned-uid'}}}
        await step('delete')
    async def wait(): await step('absent')
    gateway.stop.side_effect = stop
    gateway.aclose.side_effect = close_gateway
    api = SimpleNamespace(delete_namespaced_pod=AsyncMock(side_effect=delete))
    client = SimpleNamespace(close=AsyncMock())
    workspace = SimpleNamespace(_workmate_control=SimpleNamespace(operation=AsyncMock(return_value={
        'active': blocked, 'pids': []})), _gateway=gateway, _v1=api, _api_client=client,
        _pod_name='owned-pod', _namespace='owned-ns', _workmate_instance_id='owned-uid', _wait_pod_deleted=AsyncMock(side_effect=wait),
        is_alive=True, _backend=object())
    if blocked:
        with pytest.raises(WorkspaceBindingError, match='not quiescent'):
            await provider.close_workspace(workspace)
        api.delete_namespaced_pod.assert_not_awaited()
        gateway.stop.assert_not_awaited()
        assert workspace.is_alive
    else:
        await provider.close_workspace(workspace)
        assert calls == ['stop', 'gateway-close', 'delete', 'absent']
        assert not workspace.is_alive
        client.close.assert_awaited_once()


@pytest.mark.parametrize('identity', [None, '', 'owned-uid'])
async def test_kubernetes_delete_requires_exact_incarnation_and_keeps_failed_state(identity):
    from unittest.mock import AsyncMock
    provider = create_sandbox_provider(parse_sandbox_settings({'provider':'kubernetes'}))
    async def conflict(*args, **kwargs):
        assert kwargs['body']['preconditions']['uid'] == 'owned-uid'
        raise RuntimeError('409 UID precondition conflict')
    api = SimpleNamespace(delete_namespaced_pod=AsyncMock(side_effect=conflict))
    client = SimpleNamespace(close=AsyncMock())
    workspace = SimpleNamespace(_workmate_control=SimpleNamespace(operation=AsyncMock(return_value={
        'active': False, 'pids': []})), _gateway=None, _v1=api, _api_client=client,
        _pod_name='replacement-name', _namespace='owned-ns', _workmate_instance_id=identity,
        _wait_pod_deleted=AsyncMock(), is_alive=True, _backend=object())
    expected = RuntimeError if identity else WorkspaceBindingError
    with pytest.raises(expected):
        await provider.close_workspace(workspace)
    if not identity:
        api.delete_namespaced_pod.assert_not_awaited()
    workspace._wait_pod_deleted.assert_not_awaited()
    client.close.assert_not_awaited()
    assert workspace.is_alive and workspace._backend is not None
