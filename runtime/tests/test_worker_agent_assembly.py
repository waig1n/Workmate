"""Concrete AgentScope Core assembly; no App, network or sandbox execution."""
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from agentscope.event import UserInterruptEvent
from agentscope.message import TextBlock,ToolCallBlock,UserMsg
from agentscope.permission import PermissionMode
from agentscope.state import AgentState
from workmate_product.application.execution_context import (
    ExecutionContext,ExecutionSpec,ProductInputMessage,SelectedModel,ToolPolicy,
)
from workmate_product.config.knowledge import KnowledgeConfig
from workmate_runtime.assembly.agent import WorkerAgentAssembler,AgentAssemblyError
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.execution.controller import ExecutionGuard
from workmate_runtime.execution.stopping import StopGuard
from workmate_runtime.transport.postgres import ExecutionOwner
from test_core_engine import ScriptModel
from test_worker_workspaces import manager

OWNER = ExecutionOwner('session','task','worker',1)
MODEL = SelectedModel(profile_name='chat',model_type='openai',model='fixture',api_key_env='FIXTURE_KEY')


def context():
    original = ToolPolicy(denied_tools=('original',))
    current = ToolPolicy(denied_tools=('current',))
    spec = ExecutionSpec(task_id='task',session_id='session',conversation_id='conversation',
        actor_user_id='user',agent_id='agent',approval_profile='standard',tool_policy=original,
        platform_tool_profile='none',capability_ids=(),knowledge_base_ids=(),selected_model=MODEL)
    return ExecutionContext(task_id='task',session_id='session',conversation_id='conversation',
        actor_user_id='user',agent_id='agent',agent_scope_id='scope',system_prompt='Solve the task',
        platform_tool_profile='none',current_tool_policy=current,
        effective_tool_policy=ToolPolicy(denied_tools=('current','original')),current_model_profile='chat',
        knowledge_config=KnowledgeConfig(),capabilities=(),
        input_messages=(ProductInputMessage('message',{'text':'hello'}),),initial_spec=spec)


class Sessions:
    @asynccontextmanager
    async def begin(self):
        yield None


class LocalGuard(ExecutionGuard):
    def __init__(self,owner=OWNER):
        super().__init__(None,owner,'command')
    async def call(self,method,*args,**kwargs):
        return None


@pytest.fixture
async def assembly(tmp_path,monkeypatch):
    import workmate_runtime.assembly.agent as module
    state = {'context':context(),'models':[]}
    async def read(db,**kwargs):
        state['read_kwargs'] = kwargs
        if isinstance(state['context'],Exception):
            raise state['context']
        return state['context']
    def model(settings,selected):
        result = ScriptModel([[TextBlock(text='done')]])
        state['models'].append(result)
        return result
    async def sandbox_proof(*args):
        return None
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',sandbox_proof)
    monkeypatch.setattr(module,'read_execution_context',read)
    monkeypatch.setattr(module,'build_selected_model',model)
    workspace = manager(tmp_path)
    assembler = WorkerAgentAssembler(Sessions(),RuntimeSettings(reme_enabled=False),workspace)
    yield assembler,state,workspace
    await workspace.close()


async def test_start_builds_actual_core_toolkit_and_task_offloader(assembly):
    assembler,fixture,workspace = assembly
    guard = LocalGuard()
    prepared = await assembler.prepare_start(OWNER,None,guard)
    agent = prepared.engine.agent
    assert agent.name=='agent' and agent.state.session_id=='session'
    assert prepared.inputs[0].id=='message' and prepared.inputs[0].name=='user'
    assert prepared.inputs[0].get_text_content()=='hello'
    assert agent.offloader.get_backend() is not (await workspace.get_workspace(assembler.identity(OWNER))).get_backend()
    assert agent._reply_middlewares[-1] is guard
    assert agent._check_permission_middlewares[0]._policy.denied_tools == ['current','original']
    assert 'Workspace' in agent._system_prompt and 'Solve the task' in agent._system_prompt
    assert agent._system_prompt.rstrip().endswith(
        "Preserve code, identifiers, and quotations in their original language."
    )
    assert agent._system_prompt.index("Workspace") < agent._system_prompt.index("## Response language")
    events = [e async for e in prepared.engine.reply_stream(prepared.inputs)]
    assert events[-1].type=='REPLY_END'
    assert agent.state.permission_context.mode is PermissionMode.ACCEPT_EDITS
    assert set(agent.state.permission_context.working_directories)=={'/workspace'}


