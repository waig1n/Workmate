"""Local pause primitive preserves working files and fails closed on cleanup."""
from dataclasses import replace
from pathlib import Path

import pytest

from test_core_engine import park
from test_worker_workspaces import manager, IDENTITY, OWNER, StubWorkspace
from workmate_runtime.assembly.workspaces import WorkspaceBindingError
from workmate_runtime.execution.resources import ResourceScopeError

IDENTITY = replace(IDENTITY, session_id='session-1')
OWNER = replace(OWNER, session_id='session-1')


async def bind(mgr, owner=OWNER):
    return await mgr.bind_task(IDENTITY, owner, fresh_claim=True,
        all_resource_paths_tracked=True, read_model_input_types=['text/plain'])


async def test_pause_closes_compute_preserves_files_and_allows_new_owner(tmp_path):
    mgr = manager(tmp_path)
    old = await bind(mgr)
    path = Path(old.workspace.host_workdir) / 'working' / 'keep.txt'
    path.write_text('durable working state')
    engine, _ = await park()
    snapshot = engine.snapshot()
    before = snapshot.model_dump_json()
    await mgr.suspend_task_workspace(OWNER, snapshot, agent_name='test')
    assert old.workspace.closed == 1
    assert snapshot.model_dump_json() == before
    assert OWNER not in mgr._bindings
    assert IDENTITY not in mgr._ownership
    assert IDENTITY not in mgr._entries
    with pytest.raises(ResourceScopeError):
        old.offloader.get_backend()
    with pytest.raises(WorkspaceBindingError):
        await mgr.bind_task(IDENTITY, OWNER, fresh_claim=False,
            all_resource_paths_tracked=True, read_model_input_types=['text/plain'])
    peer = manager(tmp_path)
    fresh = await bind(peer, replace(OWNER, worker_id='peer', execution_epoch=2))
    assert fresh.workspace is not old.workspace
    assert path.read_text() == 'durable working state'
    await peer.close()
    await mgr.close()


async def test_unknown_owner_does_not_claim_success_or_close_workspace(tmp_path):
    mgr = manager(tmp_path)
    old = await bind(mgr)
    engine, _ = await park()
    with pytest.raises(WorkspaceBindingError):
        await mgr.suspend_task_workspace(replace(OWNER, worker_id='foreign'),
            engine.snapshot(), agent_name='test')
    assert old.workspace.closed == 0
    assert IDENTITY in mgr._ownership
    await mgr.close()


async def test_invalid_snapshot_does_not_seal_or_close(tmp_path):
    mgr = manager(tmp_path)
    old = await bind(mgr)
    with pytest.raises(WorkspaceBindingError):
        await mgr.suspend_task_workspace(OWNER, None, agent_name='test')
    assert not mgr.resource_registry._scope(OWNER).sealed
    assert old.workspace.closed == 0
    await mgr.close()


async def test_close_failure_keeps_binding_and_ownership_until_retry(tmp_path):
    class Failing(StubWorkspace):
        async def close(self):
            if not getattr(self, 'allow_close', False):
                raise RuntimeError('close failed')
            await super().close()
    mgr = manager(tmp_path, Failing)
    old = await bind(mgr)
    engine, _ = await park()
    with pytest.raises(RuntimeError, match='close failed'):
        await mgr.suspend_task_workspace(OWNER, engine.snapshot(), agent_name='test')
    assert OWNER in mgr._bindings and IDENTITY in mgr._ownership
    assert mgr.resource_registry._scope(OWNER).sealed
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(IDENTITY)
    old.workspace.allow_close = True
    await mgr.suspend_task_workspace(OWNER, engine.snapshot(), agent_name='test')
    assert OWNER not in mgr._bindings and IDENTITY not in mgr._ownership
    await mgr.close()


async def test_unresolved_launch_prevents_workspace_release(tmp_path):
    mgr = manager(tmp_path)
    old = await bind(mgr)
    mgr.resource_registry.reserve(OWNER, 'unknown-launch', kind='process')
    engine, _ = await park()
    with pytest.raises(WorkspaceBindingError, match='not safely releasable'):
        await mgr.suspend_task_workspace(OWNER, engine.snapshot(), agent_name='test')
    assert old.workspace.closed == 0
    assert OWNER in mgr._bindings and IDENTITY in mgr._ownership
    assert mgr.resource_registry._scope(OWNER).sealed
    # Shutdown must not manufacture a clean handoff for an unknown launch.
    with pytest.raises(ExceptionGroup):
        await mgr.close()


