"""Ownership latch integration precedes gateway launch and survives cleanup faults."""
import asyncio
from dataclasses import replace

import pytest
from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.assembly.workspaces import WorkspaceBindingError
from test_worker_workspaces import manager,StubWorkspace,IDENTITY,OWNER


async def test_second_manager_is_rejected_before_factory(tmp_path):
    first=manager(tmp_path)
    await first.get_workspace(IDENTITY)
    calls=[]
    def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError('gateway factory must not run')
    second=manager(tmp_path,forbidden)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    assert calls==[]
    await first.close();await second.close()


async def test_gateway_close_failure_retains_ownership_until_retry(tmp_path):
    class Flaky(StubWorkspace):
        async def close(self):
            self.closed+=1
            if self.closed==1: raise OSError('gateway remains alive')
    first=manager(tmp_path,Flaky)
    workspace=await first.get_workspace(IDENTITY)
    with pytest.raises(ExceptionGroup): await first.close()
    second=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    await first.close()
    assert workspace.closed==2
    await second.get_workspace(IDENTITY)
    await second.close()


async def test_factory_failure_releases_clean_ownership(tmp_path):
    def broken(**kwargs): raise RuntimeError('factory failed before gateway')
    failed=manager(tmp_path,broken)
    with pytest.raises(RuntimeError,match='factory failed'): await failed.get_workspace(IDENTITY)
    second=manager(tmp_path)
    await second.get_workspace(IDENTITY)
    await second.close();await failed.close()


async def test_dirty_record_failure_never_starts_factory(tmp_path,monkeypatch):
    calls=[]
    original=PosixWorkspaceOwnership._write
    def fail_dirty(self,*,dirty):
        if dirty: raise OSError('dirty record cannot persist')
        return original(self,dirty=dirty)
    def forbidden(**kwargs): calls.append(kwargs)
    monkeypatch.setattr(PosixWorkspaceOwnership,'_write',fail_dirty)
    failed=manager(tmp_path,forbidden)
    with pytest.raises(WorkspaceBindingError): await failed.get_workspace(IDENTITY)
    assert calls==[]
    monkeypatch.setattr(PosixWorkspaceOwnership,'_write',original)
    await failed.close()
    second=manager(tmp_path)
    await second.get_workspace(IDENTITY);await second.close()


async def test_clean_record_failure_retries_without_closing_gateway_twice(tmp_path,monkeypatch):
    first=manager(tmp_path)
    workspace=await first.get_workspace(IDENTITY)
    original=PosixWorkspaceOwnership._write
    def fail_clean(self,*,dirty):
        if not dirty: raise OSError('clean record cannot persist')
        return original(self,dirty=dirty)
    monkeypatch.setattr(PosixWorkspaceOwnership,'_write',fail_clean)
    with pytest.raises(ExceptionGroup): await first.close()
    assert workspace.closed==1
    second=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    monkeypatch.setattr(PosixWorkspaceOwnership,'_write',original)
    await first.close()
    assert workspace.closed==1
    await second.get_workspace(IDENTITY);await second.close()


async def test_unfinished_external_resource_blocks_clean_handoff(tmp_path):
    first=manager(tmp_path)
    binding=await first.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain'])
    finished=False
    async def probe(): return finished
    async def cleanup(): return None
    first.resource_registry.register_external(OWNER,'job',tool_call_id='call',cleanup=cleanup,probe=probe)
    with pytest.raises(ExceptionGroup): await first.close()
    second=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    finished=True
    assert await first.resource_registry.mark_finished(OWNER,'job')
    await first.close()
    assert binding.workspace.closed==1
    await second.get_workspace(IDENTITY);await second.close()


async def test_different_workspace_identities_remain_independent(tmp_path):
    first,second=manager(tmp_path),manager(tmp_path)
    other=replace(IDENTITY,session_id='other-session',conversation_id='other-conversation')
    one,two=await asyncio.gather(first.get_workspace(IDENTITY),second.get_workspace(other))
    assert one.host_workdir!=two.host_workdir
    await first.close();await second.close()