async def test_start_inherits_exact_previous_state_before_execution(assembly):
    assembler,fixture,workspace = assembly
    snapshot = CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))
    prepared = await assembler.prepare_start(OWNER,snapshot,LocalGuard())
    assert prepared.engine.snapshot()==snapshot
    assert snapshot.to_state().permission_context.mode is PermissionMode.DEFAULT


async def test_resume_is_fresh_core_exact_state_same_binding_and_current_policy(assembly):
    assembler,fixture,workspace = assembly
    first = await assembler.prepare_start(OWNER,None,LocalGuard())
    snapshot = first.engine.snapshot()
    guard = LocalGuard()
    guard.reply_id = snapshot.to_state().reply_id
    resumed = await assembler.prepare_resume(OWNER,snapshot,guard)
    assert resumed is not first.engine and resumed.agent is not first.engine.agent
    assert resumed.snapshot()==snapshot
    assert resumed.agent.offloader.get_backend() is first.engine.agent.offloader.get_backend()
    assert len(fixture['models'])==2


@pytest.mark.parametrize('case',['reme','knowledge','platform_mismatch','platform_unknown','capability','blocks'])
async def test_unimplemented_business_capabilities_are_not_silently_dropped(assembly,case):
    assembler,fixture,workspace = assembly
    ctx = fixture['context']
    if case=='reme':
        assembler.settings.reme_enabled=True
    elif case=='knowledge':
        assembler.settings.knowledge.enabled=True
    elif case=='platform_mismatch':
        fixture['context']=replace(ctx,platform_tool_profile='workspace')
    elif case=='platform_unknown':
        fixture['context']=replace(ctx,platform_tool_profile='unknown',
            initial_spec=ctx.initial_spec.model_copy(update={'platform_tool_profile':'unknown'}))
    elif case=='capability':
        fixture['context']=replace(ctx,initial_spec=ctx.initial_spec.model_copy(update={'capability_ids':('skill',)}))
    else:
        fixture['context']=replace(ctx,input_messages=(ProductInputMessage('message',
            {'text':'hello',case:[{'path':'input/a'}]}),))
    with pytest.raises(AgentAssemblyError):
        await assembler.prepare_start(OWNER,None,LocalGuard())
    assert not workspace._bindings


async def test_resume_without_local_binding_and_cross_session_snapshot_are_rejected(assembly):
    assembler,fixture,workspace=assembly
    snapshot=CoreSnapshot(state=AgentState(session_id='foreign').model_dump(mode='json'))
    with pytest.raises(AgentAssemblyError):
        await assembler.prepare_start(OWNER,snapshot,LocalGuard())
    snapshot=CoreSnapshot(state=AgentState(session_id='session').model_dump(mode='json'))
    with pytest.raises(AgentAssemblyError):
        await assembler.prepare_resume(OWNER,snapshot,LocalGuard())


async def test_cleanup_ignores_revocation_and_never_constructs_or_invokes_real_model(assembly):
    assembler,fixture,workspace=assembly
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    snapshot=prepared.engine.snapshot()
    fixture['context']=RuntimeError('revoked')
    guard=StopGuard(None,OWNER,'stop')
    guard.reply_id=snapshot.to_state().reply_id
    cleanup=await assembler.prepare_cleanup(OWNER,snapshot,guard)
    assert cleanup.snapshot()==snapshot
    assert len(fixture['models'])==1
    with pytest.raises(AgentAssemblyError):
        _=[e async for e in cleanup.reply_stream(UserMsg(name='user',content='new work'))]
    with pytest.raises(AgentAssemblyError):
        await cleanup.agent.model([],stream=True)


