"""Resource quiescence is established from owned processes, not asyncio cancel."""
import asyncio
import os
import signal
import sys

import pytest

from workmate_runtime.execution.resources import TaskResourceRegistry, ResourceScopeError, TaskResourceBackendMixin
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.engines.agentscope import CoreSnapshot
from agentscope.state import AgentState
from agentscope.workspace._bubblewrap._bubblewrap_backend import BubblewrapBackend


OWNER = ExecutionOwner('session','task','worker',1)


def stopped():
    return CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))


async def spawn():
    return await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(60)',
        start_new_session=True,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)


async def kill_fixture(process):
    if process.returncode is None:
        process.kill()
    await process.wait()


async def test_unknown_owner_and_new_epoch_cannot_report_empty_registry_safe():
    registry = TaskResourceRegistry()
    assert await registry.verify_cleanup(OWNER,None,stopped()) is False
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    foreign = ExecutionOwner('session','task','worker',2)
    assert await registry.verify_cleanup(foreign,None,stopped()) is False
    assert await registry.verify_cleanup(OWNER,None,stopped()) is True


async def test_cleanup_terminates_and_waits_for_actual_owned_child():
    registry = TaskResourceRegistry(grace=.1)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    registry.reserve(OWNER,'process',kind='process')
    process = await spawn()
    try:
        registry.attach_process(OWNER,'process',process,terminate_tree=BubblewrapBackend._terminate_process_tree,containment="trusted_process_group")
        assert process.returncode is None
        assert await registry.verify_cleanup(OWNER,None,stopped()) is True
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid,0)
    finally:
        await kill_fixture(process)


async def test_cancelling_wait_task_does_not_mark_process_finished():
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    registry.reserve(OWNER,'process',kind='process')
    process = await spawn()
    try:
        registry.attach_process(OWNER,'process',process,terminate_tree=BubblewrapBackend._terminate_process_tree,containment="trusted_process_group")
        waiter = asyncio.create_task(process.wait())
        waiter.cancel()
        await asyncio.gather(waiter,return_exceptions=True)
        assert process.returncode is None
        assert await registry.mark_finished(OWNER,'process') is False
        assert await registry.verify_cleanup(OWNER,None,stopped()) is True
    finally:
        await kill_fixture(process)


async def test_unknown_launch_and_unproven_external_result_fail_closed():
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    registry.reserve(OWNER,'launch',kind='process')
    assert await registry.verify_cleanup(OWNER,None,stopped()) is False
    with pytest.raises(ResourceScopeError):
        registry.reserve(OWNER,'too-late',kind='process')


@pytest.mark.parametrize('proof', [False,None,'completed',1])
async def test_external_cleanup_requires_explicit_trusted_quiescence(proof):
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    async def cleanup():
        return None
    async def probe():
        return proof
    registry.register_external(OWNER,'external',tool_call_id='call',cleanup=cleanup,probe=probe)
    assert await registry.verify_cleanup(OWNER,None,stopped()) is False


@pytest.mark.parametrize('fresh,covered', [(False,True),(True,False),(1,True),(True,'yes')])
def test_assembler_must_explicitly_attest_fresh_complete_resource_coverage(fresh,covered):
    with pytest.raises(ResourceScopeError):
        TaskResourceRegistry().open_scope(OWNER,fresh_claim=fresh,all_resource_paths_tracked=covered)


async def test_external_resource_cleanup_is_verified_and_owner_isolated():
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    alive = True
    async def cleanup():
        nonlocal alive
        alive = False
    async def probe():
        return not alive
    registry.register_external(OWNER,'job',tool_call_id='call',cleanup=cleanup,probe=probe)
    wrong = ExecutionOwner('session','other-task','worker',1)
    with pytest.raises(ResourceScopeError):
        await registry.mark_finished(wrong,'job')
    assert alive is True
    assert await registry.mark_finished(OWNER,'job') is False
    assert await registry.verify_cleanup(OWNER,None,stopped()) is True
    assert await registry.mark_finished(OWNER,'job') is True
    with pytest.raises(ResourceScopeError):
        registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)


async def test_submitted_snapshot_without_owned_external_registration_is_unknown():
    from test_core_engine import load_golden_snapshot
    original = load_golden_snapshot('parked')
    payload = original.model_dump(mode='json')
    payload['state']['session_id'] = 'session'
    for message in payload['state']['context']:
        for block in message['content']:
            if block['type']=='tool_call':
                block['state']='submitted'
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    terminal = stopped()
    terminal.state['reply_context'] = payload['state']['reply_context']
    assert await registry.verify_cleanup(OWNER,CoreSnapshot.model_validate(payload),terminal) is False


