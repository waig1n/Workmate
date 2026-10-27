"""Core-only workspace lifecycle; fixtures never bootstrap or access networks."""
import asyncio
from dataclasses import replace

import pytest

from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager,WorkspaceIdentity,WorkspaceBindingError
from workmate_runtime.execution.resources import TaskResourceRegistry,TaskResourceBackendMixin,ResourceScopeError
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.workspaces import WorkmateClearedEnvBubblewrapBackend


IDENTITY = WorkspaceIdentity('user','agent','conversation','session','node')
OWNER = ExecutionOwner('session','task','worker',1)


class StubWorkspace:
    made = []

    def __init__(self, **kwargs):
        self.host_workdir = kwargs['host_workdir']
        self.workspace_id = kwargs['workspace_id']
        self.workdir = '/workspace'
        self.instructions = 'Work in {workdir}'
        self._glob_helper_path = None
        self.initialized = self.closed = 0
        self._backend = None
        StubWorkspace.made.append(self)

    async def initialize(self):
        self.initialized += 1
        await asyncio.sleep(0)
        self._backend = WorkmateClearedEnvBubblewrapBackend(host_workdir=self.host_workdir,
            host_tmpdir=self.host_workdir+'-tmp',workdir=self.workdir)

    async def close(self):
        self.closed += 1

    def get_backend(self):
        return self._backend


def manager(tmp_path, factory=StubWorkspace):
    return WorkerWorkspaceManager(tmp_path,storage_node_id='node',resource_registry=TaskResourceRegistry(),
        workspace_factory=factory,ownership_provider=PosixWorkspaceOwnership())


async def test_concurrent_get_initializes_once_and_stable_identity_reuses_directory(tmp_path):
    first = manager(tmp_path)
    workspaces = await asyncio.gather(*(first.get_workspace(IDENTITY) for _ in range(3)))
    assert all(ws is workspaces[0] for ws in workspaces)
    assert workspaces[0].initialized == 1
    path = workspaces[0].host_workdir
    from pathlib import Path
    assert all((Path(path)/name).is_dir() for name in ('input','working','output'))
    await first.close()
    assert workspaces[0].closed == 1
    second = manager(tmp_path)
    assert (await second.get_workspace(IDENTITY)).host_workdir == path
    await second.close()


async def test_wrong_node_and_rebound_session_reject_before_initialization(tmp_path):
    mgr = manager(tmp_path)
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(replace(IDENTITY,storage_node_id='foreign'))
    await mgr.get_workspace(IDENTITY)
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(replace(IDENTITY,conversation_id='foreign'))
    await mgr.close()


