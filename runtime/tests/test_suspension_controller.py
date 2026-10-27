"""Production pause orchestration uses durable exclusion before local cleanup."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_postgres_execution_store import database
from test_resume_controller import parked, submit
from workmate_runtime.execution.suspension import SuspensionController


def controller(pool, owner, *, provider='bubblewrap'):
    bound=SimpleNamespace(identity='identity',agent_name='test')
    spaces=SimpleNamespace(provider=SimpleNamespace(name=provider),
        _ownership={'identity':SimpleNamespace(generation='generation-1')},
        suspend_task_workspace=AsyncMock())
    assembler=SimpleNamespace(workspaces=spaces,_bound={owner:bound},_preparation_started={owner})
    delivery=SimpleNamespace(_bindings={owner:object()},_checkpoint_workspaces={owner:object()})
    return SuspensionController(pool,assembler,delivery),assembler,delivery


async def test_suspend_releases_only_after_local_cleanup(database):
    pool,db=database
    first=await parked(pool,db)
    control,assembly,delivery=controller(pool,first.owner)
    async def cleanup(*args,**kwargs):
        assert await db.fetchval('SELECT status FROM runtime_suspensions')=='releasing'
        assert await db.fetchval('SELECT owner_id FROM runtime_sessions')==first.owner.worker_id
    assembly.workspaces.suspend_task_workspace.side_effect=cleanup
    assert await control.try_suspend(first.owner,'node') is True
    assert await db.fetchval('SELECT status FROM runtime_suspensions')=='released'
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions') is None
    assert not assembly._bound and not delivery._bindings and not delivery._checkpoint_workspaces


async def test_cleanup_failure_latches_and_keeps_owner(database):
    pool,db=database
    first=await parked(pool,db)
    control,assembly,delivery=controller(pool,first.owner)
    assembly.workspaces.suspend_task_workspace.side_effect=RuntimeError('cleanup')
    with pytest.raises(RuntimeError):
        await control.try_suspend(first.owner,'node')
    assert delivery._bindings and delivery._checkpoint_workspaces
    assert await db.fetchval('SELECT status FROM runtime_suspensions')=='blocked'
    assert await db.fetchval('SELECT owner_id FROM runtime_sessions')==first.owner.worker_id


@pytest.mark.parametrize('case',['control','provider'])
async def test_pending_control_or_unsupported_provider_remains_pinned(database,case):
    pool,db=database
    first=await parked(pool,db)
    control,assembly,_=controller(pool,first.owner,provider='e2b' if case=='provider' else 'bubblewrap')
    if case=='control':
        await submit(db,first.reply_id)
    assert await control.try_suspend(first.owner,'node') is False
    assembly.workspaces.suspend_task_workspace.assert_not_called()


async def test_lost_release_response_retries_receipt_without_cleanup(database,monkeypatch):
    pool,db=database
    first=await parked(pool,db)
    control,assembly,_=controller(pool,first.owner)
    from workmate_runtime.transport.suspension import SuspensionStore
    release=SuspensionStore.release
    calls=[]
    async def lost_response(store,claim,proof):
        await release(store,claim,proof)
        calls.append(1)
        if len(calls)==1:
            raise ConnectionError('response lost')
    monkeypatch.setattr(SuspensionStore,'release',lost_response)
    assert await control.try_suspend(first.owner,'node') is True
    assert len(calls)==2
    assembly.workspaces.suspend_task_workspace.assert_awaited_once()


@pytest.mark.parametrize('provider', ['docker', 'kubernetes'])
async def test_remote_suspend_persists_storage_proof_and_uses_strict_checkpoint(database, provider):
    import json
    pool, db = database
    first = await parked(pool, db)
    control, assembly, delivery = controller(pool, first.owner, provider=provider)
    storage = {'provider':provider, 'fixture':'opaque-storage-proof'}
    assembly.workspaces.suspend_task_workspace.return_value = storage
    strict = AsyncMock()
    delivery.checkpoints = SimpleNamespace(capture_required=strict)
    assert await control.try_suspend(first.owner,'node') is True
    assert assembly.workspaces.suspend_task_workspace.call_args.kwargs['checkpoint'] is strict
    proof = json.loads(await db.fetchval('SELECT cleanup_receipt FROM runtime_suspensions'))
    assert proof['storage_proof'] == storage
    assert not delivery._bindings and not delivery._checkpoint_workspaces
