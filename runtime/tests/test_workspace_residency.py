"""Workspace resource ownership brackets the durable compute residence receipt."""
from dataclasses import replace
import pytest
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager, WorkspaceBindingError
from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.execution.resources import TaskResourceRegistry
from test_worker_workspaces import IDENTITY, OWNER, StubWorkspace
from test_workspace_lifecycle import bind


class Residency:
    def __init__(self):
        self.events=[];self.allow=True;self.fail_finish=False
    async def record_held(self,identity,owner,generation):
        self.events.append(('held',owner))
        return dict(hold_version=1,workspace_generation=generation,holder=owner.worker_id)
    async def release_owned(self,identity,owner,hold):
        self.events.append(('released',owner,hold['hold_version']))
        assert identity not in self.manager._ownership
        if self.fail_finish: raise ConnectionError('receipt unavailable')
    async def begin_idle_release(self,identity,hold):
        self.events.append(('begin',hold['hold_version']))
        return dict(identity=identity,hold=hold) if self.allow else None
    async def finish_idle_release(self,claim):
        self.events.append(('finished',claim['hold']['hold_version']))
        assert claim['identity'] not in self.manager._ownership
        if self.fail_finish: raise ConnectionError('receipt unavailable')
    async def block_idle_release(self,claim,error): self.events.append(('blocked',error))


def manager(tmp_path,residency,factory=StubWorkspace):
    mgr=WorkerWorkspaceManager(tmp_path,storage_node_id='node',resource_registry=TaskResourceRegistry(),
        ownership_provider=PosixWorkspaceOwnership(),workspace_factory=factory,residency=residency)
    residency.manager=mgr
    return mgr


async def test_hold_recorded_after_flock_before_compute_initialization(tmp_path):
    residency=Residency()
    class Workspace(StubWorkspace):
        async def initialize(self):
            assert residency.events==[('held',OWNER)]
            assert IDENTITY in residency.manager._ownership
            await super().initialize()
    mgr=manager(tmp_path,residency,Workspace)
    await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.close_task_workspace(OWNER)
    assert residency.events[-1]==('released',OWNER,1)
    await mgr.release_task(OWNER)
    assert not mgr._holds and not mgr._workspace_objects
    await mgr.close()