async def test_task_tools_and_offloader_use_separate_backend_and_resume_reuses_scope(tmp_path):
    mgr = manager(tmp_path)
    first = await mgr.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain'])
    again = await mgr.bind_task(IDENTITY,OWNER,fresh_claim=False,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain','image/png'])
    assert first.backend is again.backend
    assert first.workspace.get_backend() is not first.backend
    assert isinstance(first.backend,TaskResourceBackendMixin)
    assert all(tool._backend is first.backend for tool in first.tools)
    assert again.offloader.get_backend() is again.backend
    assert first.instructions == 'Work in /workspace'
    read_first = next(t for t in first.tools if t.name=='Read')
    read_again = next(t for t in again.tools if t.name=='Read')
    assert read_first.model_input_types == ['text/plain']
    assert read_again.model_input_types == ['text/plain','image/png']
    # Closing workspace resources is never evidence of Task cancellation.
    await mgr.close()
    assert first.workspace.closed == 1


async def test_resume_without_same_process_binding_cannot_create_empty_scope(tmp_path):
    mgr = manager(tmp_path)
    with pytest.raises(WorkspaceBindingError):
        await mgr.bind_task(IDENTITY,OWNER,fresh_claim=False,all_resource_paths_tracked=True,
            read_model_input_types=['text/plain'])
    assert await mgr.resource_registry.verify_cleanup(OWNER,None,None) is False
    await mgr.close()


async def test_failed_initialization_closes_partial_workspace_and_can_retry(tmp_path):
    instances = []
    class Failing(StubWorkspace):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            instances.append(self)
        async def initialize(self):
            if len(instances)==1:
                raise RuntimeError('fixture initialization failed')
            await super().initialize()
    mgr = manager(tmp_path,Failing)
    with pytest.raises(RuntimeError):
        await mgr.get_workspace(IDENTITY)
    assert instances[0].closed == 1
    assert await mgr.get_workspace(IDENTITY) is instances[1]
    await mgr.close()
    assert instances[1].closed == 1


async def test_close_joins_initialization_and_does_not_return_a_closed_workspace(tmp_path):
    entered,release = asyncio.Event(),asyncio.Event()
    class Slow(StubWorkspace):
        async def initialize(self):
            entered.set()
            await release.wait()
            await super().initialize()
    mgr = manager(tmp_path,Slow)
    get = asyncio.create_task(mgr.get_workspace(IDENTITY))
    await entered.wait()
    close = asyncio.create_task(mgr.close())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(WorkspaceBindingError):
        await get
    await close
    await mgr.close()


async def test_cancelling_one_waiter_does_not_cancel_shared_initialization(tmp_path):
    entered,release = asyncio.Event(),asyncio.Event()
    class Slow(StubWorkspace):
        async def initialize(self):
            entered.set()
            await release.wait()
            await super().initialize()
    mgr = manager(tmp_path,Slow)
    first = asyncio.create_task(mgr.get_workspace(IDENTITY))
    await entered.wait()
    second = asyncio.create_task(mgr.get_workspace(IDENTITY))
    first.cancel()
    await asyncio.gather(first,return_exceptions=True)
    release.set()
    assert (await second).initialized == 1
    await mgr.close()


async def test_offloader_writes_all_payloads_through_task_backend_and_checks_session(tmp_path):
    from agentscope.message import UserMsg,ToolResultBlock,TextBlock,DataBlock,Base64Source
    mgr = manager(tmp_path)
    bound = await mgr.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain'])
    writes = {}
    async def read(path):
        if path not in writes:
            raise FileNotFoundError(path)
        return writes[path]
    async def write(path,content):
        writes[path] = content
    async def exists(path):
        return path in writes
    bound.backend.read_file,bound.backend.write_file,bound.backend.file_exists = read,write,exists
    await bound.offloader.offload_context('session',[UserMsg(name='user',content='test')])
    await bound.offloader.offload_tool_result('session',ToolResultBlock(id='call',name='tool',
        output=[TextBlock(text='result')]))
    await bound.offloader.offload_data_block(DataBlock(name='data',source=Base64Source(
        data='aGVsbG8=',media_type='text/plain')))
    assert len(writes) == 3 and all(path.startswith('/workspace/') for path in writes)
    with pytest.raises(WorkspaceBindingError):
        await bound.offloader.offload_context('foreign',[])
    await mgr.close()
    with pytest.raises(ResourceScopeError):
        bound.offloader.get_backend()


def test_workspace_import_does_not_load_agentscope_app_or_product_http():
    import os,subprocess,sys
    probe = '''
import importlib.abc,sys
class RejectApp(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in {'app','fastapi'} or fullname.startswith(('app.','agentscope.app')):
            raise AssertionError('App import forbidden: '+fullname)
sys.meta_path.insert(0,RejectApp())
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager
'''
    result = subprocess.run([sys.executable,'-c',probe],capture_output=True,text=True,timeout=15,
        env={**os.environ,'COVERAGE_PROCESS_START':''})
    assert result.returncode == 0,result.stderr


@pytest.mark.parametrize('media', [None,[],[''],[1]])
async def test_invalid_task_media_configuration_does_not_open_resource_scope(tmp_path,media):
    mgr = manager(tmp_path)
    with pytest.raises(ValueError):
        await mgr.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
            read_model_input_types=media)
    assert await mgr.resource_registry.verify_cleanup(OWNER,None,None) is False
    await mgr.close()


