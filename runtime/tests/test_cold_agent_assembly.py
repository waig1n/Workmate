"""Cold assembly rebinds compute without replaying original admission inputs."""
from dataclasses import replace
import pytest
from agentscope.state import AgentState
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.assembly.agent import AgentAssemblyError
from test_worker_agent_assembly import assembly, OWNER, LocalGuard


async def test_cold_resume_requires_verified_bridge_before_allocating_workspace(assembly, monkeypatch):
    assembler, _, workspaces = assembly
    async def no_bridge(*args):
        return False
    monkeypatch.setattr(assembler, '_cold_recovery', no_bridge)
    owner = replace(OWNER, worker_id='replacement', execution_epoch=2)
    snapshot = CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))
    with pytest.raises(AgentAssemblyError):
        await assembler.prepare_resume(owner, snapshot, LocalGuard(owner))
    assert not workspaces._bindings


async def test_cold_resume_preserves_snapshot_and_rebinds_delivery_without_baseline(assembly, monkeypatch):
    assembler, fixture, workspaces = assembly
    async def bridge(*args):
        from workmate_runtime.assembly.workspaces import WorkspaceIdentity
        identity = WorkspaceIdentity('user','agent','conversation','session','node')
        await workspaces.get_workspace(identity)
        return {'resource_generation':workspaces._ownership[identity].generation}
    monkeypatch.setattr(assembler, '_cold_recovery', bridge)
    calls = []
    class Delivery:
        async def rebind(self, owner, binding):
            calls.append(('rebind', owner))
        async def prepare(self, *args):
            raise AssertionError('Cold Resume must not capture a new baseline')
    assembler.delivery_coordinator = Delivery()
    owner = replace(OWNER, worker_id='replacement', execution_epoch=2)
    snapshot = CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))
    engine = await assembler.prepare_resume(owner, snapshot, LocalGuard(owner))
    assert engine.snapshot() == snapshot
    assert owner in workspaces._bindings
    assert calls == [('rebind', owner)]


async def test_cold_stop_rejects_unproven_claim_before_loading_credentials(assembly, monkeypatch):
    from workmate_runtime.execution.stopping import StopGuard
    assembler, fixture, workspaces = assembly
    async def no_bridge(*args):
        return False
    monkeypatch.setattr(assembler, '_cold_recovery', no_bridge)
    owner = replace(OWNER, worker_id='replacement', execution_epoch=2)
    snapshot = CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))
    with pytest.raises(AgentAssemblyError):
        await assembler.prepare_cleanup(owner,snapshot,StopGuard(None,owner,'stop'))
    assert not fixture['models'] and not workspaces._bindings


async def test_cold_resume_rejects_replaced_workspace_generation(assembly, monkeypatch):
    assembler, _, workspaces = assembly
    async def stale_generation(*args):
        return {'resource_generation':'another-workspace-generation'}
    monkeypatch.setattr(assembler,'_cold_recovery',stale_generation)
    owner=replace(OWNER,worker_id='replacement',execution_epoch=2)
    snapshot=CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))
    with pytest.raises(AgentAssemblyError,match='generation'):
        await assembler.prepare_resume(owner,snapshot,LocalGuard(owner))
    assert owner not in assembler._bound
    assert owner in workspaces._bindings  # The failure path retains evidence for cleanup.
