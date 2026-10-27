"""Idle eviction must preserve files and never reclaim an active Task."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from test_worker_workspaces import manager, IDENTITY, OWNER, StubWorkspace
from workmate_runtime.assembly.workspaces import WorkspaceBindingError


async def bind(mgr, owner=OWNER):
    return await mgr.bind_task(IDENTITY, owner, fresh_claim=True,
        all_resource_paths_tracked=True, read_model_input_types=['text/plain'])


async def test_release_reclaims_task_references_and_eviction_preserves_files(tmp_path):
    mgr = manager(tmp_path)
    first = await bind(mgr)
    path = Path(first.workspace.host_workdir) / 'working' / 'keep.txt'
    path.write_text('keep')
    with pytest.raises(WorkspaceBindingError):
        await mgr.release_task(OWNER)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    await mgr.release_task(OWNER)
    assert OWNER not in mgr._bindings
    assert OWNER not in mgr.resource_registry._scopes
    await mgr.sweep_idle(ttl=0, capacity=16)
    assert first.workspace.closed == 1
    second = await bind(mgr, replace(OWNER, task_id='next', execution_epoch=2))
    assert second.workspace is not first.workspace
    assert path.read_text() == 'keep'
    await mgr.close()


async def test_live_or_parked_task_is_never_evicted(tmp_path):
    mgr = manager(tmp_path)
    first = await bind(mgr)
    await mgr.sweep_idle(ttl=0, capacity=0)
    assert first.workspace.closed == 0
    await mgr.close()


async def test_suspend_failure_keeps_ownership_and_blocks_reuse(tmp_path):
    class Failing(StubWorkspace):
        async def close(self):
            if not getattr(self, 'allow_close', False):
                raise RuntimeError('close failed')
            await super().close()
    mgr = manager(tmp_path, Failing)
    first = await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    await mgr.sweep_idle(ttl=0, capacity=0)
    assert IDENTITY in mgr._ownership
    with pytest.raises(WorkspaceBindingError):
        await bind(mgr, replace(OWNER, task_id='next', execution_epoch=2))
    first.workspace.allow_close = True
    await mgr.close()


async def test_start_waits_for_inflight_eviction(tmp_path):
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Slow(StubWorkspace):
        async def close(self):
            entered.set()
            await proceed.wait()
            await super().close()
    mgr = manager(tmp_path, Slow)
    first = await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    sweep = asyncio.create_task(mgr.sweep_idle(ttl=0, capacity=0))
    await entered.wait()
    start = asyncio.create_task(bind(mgr, replace(OWNER, task_id='next', execution_epoch=2)))
    await asyncio.sleep(0)
    assert not start.done()
    proceed.set()
    await sweep
    second = await start
    assert second.workspace is not first.workspace
    await mgr.close()


async def test_capacity_evicts_oldest_and_retirement_accepts_closed_workspace(tmp_path):
    mgr = manager(tmp_path)
    first = await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    other_identity = replace(IDENTITY, session_id='other', conversation_id='other')
    other_owner = replace(OWNER, session_id='other', task_id='other')
    second = await mgr.bind_task(other_identity, other_owner, fresh_claim=True,
        all_resource_paths_tracked=True, read_model_input_types=['text/plain'])
    mgr.resource_registry._scope(other_owner).sealed = True
    await mgr.release_task(other_owner)
    await mgr.sweep_idle(ttl=86400, capacity=1)
    assert first.workspace.closed == 1 and second.workspace.closed == 0
    assert await mgr.retire_session(IDENTITY.session_id) is True
    await mgr.close()


@pytest.mark.parametrize('provider', ['local', 'e2b', 'daytona', 'opensandbox'])
async def test_other_providers_do_not_participate_in_idle_eviction(tmp_path, provider):
    mgr = manager(tmp_path)
    first = await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    mgr.provider.name = provider
    await mgr.sweep_idle(ttl=0, capacity=0)
    assert first.workspace.closed == 0
    await mgr.close()


async def test_many_tasks_release_all_backend_process_references(tmp_path):
    mgr = manager(tmp_path)
    for index in range(25):
        owner = replace(OWNER, task_id=str(index), execution_epoch=index+1)
        binding = await bind(mgr, owner)
        scope = mgr.resource_registry._scope(owner)
        scope.sealed = True
        mgr.resource_registry._processes[object()] = (owner, 'finished')
        await mgr.release_task(owner)
        with pytest.raises(Exception, match='Unknown or sealed resource scope'):
            binding.offloader.get_backend()
        assert not mgr._bindings and not mgr.resource_registry._scopes
        assert not mgr.resource_registry._processes
    await mgr.close()


@pytest.mark.parametrize('failure', [False, True])
async def test_finalization_releases_only_after_delivery_success(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from sqlalchemy.ext.asyncio import create_async_engine
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.execution.composition import compose_worker
    engine = create_async_engine('postgresql+asyncpg://unused:unused@127.0.0.1/unused')
    app = compose_worker(engine, RuntimeSettings(reme_enabled=False, skill_cache_dir=str(tmp_path/'skills')),
        storage_node_id='node', workspace_dir=tmp_path/'ws', artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024, max_files=3, workspace_factory=StubWorkspace)
    try:
        await bind(app.workspaces)
        app.workspaces.resource_registry._scope(OWNER).sealed = True
        app.assembler._bound[OWNER] = object()
        app.assembler._preparation_started.add(OWNER)
        app.delivery._bindings[OWNER] = object()
        app.external_resources.jobs[(OWNER, 'producer', 'job')] = object()
        async def capture(result):
            assert OWNER in app.assembler._bound
            if failure:
                raise RuntimeError('delivery failed')
        monkeypatch.setattr(app.delivery, 'capture', capture)
        result = SimpleNamespace(owner=OWNER)
        if failure:
            with pytest.raises(RuntimeError, match='delivery failed'):
                await app.service.bindings.finalize_result(result)
        else:
            await app.service.bindings.finalize_result(result)
        assert (OWNER in app.assembler._bound) == failure
        assert (OWNER in app.workspaces._bindings) == failure
        assert (OWNER in app.delivery._bindings) == failure
        assert bool(app.external_resources.jobs) == failure
    finally:
        await app.workspaces.close()
        await engine.dispose()


async def test_cancelled_sweep_does_not_reuse_closing_workspace(tmp_path):
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Slow(StubWorkspace):
        async def close(self):
            entered.set()
            await proceed.wait()
            await super().close()
    mgr = manager(tmp_path, Slow)
    await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    sweep = asyncio.create_task(mgr.sweep_idle(ttl=0, capacity=0))
    await entered.wait()
    sweep.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sweep
    try:
        with pytest.raises(WorkspaceBindingError):
            await bind(mgr, replace(OWNER, task_id='next', execution_epoch=2))
    finally:
        proceed.set()
        await mgr.close()


async def test_evicted_retirement_cannot_certify_another_workers_workspace(tmp_path):
    mgr = manager(tmp_path)
    await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    await mgr.sweep_idle(ttl=0, capacity=0)
    other = manager(tmp_path)
    await other.get_workspace(IDENTITY)
    try:
        with pytest.raises(WorkspaceBindingError):
            await mgr.retire_session(IDENTITY.session_id)
    finally:
        await other.close()
        await mgr.close()


async def test_sweeper_uses_policy_and_stops_without_waiting_full_interval():
    from types import SimpleNamespace
    from workmate_runtime.execution.composition import WorkerApplication
    from workmate_product.config.worker import WorkspaceLifecyclePolicy
    stop = asyncio.Event()
    calls = []
    async def sweep(**kwargs):
        calls.append(kwargs)
        stop.set()
    app = SimpleNamespace(assembler=SimpleNamespace(settings=SimpleNamespace(
        workspace_lifecycle=WorkspaceLifecyclePolicy())),
        workspaces=SimpleNamespace(sweep_idle=sweep))
    await asyncio.wait_for(WorkerApplication._sweep(app, stop), 1)
    assert calls == [{'ttl': 600, 'capacity': 16}]


async def test_reuse_within_ttl_and_local_binding_reference_release(tmp_path):
    mgr = manager(tmp_path)
    first = await bind(mgr)
    first.workspace._local_bindings = [first.backend]
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.release_task(OWNER)
    assert first.workspace._local_bindings == []
    await mgr.sweep_idle(ttl=86400, capacity=16)
    assert first.workspace.closed == 0
    second = await bind(mgr, replace(OWNER, task_id='next', execution_epoch=2))
    assert second.workspace is first.workspace
    await mgr.sweep_idle(ttl=0, capacity=0)
    assert first.workspace.closed == 0
    await mgr.close()
    await mgr.sweep_idle(ttl=0, capacity=0)


async def test_cold_handoff_preserves_references_until_finalization(tmp_path):
    first, peer = manager(tmp_path), manager(tmp_path)
    binding = await bind(first)
    for _ in range(2):  # Active and parked scopes are both unsealed.
        with pytest.raises(WorkspaceBindingError):
            await first.close_task_workspace(OWNER)
    first.resource_registry._scope(OWNER).sealed = True
    await first.close_task_workspace(OWNER)
    await first.close_task_workspace(OWNER)
    assert OWNER in first._bindings and OWNER in first.resource_registry._scopes
    assert binding.workspace.closed == 1
    with pytest.raises(WorkspaceBindingError):
        await first.get_workspace(IDENTITY)
    second = await bind(peer, replace(OWNER, task_id='peer', worker_id='peer'))
    assert second.workspace.host_workdir == binding.workspace.host_workdir
    with pytest.raises(WorkspaceBindingError):
        await first.retire_session(IDENTITY.session_id)
    await first.release_task(OWNER)
    await first.release_task(OWNER)
    await first.close()
    await peer.close()


async def test_cold_close_failure_retains_ownership_and_can_retry(tmp_path):
    class Failing(StubWorkspace):
        async def close(self):
            if not getattr(self, 'allow_close', False):
                raise RuntimeError('close failed')
            await super().close()
    first, peer = manager(tmp_path, Failing), manager(tmp_path)
    binding = await bind(first)
    first.resource_registry._scope(OWNER).sealed = True
    with pytest.raises(RuntimeError):
        await first.close_task_workspace(OWNER)
    with pytest.raises(WorkspaceBindingError):
        await peer.get_workspace(IDENTITY)
    binding.workspace.allow_close = True
    await first.close_task_workspace(OWNER)
    await first.release_task(OWNER)
    fresh = await bind(first, replace(OWNER, task_id='next'))
    assert fresh.workspace is not binding.workspace
    fresh.workspace.allow_close = True
    await first.close()
    await peer.close()


@pytest.mark.parametrize('cold,fail_close', [(False, False), (True, False), (True, True)])
async def test_composed_terminal_seal_closes_only_cold_workspace_before_return(
        tmp_path, monkeypatch, cold, fail_close):
    from sqlalchemy.ext.asyncio import create_async_engine
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.execution.composition import compose_worker
    from workmate_product.config.worker import WorkspaceLifecyclePolicy

    class ClosingWorkspace(StubWorkspace):
        async def close(self):
            if fail_close and not getattr(self, 'allow_close', False):
                raise RuntimeError('close failed')
            await super().close()

    engine = create_async_engine('postgresql+asyncpg://unused:unused@127.0.0.1/unused')
    settings = RuntimeSettings(reme_enabled=False, skill_cache_dir=str(tmp_path/'skills'),
        workspace_lifecycle=WorkspaceLifecyclePolicy(max_warm_per_worker=0 if cold else 16))
    app = compose_worker(engine, settings, storage_node_id='node', workspace_dir=tmp_path/'ws',
        artifact_dir=tmp_path/'artifacts', max_file_bytes=1024, max_files=3,
        workspace_factory=ClosingWorkspace)
    binding = await bind(app.workspaces)
    app.workspaces.resource_registry._scope(OWNER).sealed = True
    app.assembler._bound[OWNER] = object()
    async def seal(*args):
        assert binding.workspace.closed == 0
        return 'immutable-seal'
    monkeypatch.setattr(app.delivery, 'seal_terminal', seal)
    try:
        if fail_close:
            with pytest.raises(RuntimeError, match='close failed'):
                await app.service.controller.seal_terminal(OWNER, 'reply', 'completed', {})
            assert IDENTITY in app.workspaces._ownership
        else:
            assert await app.service.controller.seal_terminal(OWNER, 'reply', 'completed', {}) == 'immutable-seal'
            assert binding.workspace.closed == int(cold)
            assert (IDENTITY in app.workspaces._ownership) != cold
        assert OWNER in app.assembler._bound
        assert OWNER in app.workspaces.resource_registry._scopes
    finally:
        binding.workspace.allow_close = True
        await app.workspaces.close()
        await engine.dispose()


async def test_cold_preparation_cleanup_joins_unbound_initialization(tmp_path):
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Slow(StubWorkspace):
        async def initialize(self):
            entered.set()
            await proceed.wait()
            await super().initialize()
    mgr, peer = manager(tmp_path, Slow), manager(tmp_path)
    preparing = asyncio.create_task(mgr.get_workspace(IDENTITY))
    await entered.wait()
    preparing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    cleanup = asyncio.create_task(mgr.close_prepared_workspace(OWNER))
    await asyncio.sleep(0)
    assert not cleanup.done()
    with pytest.raises(WorkspaceBindingError):
        await peer.get_workspace(IDENTITY)
    proceed.set()
    await cleanup
    assert IDENTITY not in mgr._entries
    await peer.get_workspace(IDENTITY)
    await peer.close()
    await mgr.close()


async def test_cold_preparation_bound_cleanup_preserves_scope(tmp_path):
    mgr = manager(tmp_path)
    binding = await bind(mgr)
    mgr.resource_registry._scope(OWNER).sealed = True
    await mgr.close_prepared_workspace(OWNER)
    assert binding.workspace.closed == 1
    assert OWNER in mgr._bindings and OWNER in mgr.resource_registry._scopes
    await mgr.release_task(OWNER)
    await mgr.close()


async def test_cold_preparation_close_failure_keeps_lock(tmp_path):
    class Failing(StubWorkspace):
        async def close(self):
            if not getattr(self, 'allow_close', False):
                raise RuntimeError('close failed')
            await super().close()
    mgr, peer = manager(tmp_path, Failing), manager(tmp_path)
    workspace = await mgr.get_workspace(IDENTITY)
    with pytest.raises(RuntimeError):
        await mgr.close_prepared_workspace(OWNER)
    with pytest.raises(WorkspaceBindingError):
        await peer.get_workspace(IDENTITY)
    workspace.allow_close = True
    await mgr.close_prepared_workspace(OWNER)
    await peer.get_workspace(IDENTITY)
    await peer.close()
    await mgr.close()


@pytest.mark.parametrize('proof,fail_close', [(False, False), (True, False), (True, True)])
async def test_composed_preparation_proof_waits_for_cold_close(tmp_path, monkeypatch, proof, fail_close):
    from sqlalchemy.ext.asyncio import create_async_engine
    from workmate_runtime.config import RuntimeSettings
    from workmate_runtime.execution.composition import compose_worker
    from workmate_product.config.worker import WorkspaceLifecyclePolicy
    engine = create_async_engine('postgresql+asyncpg://unused:unused@127.0.0.1/unused')
    settings = RuntimeSettings(reme_enabled=False, skill_cache_dir=str(tmp_path/'skills'),
        workspace_lifecycle=WorkspaceLifecyclePolicy(max_warm_per_worker=0))
    app = compose_worker(engine, settings, storage_node_id='node', workspace_dir=tmp_path/'ws',
        artifact_dir=tmp_path/'artifacts', max_file_bytes=1024, max_files=3,
        workspace_factory=StubWorkspace)
    calls = []
    async def verify(owner):
        calls.append('proof')
        return proof
    async def close(owner):
        calls.append('close')
        if fail_close:
            raise RuntimeError('close failed')
    monkeypatch.setattr(app.assembler, 'verify_preparation_cleanup', verify)
    monkeypatch.setattr(app.workspaces, 'close_prepared_workspace', close)
    try:
        if fail_close:
            with pytest.raises(RuntimeError):
                await app.service.controller.verify_preparation_cleanup(OWNER)
        else:
            assert await app.service.controller.verify_preparation_cleanup(OWNER) is proof
        assert calls == (['proof', 'close'] if proof else ['proof'])
    finally:
        await app.workspaces.close()
        await engine.dispose()