@pytest.mark.parametrize('owner,fresh,coverage', [
    (replace(OWNER,session_id='foreign'),True,True),(OWNER,1,True),(OWNER,True,False)])
async def test_task_binding_rejects_invalid_identity_and_coverage(tmp_path,owner,fresh,coverage):
    mgr = manager(tmp_path)
    with pytest.raises(WorkspaceBindingError):
        await mgr.bind_task(IDENTITY,owner,fresh_claim=fresh,all_resource_paths_tracked=coverage,
            read_model_input_types=['text/plain'])
    await mgr.close()


async def test_nested_backend_retains_pid_namespace_policy_and_gateway_identity(tmp_path):
    from workmate_runtime.workspaces import WorkmateNestedBubblewrapBackend
    class Nested(StubWorkspace):
        async def initialize(self):
            self._backend = WorkmateNestedBubblewrapBackend(host_workdir=self.host_workdir,
                host_tmpdir=self.host_workdir+'-tmp',workdir=self.workdir)
    mgr = manager(tmp_path,Nested)
    binding = await mgr.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain'])
    assert isinstance(binding.backend,WorkmateNestedBubblewrapBackend)
    argv = binding.backend._bwrap_argv(['true'],cwd=None)
    assert '--unshare-pid' in argv and '--cap-drop' in argv
    assert not isinstance(binding.workspace.get_backend(),TaskResourceBackendMixin)
    await mgr.close()


async def test_mount_root_symlink_is_rejected(tmp_path):
    import hashlib,json
    digest = hashlib.sha256(json.dumps(['user','agent','conversation','session'],separators=(',',':')).encode()).hexdigest()
    outside = tmp_path/'outside'
    outside.mkdir()
    base = tmp_path/'root'
    mgr = manager(base)
    (base/digest).symlink_to(outside,target_is_directory=True)
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(IDENTITY)
    await mgr.close()


async def test_close_attempts_every_workspace_and_reports_failure(tmp_path):
    instances = []
    class CloseFailure(StubWorkspace):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            instances.append(self)
        async def close(self):
            self.closed += 1
            if self is instances[0]:
                raise OSError('fixture close failed')
    mgr = manager(tmp_path,CloseFailure)
    await mgr.get_workspace(IDENTITY)
    await mgr.get_workspace(replace(IDENTITY,session_id='session-2',conversation_id='conversation-2'))
    with pytest.raises(ExceptionGroup):
        await mgr.close()
    assert [ws.closed for ws in instances] == [1,1]
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(IDENTITY)


async def test_failed_initialization_cleanup_retains_workspace_and_blocks_second_gateway(tmp_path):
    instances = []
    class LeakingInit(StubWorkspace):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            instances.append(self)
        async def initialize(self):
            raise RuntimeError('fixture initialization failed after gateway creation')
        async def close(self):
            self.closed += 1
            if self.closed == 1:
                raise OSError('fixture cleanup failed')
    mgr = manager(tmp_path,LeakingInit)
    for _ in range(2):
        with pytest.raises(Exception):
            await mgr.get_workspace(IDENTITY)
    assert len(instances) == 1 and instances[0].closed == 1
    await mgr.close()
    assert instances[0].closed == 2


async def test_cancelled_workspace_close_is_reported_and_can_retry(tmp_path):
    instances = []
    class InterruptedClose(StubWorkspace):
        def __init__(self,**kwargs):
            super().__init__(**kwargs)
            instances.append(self)
        async def close(self):
            self.closed += 1
            if self is instances[0] and self.closed == 1:
                raise asyncio.CancelledError()
    mgr = manager(tmp_path,InterruptedClose)
    await mgr.get_workspace(IDENTITY)
    await mgr.get_workspace(replace(IDENTITY,session_id='session-2',conversation_id='conversation-2'))
    with pytest.raises(BaseExceptionGroup) as error:
        await mgr.close()
    assert any(isinstance(e,asyncio.CancelledError) for e in error.value.exceptions)
    await mgr.close()
    assert [ws.closed for ws in instances] == [2,1]