async def test_idle_cas_decline_never_closes_warm_compute(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.release_task(OWNER)
    residency.allow=False
    await mgr.sweep_idle(ttl=0,capacity=0)
    assert not binding.workspace.closed and IDENTITY in mgr._ownership
    residency.allow=True
    await mgr.sweep_idle(ttl=0,capacity=0)
    assert binding.workspace.closed==1
    assert residency.events[-1]==('finished',1)
    assert not mgr._holds and not mgr._workspace_objects
    await mgr.close()


async def test_receipt_failure_retains_closed_object_and_hold_until_retry(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.release_task(OWNER);residency.fail_finish=True
    await mgr.sweep_idle(ttl=0,capacity=0)
    assert binding.workspace.closed==1 and mgr._holds and mgr._workspace_objects
    assert residency.events[-1][0]=='blocked'
    with pytest.raises(WorkspaceBindingError):
        await bind(mgr,replace(OWNER,task_id='next',execution_epoch=2))
    residency.fail_finish=False
    await mgr.close()
    assert not mgr._holds


async def test_old_finalization_cannot_mark_new_task_idle_or_release_hold(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    first=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    second=await bind(mgr,next_owner)
    await mgr.release_task(OWNER)
    assert first.workspace is second.workspace and IDENTITY not in mgr._idle
    assert residency.events==[('held',OWNER),('held',next_owner)] and mgr._holds
    mgr.resource_registry._scope(next_owner).sealed=True
    await mgr.close_task_workspace(next_owner)
    assert residency.events[-1]==('released',next_owner,1)
    await mgr.release_task(next_owner);await mgr.close()


async def test_clean_initialization_failure_drops_heavy_references(tmp_path):
    residency=Residency()
    class Failed(StubWorkspace):
        async def initialize(self): raise RuntimeError('initialize failed')
    mgr=manager(tmp_path,residency,Failed)
    with pytest.raises(RuntimeError): await bind(mgr)
    assert not mgr._ownership and not mgr._holds and not mgr._workspace_objects
    assert not mgr._workspace_identities and not mgr._workspace_close_tasks
    await mgr.close()


async def test_registration_failure_releases_flock_without_creating_compute(tmp_path):
    residency=Residency();made=[]
    async def failed(*args): raise ConnectionError('PG unavailable')
    residency.record_held=failed
    class Workspace(StubWorkspace):
        def __init__(self,**kwargs): made.append(True);super().__init__(**kwargs)
    mgr=manager(tmp_path,residency,Workspace)
    with pytest.raises(ConnectionError): await bind(mgr)
    assert not made and not mgr._ownership and not mgr._holds
    await mgr.close()


async def test_prepared_unbound_workspace_receipt_and_references_are_released(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    workspace=await mgr.get_workspace(IDENTITY,owner=OWNER)
    assert not mgr._bindings
    await mgr.close_prepared_workspace(OWNER)
    assert workspace.closed==1 and residency.events[-1]==('released',OWNER,1)
    assert not mgr._holds and not mgr._workspace_objects and not mgr._entries
    await mgr.close()


async def test_retire_releases_residence_and_closed_backend_references(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.release_task(OWNER)
    assert await mgr.retire_session(OWNER.session_id)
    assert binding.workspace.closed==1
    assert not mgr._holds and not mgr._workspace_objects and not mgr._entries
    assert await mgr.retire_session(OWNER.session_id)
    await mgr.close()


async def test_failed_compute_close_retains_hold_flock_and_original_object(tmp_path):
    residency=Residency()
    class Failed(StubWorkspace):
        async def close(self):
            if not getattr(self,'allow_close',False): raise RuntimeError('unproven close')
            await super().close()
    mgr=manager(tmp_path,residency,Failed)
    binding=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    with pytest.raises(RuntimeError): await mgr.close_task_workspace(OWNER)
    assert mgr._holds and mgr._ownership and mgr._workspace_objects
    assert residency.events==[('held',OWNER)]
    binding.workspace.allow_close=True
    await mgr.close_task_workspace(OWNER);await mgr.release_task(OWNER);await mgr.close()


async def test_typed_release_rejects_old_hold_and_active_task(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await bind(mgr);hold=mgr._holds[IDENTITY]
    assert not await mgr.release_idle_session(OWNER.session_id,1,hold['workspace_generation'])
    mgr.resource_registry._scope(OWNER).sealed=True;await mgr.release_task(OWNER)
    assert not await mgr.release_idle_session(OWNER.session_id,2,hold['workspace_generation'])
    assert not await mgr.release_idle_session(OWNER.session_id,1,'stale-generation')
    assert binding.workspace.closed==0
    assert await mgr.release_idle_session(OWNER.session_id,1,hold['workspace_generation'])
    assert not await mgr.release_idle_session(OWNER.session_id,1,hold['workspace_generation'])
    assert binding.workspace.closed==1 and not mgr.affinity_holds()
    await mgr.close()


async def test_affinity_hints_are_copies_only_for_initialized_live_workspace(tmp_path):
    import asyncio
    entered=asyncio.Event();release=asyncio.Event();residency=Residency()
    class Slow(StubWorkspace):
        async def initialize(self):
            entered.set();await release.wait();await super().initialize()
    mgr=manager(tmp_path,residency,Slow)
    initializing=asyncio.create_task(bind(mgr));await entered.wait()
    assert mgr._holds and mgr.affinity_holds()==()
    release.set();await initializing
    hints=mgr.affinity_holds()
    assert len(hints)==1 and hints[0]['hold_version']==1
    hints[0]['hold_version']=999
    assert mgr._holds[IDENTITY]['hold_version']==1
    mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.close_task_workspace(OWNER)
    assert mgr.affinity_holds()==()
    await mgr.release_task(OWNER);await mgr.close()


async def test_terminal_close_without_receipt_cannot_certify_retirement(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    residency.fail_finish=True
    with pytest.raises(ConnectionError): await mgr.close_task_workspace(OWNER)
    residency.allow=False
    assert not await mgr.retire_session(OWNER.session_id)
    assert mgr._holds
    residency.fail_finish=False;residency.allow=True
    await mgr.close()


async def test_permission_pause_receipt_precedes_forgetting_task_binding(tmp_path):
    from test_core_engine import park
    from test_parked_workspace_lifecycle import bind as parked_bind,OWNER as parked_owner
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await parked_bind(mgr)
    original=residency.release_owned
    async def release(identity,owner,hold):
        assert owner in mgr._bindings and mgr.resource_registry._scope(owner).sealed
        await original(identity,owner,hold)
    residency.release_owned=release
    engine,_=await park()
    await mgr.suspend_task_workspace(parked_owner,engine.snapshot(),agent_name='test')
    assert binding.workspace.closed==1 and residency.events[-1]==('released',parked_owner,1)
    assert not mgr._holds and not mgr._bindings and not mgr._workspace_objects
    await mgr.close()


async def test_delayed_unbound_preparation_cleanup_cannot_close_new_task_workspace(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    binding=await bind(mgr,next_owner)
    with pytest.raises(WorkspaceBindingError): await mgr.close_prepared_workspace(OWNER)
    assert binding.workspace.closed==0 and next_owner in mgr._bindings
    mgr.resource_registry._scope(next_owner).sealed=True
    await mgr.close_task_workspace(next_owner);await mgr.release_task(next_owner);await mgr.close()


async def test_shutdown_snapshot_survives_concurrent_retirement_forgetting_objects(tmp_path):
    import asyncio
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.release_task(OWNER)
    entered=asyncio.Event();release=asyncio.Event();original=mgr._close_all
    async def delayed(pending):
        entered.set();await release.wait();await original(pending)
    mgr._close_all=delayed
    closing=asyncio.create_task(mgr.close());await entered.wait()
    assert await mgr.retire_session(OWNER.session_id)
    release.set();await closing
    assert binding.workspace.closed==1 and not mgr._holds


async def test_warm_next_task_refreshes_durable_owner_before_scope_is_exposed(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    first=await bind(mgr);original_hold=dict(mgr._holds[IDENTITY])
    mgr.resource_registry._scope(OWNER).sealed=True;await mgr.release_task(OWNER)
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    original=residency.record_held
    async def refresh(identity,owner,generation):
        assert mgr._lock.locked() and identity not in mgr._idle
        assert next_owner not in mgr._bindings and next_owner not in mgr.resource_registry._scopes
        hold=await original(identity,owner,generation)
        return {**hold,'last_task_id':owner.task_id,'last_epoch':owner.execution_epoch}
    residency.record_held=refresh
    second=await bind(mgr,next_owner)
    assert second.workspace is first.workspace
    assert residency.events==[('held',OWNER),('held',next_owner)]
    assert mgr._holds[IDENTITY]['last_task_id']=='next' and mgr._holds[IDENTITY]['last_epoch']==2
    assert mgr._holds[IDENTITY]['hold_version']==original_hold['hold_version']
    mgr.resource_registry._scope(next_owner).sealed=True
    await mgr.close_task_workspace(next_owner);await mgr.release_task(next_owner);await mgr.close()


async def test_warm_refresh_serializes_idle_release_until_new_task_binding(tmp_path):
    import asyncio
    residency=Residency();mgr=manager(tmp_path,residency)
    binding=await bind(mgr);hold=dict(mgr._holds[IDENTITY])
    mgr.resource_registry._scope(OWNER).sealed=True;await mgr.release_task(OWNER)
    entered=asyncio.Event();allow=asyncio.Event();original=residency.record_held
    async def refresh(*args): entered.set();await allow.wait();return await original(*args)
    residency.record_held=refresh
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    rebinding=asyncio.create_task(bind(mgr,next_owner));await entered.wait()
    releasing=asyncio.create_task(mgr.release_idle_session(OWNER.session_id,hold['hold_version'],hold['workspace_generation']))
    await asyncio.sleep(0)
    assert not releasing.done() and mgr._lock.locked()
    allow.set();await rebinding
    assert not await releasing and binding.workspace.closed==0
    mgr.resource_registry._scope(next_owner).sealed=True
    await mgr.close_task_workspace(next_owner);await mgr.release_task(next_owner);await mgr.close()


async def test_failed_warm_refresh_never_exposes_a_new_task_scope(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True;await mgr.release_task(OWNER)
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    async def unavailable(*args): raise ConnectionError('ambiguous refresh response')
    residency.record_held=unavailable
    with pytest.raises(ConnectionError): await bind(mgr,next_owner)
    assert next_owner not in mgr._bindings and next_owner not in mgr.resource_registry._scopes
    assert mgr._holds and mgr._ownership and not mgr.affinity_holds()
    await mgr.close()


async def test_new_task_prebind_failure_refreshes_warm_owner_before_cleanup(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    first=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.release_task(OWNER)
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    assert mgr._residence_owners[IDENTITY]==OWNER and not mgr._bindings
    await mgr.close_prepared_workspace(next_owner)
    assert residency.events[-2:]==[('held',next_owner),('released',next_owner,1)]
    assert first.workspace.closed==1 and not mgr._holds and not mgr._ownership
    await mgr.close()


async def test_old_prebind_cleanup_requires_pg_owner_even_when_local_hold_still_old(tmp_path):
    residency=Residency();mgr=manager(tmp_path,residency)
    first=await bind(mgr);mgr.resource_registry._scope(OWNER).sealed=True
    await mgr.release_task(OWNER)
    next_owner=replace(OWNER,task_id='next',execution_epoch=2)
    original=residency.record_held
    async def exact_owner(identity,owner,generation):
        if owner!=next_owner: raise WorkspaceBindingError('PG execution owner changed')
        return await original(identity,owner,generation)
    residency.record_held=exact_owner
    with pytest.raises(WorkspaceBindingError,match='PG execution owner changed'):
        await mgr.close_prepared_workspace(OWNER)
    assert first.workspace.closed==0 and mgr._holds and mgr._ownership
    await mgr.close_prepared_workspace(next_owner)
    assert first.workspace.closed==1 and not mgr._holds
    await mgr.close()