async def test_live_authority_is_rechecked_before_model_and_acting(assembly):
    assembler,fixture,workspace=assembly
    guard=LocalGuard()
    prepared=await assembler.prepare_start(OWNER,None,guard)
    guard.reply_id='reply'
    from workmate_runtime.assembly.agent import _CurrentAuthority
    from workmate_runtime.engines.agentscope.tool_output import ToolOutputCharacters
    acting = prepared.engine.agent._acting_middlewares
    assert isinstance(acting[0], ToolOutputCharacters)
    authority = next(item for item in acting if isinstance(item, _CurrentAuthority))
    fixture['context']=replace(fixture['context'],effective_tool_policy=ToolPolicy(denied_tools=('present_files',)))
    called=[]
    async def action(**kwargs):
        called.append(True)
        if False:
            yield
    with pytest.raises(AgentAssemblyError,match='revoked'):
        _=[e async for e in authority.on_acting(prepared.engine.agent,
            {'tool_call':ToolCallBlock(id='call',name='present_files',input='{"files":[]}')},action)]
    assert called==[] and guard.failure is not None


async def test_parked_cleanup_drains_real_core_after_access_is_revoked(assembly,monkeypatch):
    import workmate_runtime.assembly.agent as module
    assembler,fixture,workspace=assembly
    ctx=fixture['context']
    fixture['context']=replace(ctx,initial_spec=ctx.initial_spec.model_copy(update={'approval_profile':'strict'}))
    def model(*args):
        result=ScriptModel([[ToolCallBlock(id='read-call',name='Bash',input='{"command":"touch /workspace/output/fixture"}')]])
        fixture['models'].append(result)
        return result
    monkeypatch.setattr(module,'build_selected_model',model)
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    events=[e async for e in prepared.engine.reply_stream(prepared.inputs)]
    assert any(e.type=='REQUIRE_USER_CONFIRM' for e in events)
    snapshot=prepared.engine.snapshot()
    fixture['context']=RuntimeError('revoked')
    guard=StopGuard(None,OWNER,'stop')
    guard.reply_id=snapshot.to_state().reply_id
    cleanup=await assembler.prepare_cleanup(OWNER,snapshot,guard)
    assert cleanup.snapshot()==snapshot
    events=[e async for e in cleanup.reply_stream(UserInterruptEvent(reply_id=guard.reply_id))]
    assert events[-1].type=='REPLY_END' and events[-1].finished_reason=='interrupted'
    assert not cleanup.agent.state.has_awaiting_tool_calls('agent')
    assert len(fixture['models'])==1


async def test_present_files_uses_only_bound_task_backend(assembly,monkeypatch):
    from unittest.mock import AsyncMock
    assembler,fixture,workspace=assembly
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    backend=prepared.engine.agent.offloader.get_backend()
    monkeypatch.setattr(backend,'file_exists',AsyncMock(return_value=True))
    monkeypatch.setattr(backend,'is_dir',AsyncMock(return_value=False))
    write=AsyncMock()
    monkeypatch.setattr(backend,'write_file',write)
    result=[e async for e in prepared.engine.agent.toolkit.call_tool(
        ToolCallBlock(id='presentation',name='present_files',input='{"files":["output/report.txt"]}'),prepared.engine.agent.state)]
    assert result
    write.assert_awaited_once()
    assert write.call_args.args[0]=='/workspace/.workmate/presented_files.json'


def test_core_assembler_imports_without_app_or_web_backend():
    import os
    from pathlib import Path
    import subprocess
    import sys
    root=Path(__file__).resolve().parents[2]
    env={**os.environ,'PYTHONPATH':os.pathsep.join(str(root/p) for p in
        ('runtime','packages/product/src','packages/contracts/src'))}
    code='''
import sys
from importlib.abc import MetaPathFinder
class Reject(MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='agentscope.app' or fullname.startswith('agentscope.app.') or fullname.split('.')[0] in {'fastapi','app','backend'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0,Reject())
from workmate_runtime.assembly.agent import WorkerAgentAssembler
'''
    result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