async def test_cancelled_close_retains_ownership_and_retry_joins_same_close(tmp_path):
    import asyncio
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = []
    class Slow(StubWorkspace):
        async def close(self):
            calls.append(1)
            entered.set()
            await proceed.wait()
            await super().close()
    mgr = manager(tmp_path, Slow)
    old = await bind(mgr)
    engine, _ = await park()
    operation = asyncio.create_task(mgr.suspend_task_workspace(
        OWNER, engine.snapshot(), agent_name='test'))
    await asyncio.wait_for(entered.wait(), 2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert OWNER in mgr._bindings and IDENTITY in mgr._ownership
    assert mgr.resource_registry._scope(OWNER).sealed
    with pytest.raises(WorkspaceBindingError):
        await mgr.get_workspace(IDENTITY)
    proceed.set()
    await mgr.suspend_task_workspace(OWNER, engine.snapshot(), agent_name='test')
    assert calls == [1] and old.workspace.closed == 1
    assert OWNER not in mgr._bindings and IDENTITY not in mgr._ownership
    await mgr.close()


async def test_unsupported_provider_keeps_pinned_binding(tmp_path):
    from types import SimpleNamespace
    mgr = manager(tmp_path)
    old = await bind(mgr)
    engine, _ = await park()
    provider = mgr.provider
    mgr.provider = SimpleNamespace(name='e2b')
    try:
        with pytest.raises(WorkspaceBindingError):
            await mgr.suspend_task_workspace(OWNER, engine.snapshot(), agent_name='test')
        assert not mgr.resource_registry._scope(OWNER).sealed
        assert old.workspace.closed == 0
    finally:
        mgr.provider = provider
        await mgr.close()


async def test_checkpoint_runs_after_close_before_ownership_release(tmp_path):
    mgr=manager(tmp_path)
    bound=await bind(mgr)
    engine,_=await park()
    observed=[]
    async def capture(owner, root, *, boundary):
        assert owner==OWNER and boundary=='approval'
        assert root==bound.workspace.host_workdir
        assert bound.workspace.closed==1
        assert IDENTITY in mgr._ownership and OWNER in mgr._bindings
        observed.append(root)
    await mgr.suspend_task_workspace(OWNER,engine.snapshot(),agent_name='test',checkpoint=capture)
    assert observed==[bound.workspace.host_workdir]
    assert IDENTITY not in mgr._ownership
    await mgr.close()


@pytest.mark.parametrize('failed', [False, True])
async def test_remote_pause_captures_before_compute_close_and_preserves_failure(tmp_path, failed):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    mgr = manager(tmp_path)
    bound = await bind(mgr)
    original = mgr.provider
    proof = {'provider':'docker', 'marker':'fixture'}
    mgr.provider = SimpleNamespace(name='docker', capture_persistent_storage=AsyncMock(return_value=proof),
        close_workspace=original.close_workspace, verify_persistent_storage=AsyncMock())
    engine, _ = await park()
    calls = []
    async def capture(owner, workspace, *, boundary):
        assert workspace is bound.workspace and workspace.closed == 0
        assert owner == OWNER and boundary == 'approval'
        calls.append('capture')
        if failed: raise RuntimeError('checkpoint unavailable')
        return 'committed-checkpoint'
    try:
        if failed:
            with pytest.raises(RuntimeError, match='checkpoint unavailable'):
                await mgr.suspend_task_workspace(OWNER,engine.snapshot(),agent_name='test',checkpoint=capture)
            assert IDENTITY in mgr._ownership and OWNER in mgr._bindings
            assert bound.workspace.closed == 0
        else:
            result = await mgr.suspend_task_workspace(OWNER,engine.snapshot(),agent_name='test',checkpoint=capture)
            assert result == proof and bound.workspace.closed == 1
            assert IDENTITY not in mgr._ownership
        assert calls == ['capture']
    finally:
        mgr.provider = original
        await mgr.close()


async def test_cold_expected_storage_is_verified_even_for_cached_workspace(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    mgr = manager(tmp_path)
    bound = await bind(mgr)
    original = mgr.provider
    verifier = AsyncMock(side_effect=WorkspaceBindingError('persistent files replaced'))
    mgr.provider = SimpleNamespace(name='docker', verify_persistent_storage=verifier)
    proof = {'provider':'docker', 'fixture':'stored-proof'}
    try:
        with pytest.raises(WorkspaceBindingError, match='persistent files replaced'):
            await mgr.get_workspace(IDENTITY, expected_storage_proof=proof)
        verifier.assert_awaited_once_with(bound.workspace, proof)
    finally:
        mgr.provider = original
        await mgr.close()
