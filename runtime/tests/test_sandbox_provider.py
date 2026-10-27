"""Provider delegation preserves workspace lifecycle and verified execution."""
from types import SimpleNamespace
import pytest
from workmate_runtime.sandbox import BubblewrapSandboxProvider, SandboxWorkspaceRequest
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager
from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.execution.controller import SandboxVerificationFailed
from test_worker_workspaces import StubWorkspace, IDENTITY, OWNER


async def test_manager_routes_lifecycle_and_binding_through_provider(tmp_path):
    class Recording(BubblewrapSandboxProvider):
        def __init__(self):
            super().__init__(workspace_factory=StubWorkspace)
            self.calls=[]
        def create_workspace(self, request):
            self.calls.append('create')
            assert isinstance(request, SandboxWorkspaceRequest)
            return super().create_workspace(request)
        async def initialize_workspace(self, workspace):
            self.calls.append('initialize')
            await super().initialize_workspace(workspace)
        def bind_backend(self, workspace, owner, registry):
            self.calls.append('bind')
            return super().bind_backend(workspace, owner, registry)
        async def close_workspace(self, workspace):
            self.calls.append('close')
            await super().close_workspace(workspace)
    provider=Recording()
    manager=WorkerWorkspaceManager(tmp_path,storage_node_id='node',resource_registry=TaskResourceRegistry(),
        ownership_provider=PosixWorkspaceOwnership(),provider=provider)
    binding=await manager.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain'])
    assert binding.provider is provider
    await manager.close()
    assert provider.calls==['create','initialize','bind','close']


@pytest.mark.parametrize('stdout,code', [(b'pid:[222]',0),(b'pid:[111]',0),(b'invalid',0),(b'pid:[222]',1)])
async def test_pid_proof_requires_different_valid_namespace(tmp_path,monkeypatch,stdout,code):
    provider=BubblewrapSandboxProvider(workspace_factory=StubWorkspace)
    workspace=provider.create_workspace(SandboxWorkspaceRequest('workspace',tmp_path))
    await provider.initialize_workspace(workspace)
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    backend=provider.bind_backend(workspace,OWNER,registry)
    async def run(*args,**kwargs):
        return SimpleNamespace(stdout=stdout,exit_code=code)
    backend.exec_shell=run
    monkeypatch.setattr('workmate_runtime.sandbox.bubblewrap.os.readlink',lambda _: 'pid:[111]')
    if stdout==b'pid:[222]' and code==0:
        await provider.verify_task_isolation(backend)
    else:
        with pytest.raises(SandboxVerificationFailed):
            await provider.verify_task_isolation(backend)


async def test_proof_rejects_untracked_backend():
    with pytest.raises(SandboxVerificationFailed):
        await BubblewrapSandboxProvider().verify_task_isolation(object())


def test_local_directory_required_and_unknown_backend_rejected():
    from workmate_runtime.sandbox import WorkspaceBindingError
    provider=BubblewrapSandboxProvider()
    with pytest.raises(WorkspaceBindingError):
        provider.create_workspace(SandboxWorkspaceRequest('workspace'))
    with pytest.raises(WorkspaceBindingError):
        provider.bind_backend(SimpleNamespace(get_backend=lambda: object()),OWNER,TaskResourceRegistry())


async def test_startup_probe_delegates_to_existing_bubblewrap_probe(monkeypatch):
    calls=[]
    async def probe():
        calls.append(True)
    monkeypatch.setattr('workmate_runtime.sandbox.bubblewrap.WorkmateSandboxWorkspace._probe_bubblewrap',probe)
    await BubblewrapSandboxProvider().probe()
    assert calls==[True]


@pytest.mark.parametrize('failure',[OSError('probe failed'),__import__('asyncio').CancelledError()])
async def test_probe_errors_fail_closed_and_cancellation_propagates(tmp_path,monkeypatch,failure):
    import asyncio
    provider=BubblewrapSandboxProvider(workspace_factory=StubWorkspace)
    workspace=provider.create_workspace(SandboxWorkspaceRequest('workspace',tmp_path))
    await provider.initialize_workspace(workspace)
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    backend=provider.bind_backend(workspace,OWNER,registry)
    async def fail(*args,**kwargs):
        raise failure
    backend.exec_shell=fail
    monkeypatch.setattr('workmate_runtime.sandbox.bubblewrap.os.readlink',lambda _: 'pid:[111]')
    expected=asyncio.CancelledError if isinstance(failure,asyncio.CancelledError) else SandboxVerificationFailed
    with pytest.raises(expected):
        await provider.verify_task_isolation(backend)


async def test_attestation_rejects_wrong_owner_before_probe_or_database():
    from workmate_runtime.assembly.sandbox_proof import attest_task_sandbox
    with pytest.raises(SandboxVerificationFailed):
        await attest_task_sandbox(SimpleNamespace(owner=None),OWNER,None)


async def test_attestation_requires_provider_proof_before_database():
    from workmate_runtime.assembly.sandbox_proof import attest_task_sandbox
    class Reject:
        async def verify_task_isolation(self,backend):
            raise SandboxVerificationFailed('provider proof failed')
    with pytest.raises(SandboxVerificationFailed,match='provider proof failed'):
        await attest_task_sandbox(SimpleNamespace(owner=OWNER,provider=Reject(),backend=SimpleNamespace(_execution_owner=OWNER)),OWNER,None)
