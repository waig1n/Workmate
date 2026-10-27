"""Session retirement drains only locally held infrastructure and retains content."""
import asyncio
from dataclasses import replace
from pathlib import Path
import pytest
from workmate_runtime.assembly.workspaces import WorkspaceBindingError
from workmate_runtime.execution.resources import ResourceScopeError
from test_worker_workspaces import manager,StubWorkspace,IDENTITY,OWNER


async def test_retirement_missing_local_workspace_is_not_close_proof_or_creation(tmp_path):
    mgr=manager(tmp_path)
    assert await mgr.retire_session(IDENTITY.session_id) is False
    with pytest.raises(WorkspaceBindingError): await mgr.get_workspace(IDENTITY)
    assert not list(tmp_path.glob('*/input'))
    await mgr.close()


async def test_retirement_closes_once_retains_files_and_other_sessions(tmp_path):
    mgr=manager(tmp_path)
    first=await mgr.get_workspace(IDENTITY)
    other_identity=replace(IDENTITY,session_id='other',conversation_id='other-conversation')
    other=await mgr.get_workspace(other_identity)
    content=Path(first.host_workdir)/'output'/'keep.txt';content.write_text('immutable historical content')
    assert await asyncio.gather(*(mgr.retire_session(IDENTITY.session_id) for _ in range(3)))==[True]*3
    assert first.closed==1 and other.closed==0
    assert content.read_text()=='immutable historical content'
    with pytest.raises(WorkspaceBindingError): await mgr.get_workspace(IDENTITY)
    with pytest.raises(WorkspaceBindingError):
        await mgr.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,read_model_input_types=['text/plain'])
    assert await mgr.get_workspace(other_identity) is other
    await mgr.close()
    assert first.closed==other.closed==1


async def test_failed_gateway_retirement_keeps_dirty_lease_until_retry(tmp_path):
    class Flaky(StubWorkspace):
        async def close(self):
            self.closed+=1
            if self.closed==1: raise OSError('gateway still alive')
    mgr=manager(tmp_path,Flaky)
    workspace=await mgr.get_workspace(IDENTITY)
    with pytest.raises(OSError): await mgr.retire_session(IDENTITY.session_id)
    outsider=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await outsider.get_workspace(IDENTITY)
    with pytest.raises(WorkspaceBindingError): await mgr.get_workspace(IDENTITY)
    assert await mgr.retire_session(IDENTITY.session_id) is True
    assert workspace.closed==2
    await outsider.get_workspace(IDENTITY)
    await outsider.close();await mgr.close()


async def test_retirement_cannot_clean_unfinished_resources_and_retry_uses_existing_gateway_close(tmp_path):
    mgr=manager(tmp_path)
    binding=await mgr.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,read_model_input_types=['text/plain'])
    finished=False
    async def probe(): return finished
    async def cleanup(): return None
    mgr.resource_registry.register_external(OWNER,'remote',tool_call_id='call',cleanup=cleanup,probe=probe)
    with pytest.raises(WorkspaceBindingError): await mgr.retire_session(IDENTITY.session_id)
    with pytest.raises(ResourceScopeError): mgr.resource_registry.reserve(OWNER,'new-launch',kind='process')
    outsider=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await outsider.get_workspace(IDENTITY)
    finished=True
    assert await mgr.resource_registry.mark_finished(OWNER,'remote')
    assert await mgr.retire_session(IDENTITY.session_id) is True
    assert binding.workspace.closed==1
    await mgr.close();await outsider.close()


async def test_retirement_joins_inflight_initialization_without_returning_closed_workspace(tmp_path):
    entered,release=asyncio.Event(),asyncio.Event()
    class Slow(StubWorkspace):
        async def initialize(self):
            entered.set();await release.wait();await super().initialize()
    mgr=manager(tmp_path,Slow)
    opening=asyncio.create_task(mgr.get_workspace(IDENTITY))
    await asyncio.wait_for(entered.wait(),2)
    retiring=asyncio.create_task(mgr.retire_session(IDENTITY.session_id))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(WorkspaceBindingError): await asyncio.wait_for(opening,2)
    assert await asyncio.wait_for(retiring,2) is True
    await mgr.close()