@pytest.mark.parametrize('state',['pending','cancelled','failed'])
async def test_finished_resource_with_unresolved_launch_cannot_release_workspace(tmp_path,state):
    first=manager(tmp_path)
    await first.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,
        read_model_input_types=['text/plain'])
    first.resource_registry.reserve(OWNER,'launch',kind='process')
    resource=first.resource_registry._resource(OWNER,'launch')
    resource.finished=True
    future=asyncio.get_running_loop().create_future()
    if state=='cancelled': future.cancel()
    elif state=='failed': future.set_exception(RuntimeError('launch outcome unknown'))
    resource.launch=future
    with pytest.raises(ExceptionGroup): await first.close()
    second=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    # Test cleanup models trusted resolution; no production API is bypassed
    # to assert an actual process has died.
    if state=='pending': future.set_result(None)
    elif state=='failed': future.exception()
    resource.launch=None
    await first.close();await second.close()


async def test_parent_directory_fsync_failure_blocks_factory_and_keeps_dirty_latch(tmp_path,monkeypatch):
    from workmate_runtime.assembly import posix_workspace_ownership as workspace_ownership
    calls=[]
    original=workspace_ownership._sync_directory
    def fail_parent(path):
        if path==tmp_path: raise OSError('parent directory fsync failed')
        return original(path)
    def forbidden(**kwargs): calls.append(kwargs)
    monkeypatch.setattr(workspace_ownership,'_sync_directory',fail_parent)
    failed=manager(tmp_path,forbidden)
    with pytest.raises(WorkspaceBindingError): await failed.get_workspace(IDENTITY)
    assert calls==[]
    monkeypatch.setattr(workspace_ownership,'_sync_directory',original)
    second=manager(tmp_path,forbidden)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    assert calls==[]
    await failed.close();await second.close()


async def test_cancelled_initialization_cleanup_reuses_pending_gateway_close(tmp_path):
    entered,release=asyncio.Event(),asyncio.Event()
    instances=[]
    class SlowCleanup(StubWorkspace):
        def __init__(self,**kwargs):
            super().__init__(**kwargs);instances.append(self)
        async def initialize(self): raise RuntimeError('initialization failed')
        async def close(self):
            self.closed+=1
            entered.set()
            await release.wait()
    first=manager(tmp_path,SlowCleanup)
    get=asyncio.create_task(first.get_workspace(IDENTITY))
    await asyncio.wait_for(entered.wait(),2)
    # Cancel the initializer while its shielded gateway close is in progress.
    first._entries[IDENTITY].cancel()
    with pytest.raises(WorkspaceBindingError): await get
    close=asyncio.create_task(first.close())
    await asyncio.sleep(0)
    assert instances[0].closed==1
    second=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await second.get_workspace(IDENTITY)
    release.set()
    await asyncio.wait_for(close,2)
    assert instances[0].closed==1
    await second.get_workspace(IDENTITY);await second.close()


async def test_cleaned_failed_workspace_releases_object_and_lifecycle_markers(tmp_path):
    import gc
    import weakref
    references=[]
    close_counts=[]
    class Workspace:
        # No global object list: only manager ownership may keep these alive.
        def __init__(self,**kwargs):
            self.host_workdir=kwargs['host_workdir']
            self.workspace_id=kwargs['workspace_id']
            self.index=len(references)
            references.append(weakref.ref(self))
            close_counts.append(0)
        async def initialize(self):
            if self.index==0: raise RuntimeError('first initialization fails')
        async def close(self): close_counts[self.index]+=1
    first=manager(tmp_path,Workspace)
    async def attempt():
        try: await first.get_workspace(IDENTITY)
        except RuntimeError: return
        raise AssertionError('first initialization must fail')
    await attempt()
    # Let the completed initialization task and exception traceback disappear.
    await asyncio.sleep(0)
    gc.collect()
    assert references[0]() is None
    assert not first._workspace_objects
    assert not first._closed_workspaces
    assert not first._workspace_close_tasks
    assert not first._workspace_identities
    assert not first._ownership
    assert close_counts==[1]
    second=await first.get_workspace(IDENTITY)
    assert second is references[1]() and second is not references[0]()
    await first.close()
    await first.close()
    assert close_counts==[1,1]