async def test_resume_confirmation_cannot_override_new_current_deny(assembly,monkeypatch):
    from agentscope.event import ConfirmResult,UserConfirmResultEvent
    import workmate_runtime.assembly.agent as module
    assembler,fixture,workspace=assembly
    ctx=fixture['context']
    fixture['context']=replace(ctx,initial_spec=ctx.initial_spec.model_copy(update={'approval_profile':'strict'}))
    responses=iter([
        [[ToolCallBlock(id='shell',name='Bash',input='{"command":"touch /workspace/output/fixture"}')]],
        [[TextBlock(text='The action is no longer permitted')]],
    ])
    monkeypatch.setattr(module,'build_selected_model',lambda *_:ScriptModel(next(responses)))
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    events=[e async for e in prepared.engine.reply_stream(prepared.inputs)]
    assert any(e.type=='REQUIRE_USER_CONFIRM' for e in events)
    snapshot=prepared.engine.snapshot()
    original_call=snapshot.to_state().get_awaiting_tool_calls('agent')[0]
    fixture['context']=replace(fixture['context'],effective_tool_policy=ToolPolicy(denied_tools=('Bash',)))
    guard=LocalGuard()
    guard.reply_id=snapshot.to_state().reply_id
    resumed=await assembler.prepare_resume(OWNER,snapshot,guard)
    assert resumed.snapshot()==snapshot
    events=[e async for e in resumed.reply_stream(UserConfirmResultEvent(reply_id=guard.reply_id,
        confirm_results=[ConfirmResult(confirmed=True,tool_call=original_call)]))]
    assert any(e.type=='TOOL_RESULT_END' and e.state=='denied' for e in events)
    assert events[-1].type=='REPLY_END'
    assert workspace.resource_registry._scope(OWNER).resources=={}


async def test_start_materializes_once_and_resume_does_not_replay_inputs(assembly,monkeypatch):
    import workmate_runtime.assembly.attachments as module
    assembler,fixture,workspace=assembly
    original=module.materialize_start_attachments
    calls=[]
    async def materialize(*args,**kwargs):
        calls.append(args[0].task_id)
        return await original(*args,**kwargs)
    monkeypatch.setattr(module,'materialize_start_attachments',materialize)
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    await assembler.prepare_resume(OWNER,prepared.engine.snapshot(),LocalGuard())
    assert calls==['task']