async def test_cancelled_retirement_waiter_does_not_duplicate_or_cancel_gateway_close(tmp_path):
    entered,release=asyncio.Event(),asyncio.Event()
    class SlowClose(StubWorkspace):
        async def close(self):
            self.closed+=1;entered.set();await release.wait()
    mgr=manager(tmp_path,SlowClose)
    workspace=await mgr.get_workspace(IDENTITY)
    waiter=asyncio.create_task(mgr.retire_session(IDENTITY.session_id))
    await asyncio.wait_for(entered.wait(),2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    retry=asyncio.create_task(mgr.retire_session(IDENTITY.session_id))
    release.set()
    assert await asyncio.wait_for(retry,2) is True
    assert workspace.closed==1
    await mgr.close()


async def test_retirement_clean_persistence_failure_retains_lease_for_retry(tmp_path,monkeypatch):
    mgr=manager(tmp_path)
    workspace=await mgr.get_workspace(IDENTITY)
    lease=mgr._ownership[IDENTITY]
    original=lease.close
    def unavailable(*,clean): raise OSError('clean record persistence unavailable')
    monkeypatch.setattr(lease,'close',unavailable)
    with pytest.raises(OSError): await mgr.retire_session(IDENTITY.session_id)
    outsider=manager(tmp_path)
    with pytest.raises(WorkspaceBindingError): await outsider.get_workspace(IDENTITY)
    monkeypatch.setattr(lease,'close',original)
    assert await mgr.retire_session(IDENTITY.session_id) is True
    assert workspace.closed==1
    await mgr.close();await outsider.close()


async def test_retirement_and_manager_shutdown_share_single_gateway_close(tmp_path):
    entered,release=asyncio.Event(),asyncio.Event()
    class SlowClose(StubWorkspace):
        async def close(self):
            self.closed+=1;entered.set();await release.wait()
    mgr=manager(tmp_path,SlowClose)
    workspace=await mgr.get_workspace(IDENTITY)
    retirement=asyncio.create_task(mgr.retire_session(IDENTITY.session_id))
    await asyncio.wait_for(entered.wait(),2)
    shutdown=asyncio.create_task(mgr.close())
    await asyncio.sleep(0)
    release.set()
    result=await asyncio.wait_for(asyncio.gather(retirement,shutdown),2)
    assert result[0] is True
    assert workspace.closed==1
    assert await mgr.retire_session(IDENTITY.session_id) is True


async def test_retirement_retries_failed_initialization_cleanup_using_original_object(tmp_path):
    instances = []

    class Leaking(StubWorkspace):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            instances.append(self)

        async def initialize(self):
            raise RuntimeError('partial initialization')

        async def close(self):
            self.closed += 1
            if self.closed == 1:
                raise OSError('partial gateway still alive')

    mgr = manager(tmp_path, Leaking)
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(IDENTITY)
    workspace, = instances
    assert mgr._workspace_objects[id(workspace)] is workspace
    assert workspace.closed == 1 and IDENTITY in mgr._ownership
    outsider = manager(tmp_path)
    with pytest.raises(WorkspaceBindingError):
        await outsider.get_workspace(IDENTITY)

    assert await mgr.retire_session(IDENTITY.session_id) is True
    assert instances == [workspace] and workspace.closed == 2
    assert not mgr._workspace_objects and not mgr._workspace_identities
    assert not mgr._workspace_close_tasks and not mgr._closed_workspaces
    assert not mgr._ownership and not mgr._entries
    assert await mgr.retire_session(IDENTITY.session_id) is True
    await mgr.close()
    assert workspace.closed == 2
    await outsider.get_workspace(IDENTITY)
    await outsider.close()
