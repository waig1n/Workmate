"""Only a complete permission boundary can release a tracked execution scope."""
import asyncio
from copy import deepcopy

import pytest

from workmate_runtime.execution.resources import ResourceScopeError, TaskResourceRegistry
from workmate_runtime.transport.postgres import ExecutionOwner
from test_core_engine import load_golden_snapshot

OWNER = ExecutionOwner('session-1', 'task', 'worker', 1)


def registry():
    value = TaskResourceRegistry(cleanup_timeout=.02)
    value.open_scope(OWNER, fresh_claim=True, all_resource_paths_tracked=True)
    return value


def parked():
    return load_golden_snapshot('parked').model_dump(mode='json')


async def test_permission_is_not_running_resource_and_scope_cannot_launch_after_release():
    value = registry()
    snapshot = parked()
    original = deepcopy(snapshot)
    assert await value.verify_parked_cleanup(OWNER, snapshot, agent_name='test') is True
    assert snapshot == original
    with pytest.raises(ResourceScopeError):
        value.reserve(OWNER, 'late', kind='process')


@pytest.mark.parametrize('case', ['missing', 'empty', 'version', 'session', 'reply', 'agent', 'normal', 'external', 'mixed'])
async def test_invalid_or_nonpermission_boundary_cannot_release(case):
    value = registry()
    snapshot = parked()
    name = 'test'
    if case == 'missing':
        snapshot = None
    elif case == 'empty':
        snapshot = {'state': {}}
    elif case == 'version':
        snapshot['format_version'] = 2
    elif case == 'session':
        snapshot['state']['session_id'] = 'different'
    elif case == 'reply':
        snapshot['state']['reply_context']['reply_id'] = 'different'
    elif case == 'agent':
        name = 'different'
    elif case == 'normal':
        snapshot = load_golden_snapshot('normal').model_dump(mode='json')
    elif case == 'external':
        snapshot['state']['context'][-1]['content'][0]['state'] = 'submitted'
    else:
        call = deepcopy(snapshot['state']['context'][-1]['content'][0])
        call.update(id='external', state='submitted')
        snapshot['state']['context'][-1]['content'].append(call)
    assert await value.verify_parked_cleanup(OWNER, snapshot, agent_name=name) is False
    # Unsupported boundaries stay pinned; their owner may continue execution.
    value.reserve(OWNER, 'continuation', kind='process')


async def test_unknown_owner_cannot_prove_quiescence():
    assert await TaskResourceRegistry().verify_parked_cleanup(OWNER, parked(), agent_name='test') is False


async def test_registered_external_cleanup_is_verified_even_at_permission_boundary():
    value = registry()
    alive = True
    async def cleanup():
        nonlocal alive
        alive = False
    async def probe():
        return not alive
    value.register_external(OWNER, 'job', tool_call_id='previous', cleanup=cleanup, probe=probe)
    assert await value.verify_parked_cleanup(OWNER, parked(), agent_name='test') is True
    assert alive is False


@pytest.mark.parametrize('failure', ['unknown_launch', 'false_probe', 'exception', 'timeout'])
async def test_resource_failures_never_claim_released(failure):
    value = registry()
    if failure == 'unknown_launch':
        value.reserve(OWNER, 'launch', kind='process')
    else:
        async def cleanup():
            if failure == 'exception':
                raise OSError('unknown cleanup')
            if failure == 'timeout':
                await asyncio.Event().wait()
        async def probe():
            return False
        value.register_external(OWNER, 'job', tool_call_id='prior', cleanup=cleanup, probe=probe)
    assert await value.verify_parked_cleanup(OWNER, parked(), agent_name='test') is False
    with pytest.raises(ResourceScopeError):
        value.reserve(OWNER, 'late', kind='process')


async def test_cancellation_propagates_and_keeps_scope_sealed():
    value = registry()
    entered = asyncio.Event()
    async def cleanup():
        entered.set()
        await asyncio.Event().wait()
    async def probe():
        return False
    value.register_external(OWNER, 'job', tool_call_id='prior', cleanup=cleanup, probe=probe)
    task = asyncio.create_task(value.verify_parked_cleanup(OWNER, parked(), agent_name='test'))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ResourceScopeError):
        value.reserve(OWNER, 'late', kind='process')


async def test_parked_cleanup_terminates_actual_process_and_preserves_snapshot():
    from agentscope.workspace._bubblewrap._bubblewrap_backend import BubblewrapBackend
    from test_execution_resources import spawn, kill_fixture
    value = TaskResourceRegistry(grace=.05, cleanup_timeout=1)
    value.open_scope(OWNER, fresh_claim=True, all_resource_paths_tracked=True)
    value.reserve(OWNER, 'child', kind='process')
    process = await spawn()
    snapshot = parked()
    original = deepcopy(snapshot)
    try:
        value.attach_process(OWNER, 'child', process,
            terminate_tree=BubblewrapBackend._terminate_process_tree,
            containment='trusted_process_group')
        assert await value.verify_parked_cleanup(OWNER, snapshot, agent_name='test') is True
        assert process.returncode is not None
        assert snapshot == original
    finally:
        await kill_fixture(process)


async def test_unresolved_launch_blocks_release_even_with_positive_probe():
    value = registry()
    async def cleanup():
        return None
    async def probe():
        return True
    value.register_external(OWNER, 'job', tool_call_id='prior', cleanup=cleanup, probe=probe)
    gate = asyncio.Event()
    launch = asyncio.create_task(gate.wait())
    value._resource(OWNER, 'job').launch = launch
    try:
        assert await value.verify_parked_cleanup(OWNER, parked(), agent_name='test') is False
        gate.set()
        await launch
        assert await value.verify_parked_cleanup(OWNER, parked(), agent_name='test') is True
    finally:
        gate.set()
        await launch


async def test_external_pending_in_earlier_message_blocks_permission_release():
    snapshot = parked()
    prior = deepcopy(snapshot['state']['context'][-1])
    prior['id'] = 'earlier-reply'
    prior['content'][0].update(id='external', state='submitted')
    snapshot['state']['context'].insert(0, prior)
    assert await registry().verify_parked_cleanup(OWNER, snapshot, agent_name='test') is False