async def test_preparation_cleanup_can_prove_known_cancelled_prebinding_has_no_task_resources(assembly,monkeypatch):
    import asyncio
    import workmate_runtime.assembly.agent as module
    assembler,fixture,workspace=assembly
    assert await assembler.verify_preparation_cleanup(OWNER) is False
    entered=asyncio.Event()
    async def pending(*args,**kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(module,'read_execution_context',pending)
    task=asyncio.create_task(assembler.prepare_start(OWNER,None,LocalGuard()))
    await entered.wait()
    assert await assembler.verify_preparation_cleanup(OWNER) is False
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert not workspace._bindings
    assert await assembler.verify_preparation_cleanup(OWNER) is True


async def test_preparation_cleanup_of_bound_owner_uses_real_registry(assembly):
    assembler,fixture,workspace=assembly
    await assembler.prepare_start(OWNER,None,LocalGuard())
    assert await assembler.verify_preparation_cleanup(OWNER) is True
    assert workspace.resource_registry._scope(OWNER).sealed is True


async def test_delivery_baseline_is_captured_once_before_core_advances(assembly):
    assembler,fixture,workspace=assembly
    calls=[]
    class Delivery:
        async def prepare(self,owner,binding):
            assert owner==OWNER and binding.owner==OWNER
            calls.append(owner)
    assembler.delivery_coordinator=Delivery()
    first=await assembler.prepare_start(OWNER,None,LocalGuard())
    assert calls==[OWNER]
    await assembler.prepare_resume(OWNER,first.engine.snapshot(),LocalGuard())
    assert calls==[OWNER]


async def test_resume_applies_durable_upgrade_only_when_core_advances(assembly):
    assembler,fixture,workspace=assembly
    guard=LocalGuard()
    await assembler.prepare_start(OWNER,None,guard)
    original=CoreSnapshot(state=AgentState(session_id='session',reply_id='reply').model_dump(mode='json'))
    fixture['context']=replace(fixture['context'],effective_approval_profile='full_access',permission_version='upgrade')
    resumed=await assembler.prepare_resume(OWNER,original,guard)
    assert resumed.snapshot()==original
    policy=resumed.agent._check_permission_middlewares[0]._policy
    assert policy.approval_profile=='standard'
    configuration=resumed.agent._reply_middlewares[-2]
    assert configuration.mode is PermissionMode.BYPASS
    assert policy.denied_tools==['current','original']
    assert fixture['context'].initial_spec.approval_profile=='standard'


async def test_full_access_mode_keeps_initial_read_only_platform_write_denial():
    from workmate_runtime.assembly.agent import _ExecutionConfiguration
    from workmate_runtime.permission_policy import ProductPermissionMiddleware,ExecutionPermissionPolicy
    from test_core_engine import ConfirmTool,make_engine
    tool=ConfirmTool()
    tool.name='agent_create'
    engine=make_engine([[ToolCallBlock(id='write',name=tool.name,input='{}')],[TextBlock(text='done')]],tool=tool,
        middlewares=[ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id='task',approval_profile='read_only')),
            _ExecutionConfiguration('full_access','/workspace')])
    events=[event async for event in engine.reply_stream(UserMsg(name='user',content='do work'))]
    assert tool.calls==0
    assert any(event.type=='TOOL_RESULT_END' and event.state=='denied' for event in events)
    assert engine.agent.state.permission_context.mode is PermissionMode.DEFAULT


async def test_preparation_reports_stages_without_inputs(assembly, caplog):
    import logging
    assembler, fixture, workspace = assembly
    with caplog.at_level(logging.INFO, logger='workmate_runtime.observability.preparation'):
        await assembler.prepare_start(OWNER, None, LocalGuard())
    records = [record for record in caplog.records
               if record.name == 'workmate_runtime.observability.preparation']
    stages = {record.stage for record in records}
    assert {'total', 'context', 'model', 'workspace', 'sandbox_attestation',
            'skills', 'attachments', 'mcp'} <= stages
    assert records[-1].stage == 'total'
    assert all(record.outcome == 'completed' and record.task_id == OWNER.task_id
               and record.duration_ms >= 0 for record in records)
    assert 'Solve the task' not in caplog.text and 'hello' not in caplog.text


async def test_missing_worker_model_credential_is_joined_before_resource_creation(assembly, monkeypatch):
    from workmate_runtime.assembly.models import build_selected_model, ModelUnavailable
    from test_worker_models import settings
    assembler, fixture, workspace = assembly
    assembler.settings = settings().model_copy(update={'reme_enabled':False})
    profile = assembler.settings.chat_model_profiles['chosen']
    selected = SelectedModel(profile_name=profile.name,
        **profile.model_dump(exclude={'name','base_url'}))
    fixture['context'] = replace(fixture['context'], initial_spec=fixture['context'].initial_spec.model_copy(
        update={'selected_model':selected}))
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model', build_selected_model)
    monkeypatch.delenv('WORKMATE_FIXTURE_MODEL_KEY', raising=False)
    with pytest.raises(ModelUnavailable, match='credential is unavailable'):
        await assembler.prepare_start(OWNER, None, LocalGuard())
    assert OWNER not in workspace.resource_registry._scopes
    assert await assembler.verify_preparation_cleanup(OWNER) is True