async def test_exited_leader_does_not_prove_surviving_child_quiescent():
    registry = TaskResourceRegistry(grace=.05)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    registry.reserve(OWNER,'tree',kind='process')
    script = 'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)'
    process = await asyncio.create_subprocess_exec(sys.executable,'-c',script,start_new_session=True,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    child = None
    try:
        registry.attach_process(OWNER,'tree',process,terminate_tree=BubblewrapBackend._terminate_process_tree,
            containment='trusted_process_group')
        child = int(await process.stdout.readline())
        await process.wait()
        assert await registry.verify_cleanup(OWNER,None,stopped()) is False
        os.kill(child,0)  # Only this fixture's known child remains; no false proof.
    finally:
        if child is not None:
            try:
                os.kill(child,signal.SIGKILL)
            except ProcessLookupError:
                pass
        await kill_fixture(process)


@pytest.mark.parametrize('failure', ['probe','cleanup','timeout'])
async def test_unknown_cleanup_outcomes_cannot_become_true(failure):
    registry = TaskResourceRegistry(cleanup_timeout=.02)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    async def cleanup():
        if failure=='timeout':
            await asyncio.Event().wait()
        raise OSError('fixture cleanup unknown')
    async def probe():
        if failure=='probe':
            raise OSError('fixture probe unknown')
        return False
    registry.register_external(OWNER,'job',tool_call_id='call',cleanup=cleanup,probe=probe)
    assert await registry.verify_cleanup(OWNER,None,stopped()) is False


async def test_backend_start_cancellation_keeps_spawned_process_registered(tmp_path):
    registry = TaskResourceRegistry(grace=.05)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    spawned,release = asyncio.Event(),asyncio.Event()
    processes = []
    class FixtureBackend(BubblewrapBackend):
        async def start_process(self,command,**kwargs):
            process = await spawn()
            processes.append(process)
            spawned.set()
            await release.wait()
            return process
    class Tracked(TaskResourceBackendMixin,FixtureBackend):
        pass
    backend = Tracked(resource_registry=registry,execution_owner=OWNER,
        host_workdir=str(tmp_path/'work'),host_tmpdir=str(tmp_path/'tmp'))
    task = asyncio.create_task(backend.start_process(['fixture']))
    try:
        await spawned.wait()
        task.cancel()
        assert await registry.verify_cleanup(OWNER,None,stopped()) is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert await registry.verify_cleanup(OWNER,None,stopped()) is True
        with pytest.raises(ResourceScopeError):
            await backend.start_process(['late'])
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        for process in processes:
            await kill_fixture(process)


async def test_process_handle_cannot_be_rebound_to_another_owner():
    registry = TaskResourceRegistry()
    other = ExecutionOwner('session','other-task','worker',2)
    for owner in (OWNER,other):
        registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
        registry.reserve(owner,'child',kind='process')
    process = await spawn()
    try:
        registry.attach_process(OWNER,'child',process,terminate_tree=BubblewrapBackend._terminate_process_tree)
        with pytest.raises(ResourceScopeError):
            registry.attach_process(other,'child',process,terminate_tree=BubblewrapBackend._terminate_process_tree)
        assert await registry.verify_cleanup(other,None,stopped()) is False
        assert process.returncode is None
    finally:
        await kill_fixture(process)


async def test_repeated_cancellation_cannot_erase_inflight_launch_handle(tmp_path):
    registry = TaskResourceRegistry(grace=.05)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    spawned,release = asyncio.Event(),asyncio.Event()
    processes = []
    class FixtureBackend(BubblewrapBackend):
        async def start_process(self,command,**kwargs):
            process = await spawn()
            processes.append(process)
            spawned.set()
            await release.wait()
            return process
    class Tracked(TaskResourceBackendMixin,FixtureBackend):
        pass
    backend = Tracked(resource_registry=registry,execution_owner=OWNER,
        host_workdir=str(tmp_path/'work'),host_tmpdir=str(tmp_path/'tmp'))
    task = asyncio.create_task(backend.start_process(['fixture']))
    try:
        await spawned.wait()
        task.cancel()
        await asyncio.sleep(0)  # Deliver first cancellation at the launch shield.
        task.cancel()
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        async with asyncio.timeout(3):
            while not await registry.verify_cleanup(OWNER,None,stopped()):
                await asyncio.sleep(.01)
        assert processes[0].returncode is not None
    finally:
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        for process in processes:
            await kill_fixture(process)


@pytest.mark.parametrize('own_group', [True,False])
async def test_failed_process_attachment_cleans_new_child_but_retains_unknown_blocker(tmp_path,own_group):
    class RefuseAttachment(TaskResourceRegistry):
        def attach_process(self,*args,**kwargs):
            raise ResourceScopeError('fixture registration rejected')
    registry = RefuseAttachment(grace=.05)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    processes = []
    class FixtureBackend(BubblewrapBackend):
        async def start_process(self,command,**kwargs):
            process = await asyncio.create_subprocess_exec(sys.executable,'-c','import time; time.sleep(60)',
                start_new_session=own_group,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            processes.append(process)
            return process
    class Tracked(TaskResourceBackendMixin,FixtureBackend):
        pass
    backend = Tracked(resource_registry=registry,execution_owner=OWNER,
        host_workdir=str(tmp_path/'work'),host_tmpdir=str(tmp_path/'tmp'))
    try:
        with pytest.raises(ResourceScopeError):
            await backend.start_process(['fixture'])
        assert processes[0].returncode is not None
        assert await registry.verify_cleanup(OWNER,None,stopped()) is False
    finally:
        for process in processes:
            await kill_fixture(process)


async def test_launch_error_after_repeated_cancellation_is_observed_and_unknown(tmp_path):
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    entered,release = asyncio.Event(),asyncio.Event()
    class FixtureBackend(BubblewrapBackend):
        async def start_process(self,command,**kwargs):
            entered.set()
            await release.wait()
            raise OSError('fixture spawn outcome unknown')
    class Tracked(TaskResourceBackendMixin,FixtureBackend):
        pass
    backend = Tracked(resource_registry=registry,execution_owner=OWNER,
        host_workdir=str(tmp_path/'work'),host_tmpdir=str(tmp_path/'tmp'))
    task = asyncio.create_task(backend.start_process(['fixture']))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    await asyncio.gather(task,return_exceptions=True)
    async with asyncio.timeout(3):
        while registry._cleanup_tasks:
            await asyncio.sleep(0)
    assert await registry.verify_cleanup(OWNER,None,stopped()) is False


async def test_real_core_prior_reply_external_snapshot_does_not_require_new_task_registry_entry():
    from test_core_engine import ConfirmTool,make_engine,collect
    from agentscope.message import ToolCallBlock,ToolResultBlock,TextBlock,UserMsg
    from agentscope.event import ConfirmResult,UserConfirmResultEvent,ExternalExecutionResultEvent
    tool=ConfirmTool()
    tool.is_external_tool=True
    engine=make_engine([[ToolCallBlock(id='external',name=tool.name,input='{}')]],
        state=AgentState(session_id='session'),tool=tool)
    events=await collect(engine,UserMsg(name='user',content='external'))
    request=next(e for e in events if e.type=='REQUIRE_USER_CONFIRM')
    waiting=make_engine([],state=engine.snapshot().to_state(),tool=tool)
    await collect(waiting,UserConfirmResultEvent(reply_id=request.reply_id,
        confirm_results=[ConfirmResult(confirmed=True,tool_call=request.tool_calls[0])]))
    previous=waiting.snapshot()
    assert previous.boundary_evidence('test').pending_tools[0].kind=='external_execution'
    from agentscope.event import UserInterruptEvent
    interrupted=make_engine([],state=previous.to_state(),tool=tool)
    await collect(interrupted,UserInterruptEvent(reply_id=request.reply_id))
    missing=TaskResourceRegistry()
    missing.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    assert await missing.verify_cleanup(OWNER,previous,interrupted.snapshot()) is False
    finished=make_engine([[TextBlock(text='external completed')]],state=previous.to_state(),tool=tool)
    await collect(finished,ExternalExecutionResultEvent(reply_id=request.reply_id,
        execution_results=[ToolResultBlock(id='external',name=tool.name,output=[TextBlock(text='done')])]))
    next_task=make_engine([[TextBlock(text='next task completed')]],state=finished.snapshot().to_state())
    await collect(next_task,UserMsg(name='user',content='new task'))
    assert next_task.agent.state.reply_id != request.reply_id
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    assert await registry.verify_cleanup(OWNER,previous,next_task.snapshot()) is True


async def test_closed_submitted_history_in_same_snapshot_does_not_count_as_live_job():
    from agentscope.message import AssistantMsg,ToolCallBlock,ToolResultBlock,TextBlock
    state=AgentState(session_id='session')
    state.context.append(AssistantMsg(id=state.reply_id,name='test',content=[
        ToolCallBlock(id='old-job',name='external',input='{}',state='submitted'),
        ToolResultBlock(id='old-job',name='external',output=[TextBlock(text='completed')])]))
    snapshot=CoreSnapshot(state=state.model_dump(mode='json'))
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    assert await registry.verify_cleanup(OWNER,None,snapshot) is True


@pytest.mark.asyncio
async def test_preparation_cleanup_has_no_core_snapshot_and_requires_known_scope():
    registry=TaskResourceRegistry()
    owner=ExecutionOwner('s','t','w',1)
    assert await registry.verify_preparation_cleanup(owner) is False
    registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
    assert await registry.verify_preparation_cleanup(owner) is True
    with pytest.raises(ResourceScopeError):
        registry.reserve(owner,'late',kind='process')


async def test_preparation_cleanup_terminates_real_child_and_blocks_unresolved_launch():
    registry=TaskResourceRegistry(grace=.1)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    registry.reserve(OWNER,'process',kind='process')
    process=await spawn()
    try:
        registry.attach_process(OWNER,'process',process,terminate_tree=BubblewrapBackend._terminate_process_tree,
            containment='trusted_process_group')
        assert await registry.verify_preparation_cleanup(OWNER) is True
        assert process.returncode is not None
    finally:
        await kill_fixture(process)
    unknown=TaskResourceRegistry()
    unknown.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    unknown.reserve(OWNER,'launch',kind='process')
    assert await unknown.verify_preparation_cleanup(OWNER) is False


@pytest.mark.parametrize('descendant', ['none','separate_stdio','inherited_pipe'])
async def test_fast_exit_registration_waits_for_child_without_forging_group_cleanup(tmp_path,monkeypatch,descendant):
    """Model the getpgid/child-watcher gap; all wait/group evidence stays real."""
    registry=TaskResourceRegistry(grace=.05,cleanup_timeout=.2)
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    script='''import subprocess,sys
mode=sys.argv[1]
child=None
if mode!='none':
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],
        stdout=None if mode=='inherited_pipe' else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
print(child.pid if child else 0,flush=True)
sys.stdin.readline()
'''
    process=None
    child=0
    real_getpgid=os.getpgid
    lookups=0
    class FixtureBackend(BubblewrapBackend):
        async def start_process(self,command,**kwargs):
            nonlocal process,child
            process=await asyncio.create_subprocess_exec(sys.executable,'-c',script,descendant,
                start_new_session=True,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            child=int(await process.stdout.readline())
            return process
    class Tracked(TaskResourceBackendMixin,FixtureBackend):
        pass
    def exited_before_callback(pid):
        nonlocal lookups
        if process is not None and pid==process.pid:
            lookups+=1
            if lookups==1:
                assert real_getpgid(pid)==pid and process.returncode is None
                # The child callback has not established completion yet. Make
                # the OS identity unavailable and let the actual child exit.
                process.stdin.write(b'exit\n')
                raise ProcessLookupError()
        return real_getpgid(pid)
    monkeypatch.setattr(os,'getpgid',exited_before_callback)
    backend=Tracked(resource_registry=registry,execution_owner=OWNER,
        host_workdir=str(tmp_path/'work'),host_tmpdir=str(tmp_path/'tmp'))
    try:
        async with asyncio.timeout(3):
            if descendant=='inherited_pipe':
                # An exited leader's inherited pipe can keep an early wait()
                # pending. Bound registration and retain the unknown blocker.
                with pytest.raises(TimeoutError):
                    await backend.start_process(['fixture'])
                assert await registry.verify_cleanup(OWNER,None,stopped()) is False
            else:
                assert await backend.start_process(['fixture']) is process
                assert process.returncode==0
                assert await registry.verify_cleanup(OWNER,None,stopped()) is (descendant=='none')
            if child:
                os.kill(child,0)
    finally:
        if child:
            try: os.kill(child,signal.SIGKILL)
            except ProcessLookupError: pass
        if process is not None:
            await kill_fixture(process)
