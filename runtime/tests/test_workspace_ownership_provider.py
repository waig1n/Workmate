"""Workspace manager depends on a provider contract, not POSIX flock machinery."""
from pathlib import Path
import os
import subprocess
import sys

import pytest
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager,WorkspaceIdentity,WorkspaceBindingError
from workmate_runtime.assembly.workspace_ownership import WorkspaceOwnershipError
from workmate_runtime.execution.resources import TaskResourceRegistry


IDENTITY=WorkspaceIdentity('user','agent','conversation','session','node')


class Lease:
    def __init__(self,path,events):
        self.path=path
        self.generation='provider-generation'
        self.events=events
        path.mkdir(parents=True)
    def close(self,*,clean):
        assert clean is True
        self.events.append('release')


class Provider:
    def __init__(self,events): self.events=events;self.calls=[]
    def acquire(self,basedir,workspace_id,identity):
        self.events.append('acquire')
        self.calls.append((basedir,workspace_id,identity))
        return Lease(basedir/workspace_id,self.events)


def make_manager(root,provider,events,*,fail_close=False):
    class Workspace:
        def __init__(self,**kwargs):
            events.append('factory')
            self.host_workdir=kwargs['host_workdir']
        async def initialize(self): events.append('initialize')
        async def close(self):
            events.append('gateway-close')
            if fail_close: raise OSError('gateway still alive')
    return WorkerWorkspaceManager(root,storage_node_id='node',resource_registry=TaskResourceRegistry(),
        ownership_provider=provider,workspace_factory=Workspace)


async def test_provider_acquires_before_factory_and_releases_after_gateway(tmp_path):
    events=[];provider=Provider(events)
    manager=make_manager(tmp_path,provider,events)
    workspace=await manager.get_workspace(IDENTITY)
    assert events==['acquire','factory','initialize']
    root,workspace_id,identity=provider.calls[0]
    assert root==tmp_path
    assert identity==vars(IDENTITY)
    assert workspace.host_workdir==str(tmp_path/workspace_id)
    await manager.close()
    assert events==['acquire','factory','initialize','gateway-close','release']


async def test_provider_rejection_prevents_gateway_factory(tmp_path):
    events=[]
    class Reject:
        def acquire(self,*args):
            events.append('acquire')
            raise WorkspaceOwnershipError('provider denies ownership')
    manager=make_manager(tmp_path,Reject(),events)
    with pytest.raises(WorkspaceBindingError): await manager.get_workspace(IDENTITY)
    assert events==['acquire']
    await manager.close()
    assert events==['acquire']


async def test_gateway_cleanup_failure_never_releases_provider_lease(tmp_path):
    events=[];provider=Provider(events)
    manager=make_manager(tmp_path,provider,events,fail_close=True)
    await manager.get_workspace(IDENTITY)
    with pytest.raises(ExceptionGroup): await manager.close()
    assert events==['acquire','factory','initialize','gateway-close']


def test_manager_requires_explicit_ownership_provider(tmp_path):
    with pytest.raises(TypeError,match='ownership_provider'):
        WorkerWorkspaceManager(tmp_path,storage_node_id='node',resource_registry=TaskResourceRegistry())


def test_provider_contract_and_manager_run_without_importing_posix_lock_implementation(tmp_path):
    script='''
import importlib.abc,sys,asyncio
from pathlib import Path
class RejectPosix(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='fcntl' or fullname.endswith('.posix_workspace_ownership'):
            raise AssertionError('POSIX implementation imported: '+fullname)
sys.meta_path.insert(0,RejectPosix())
from workmate_runtime.assembly.workspace_ownership import WorkspaceOwnershipProvider,WorkspaceOwnershipLease
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager,WorkspaceIdentity
from workmate_runtime.execution.resources import TaskResourceRegistry
class Lease:
    generation='generation'
    def __init__(self,path): self.path=path;path.mkdir()
    def close(self,*,clean): assert clean is True
class Provider:
    def acquire(self,basedir,workspace_id,identity): return Lease(basedir/workspace_id)
class Workspace:
    def __init__(self,**kwargs): pass
    async def initialize(self): pass
    async def close(self): pass
async def main():
    manager=WorkerWorkspaceManager(Path(sys.argv[1]),storage_node_id='node',resource_registry=TaskResourceRegistry(),
        ownership_provider=Provider(),workspace_factory=Workspace)
    await manager.get_workspace(WorkspaceIdentity('u','a','c','s','node'))
    await manager.close()
asyncio.run(main())
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path)],capture_output=True,text=True,timeout=15,
        env={**os.environ,'COVERAGE_PROCESS_START':''})
    assert result.returncode==0,result.stderr


async def test_compose_worker_default_provider_enforces_real_workspace_exclusion(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import event
    from workmate_runtime.execution.composition import compose_worker
    from workmate_runtime.config import RuntimeSettings
    from test_worker_workspaces import StubWorkspace
    engine=create_async_engine('postgresql+asyncpg://fixture@localhost/fixture')
    @event.listens_for(engine.sync_engine,'do_connect')
    def reject_connection(*args): raise AssertionError('composition test must not connect to PostgreSQL')
    apps=[]
    try:
        for _ in range(2):
            apps.append(compose_worker(engine,RuntimeSettings(),storage_node_id='node',
                workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
                max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace))
        original=await apps[0].workspaces.get_workspace(IDENTITY)
        with pytest.raises(WorkspaceBindingError): await apps[1].workspaces.get_workspace(IDENTITY)
        await apps[0].workspaces.close()
        successor=await apps[1].workspaces.get_workspace(IDENTITY)
        assert successor.host_workdir==original.host_workdir
    finally:
        for app in apps: await app.workspaces.close()
        await engine.dispose()


async def test_compose_worker_passes_explicit_provider_to_workspace_manager(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import event
    from workmate_runtime.execution.composition import compose_worker
    from workmate_runtime.config import RuntimeSettings
    from test_worker_workspaces import StubWorkspace
    engine=create_async_engine('postgresql+asyncpg://fixture@localhost/fixture')
    @event.listens_for(engine.sync_engine,'do_connect')
    def reject_connection(*args): raise AssertionError('composition test must not connect to PostgreSQL')
    events=[];provider=Provider(events)
    app=None
    try:
        app=compose_worker(engine,RuntimeSettings(),storage_node_id='node',
            workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
            max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace,ownership_provider=provider)
        workspace=await app.workspaces.get_workspace(IDENTITY)
        assert len(provider.calls)==1
        assert provider.calls[0][2]==vars(IDENTITY)
        assert events==['acquire']
        await app.workspaces.close()
        assert workspace.closed==1
        assert events==['acquire','release']
    finally:
        if app is not None: await app.workspaces.close()
        await engine.dispose()
