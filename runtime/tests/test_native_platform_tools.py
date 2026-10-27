"""Worker native tools bind exact Core acting identity, never runtimeAgentId."""
from types import SimpleNamespace
import pytest
from agentscope.message import ToolCallBlock,ToolCallState
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.assembly.platform_tools import NativePlatformTools,PlatformCallBinding
from test_worker_agent_assembly import assembly


async def test_platform_adapter_requires_current_acting_identity():
    async def check(): pass
    guard=SimpleNamespace(owner=ExecutionOwner('s','t','worker',1),reply_id='reply',node_id='node',check=check)
    context=SimpleNamespace(session_id='s',task_id='t',agent_id='a',actor_user_id='u',conversation_id='c')
    adapter=NativePlatformTools(None,context,guard,None)
    with pytest.raises(RuntimeError,match='acting'):
        await adapter.mutate('agent_create',{'slug':'valid','display_name':'Valid'})


async def test_binding_scopes_exact_tool_call_and_rejects_input_substitution(monkeypatch):
    import workmate_runtime.assembly.platform_tools as module
    async def check(): pass
    guard=SimpleNamespace(owner=ExecutionOwner('s','t','worker',1),reply_id='reply',node_id='node',check=check)
    context=SimpleNamespace(session_id='s',task_id='t',agent_id='a',actor_user_id='u',conversation_id='c')
    adapter=NativePlatformTools(None,context,guard,None)
    middleware=PlatformCallBinding(adapter)
    agent=SimpleNamespace(state=SimpleNamespace(session_id='s',reply_id='reply'))
    call=ToolCallBlock(id='exact',name='agent_create',input='{"slug":"valid","display_name":"Valid"}',state=ToolCallState.ALLOWED)
    async def next_handler(**kwargs):
        with pytest.raises(RuntimeError,match='input'):
            await adapter.mutate('agent_create',{'slug':'changed','display_name':'Valid'})
        yield 'done'
    assert [item async for item in middleware.on_acting(agent,{'tool_call':call},next_handler)]==['done']
    with pytest.raises(RuntimeError,match='acting'):
        await adapter.mutate('agent_create',{'slug':'valid','display_name':'Valid'})


def test_native_adapter_imports_without_fastapi_or_agentscope_app():
    import subprocess,sys,os
    result=subprocess.run([sys.executable,'-c','''
import sys,importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in {'fastapi','app','workmate_runtime.platform_tools'} or fullname.startswith(('fastapi.','app.','agentscope.app')):
            raise AssertionError('forbidden dependency '+fullname)
sys.meta_path.insert(0,Block())
from workmate_runtime.assembly.platform_tools import NativePlatformTools
from workmate_product.application.platform_tools import execute_platform_mutation
'''],capture_output=True,text=True,env=os.environ,timeout=30)
    assert result.returncode==0,result.stderr


async def test_workspace_profile_assembles_complete_native_tools(assembly):
    from dataclasses import replace
    from test_worker_agent_assembly import OWNER,LocalGuard
    from workmate_runtime.assembly.platform_tools import PlatformCallBinding
    assembler,fixture,workspace=assembly
    old=fixture['context']
    fixture['context']=replace(old,platform_tool_profile='workspace',initial_spec=old.initial_spec.model_copy(update={'platform_tool_profile':'workspace'}))
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    names={name for name in NativePlatformTools.schemas if await prepared.engine.agent.toolkit.get_tool(name) is not None}
    assert names==set(NativePlatformTools.schemas)
    assert any(isinstance(m,PlatformCallBinding) for m in prepared.engine.agent._acting_middlewares)


def test_model_read_view_excludes_provider_and_credential_values(monkeypatch):
    from workmate_runtime.config import RuntimeSettings,ChatModelProfile
    from workmate_runtime.assembly.platform_tools import public_model_profiles
    monkeypatch.setenv('FIXTURE_KEY','example-secret')
    settings=RuntimeSettings(default_chat_profile='office',chat_model_profiles={'office':ChatModelProfile(
        name='office',model_type='openai',model='private-provider-model',api_key_env='FIXTURE_KEY',
        base_url='https://private-provider.example',media_profile='vision',model_input_types=['text/plain','image/*'],
        pdf_strategy='native',supports_thinking=True)})
    assert public_model_profiles(settings)==[{'id':'office','display_name':'office','media_profile':'vision',
        'supports_multimodal_input':True,'supports_images':True,'supports_audio':False,'supports_video':False,
        'supports_documents':True,'supports_native_pdf':True,'supports_thinking':True,'is_default':True,'available':True}]


@pytest.mark.asyncio
async def test_native_permission_denial_is_conversational_only_after_fenced_submission(monkeypatch):
    from contextlib import asynccontextmanager
    from workmate_runtime.assembly import platform_tools as module
    from workmate_runtime.assembly.platform_tools import ActingCall
    from workmate_product.application.platform_types import PlatformToolError
    async def check(): pass
    guard=SimpleNamespace(owner=ExecutionOwner('s','t','worker',1),reply_id='reply',node_id='node',check=check)
    context=SimpleNamespace(session_id='s',task_id='t',agent_id='a',actor_user_id='u',conversation_id='c')
    @asynccontextmanager
    async def transaction(): yield object()
    adapter=NativePlatformTools(SimpleNamespace(begin=transaction),context,guard,None)
    call=ActingCall('call','agent_create',{},'reply')
    async def denied(*args,**kwargs): return {'error':True,'status_code':403,'detail':'permission denied'}
    monkeypatch.setattr(module,'execute_platform_mutation',denied)
    result=await adapter._execute(call,'agent_create',{})
    assert result['ok'] is False and result['error']['code']=='permission_denied'
    async def stale(*args,**kwargs): raise PlatformToolError(409,'stale owner')
    monkeypatch.setattr(module,'execute_platform_mutation',stale)
    with pytest.raises(PlatformToolError,match='stale owner'):
        await adapter._execute(call,'agent_create',{})
    async def no_approval(*args,**kwargs): raise PlatformToolError(403,'Explicit submission confirmation required')
    monkeypatch.setattr(module,'execute_platform_mutation',no_approval)
    with pytest.raises(PlatformToolError,match='confirmation'):
        await adapter._execute(call,'agent_create',{})


async def test_normal_agent_has_only_profile_tools_from_platform_catalog(assembly):
    from test_worker_agent_assembly import OWNER, LocalGuard
    assembler, fixture, workspace = assembly
    prepared = await assembler.prepare_start(OWNER, None, LocalGuard())
    toolkit = prepared.engine.agent.toolkit
    assert await toolkit.get_tool('get_current_user') is not None
    assert await toolkit.get_tool('list_scope_users') is not None
    assert await toolkit.get_tool('agent_create') is None


@pytest.mark.parametrize('status,code',[(403,'permission_denied'),(404,'not_found'),(422,'invalid_arguments')])
async def test_profile_read_denial_is_recoverable_and_does_not_expose_detail(monkeypatch,status,code):
    from contextlib import asynccontextmanager
    from workmate_product.application.platform_types import PlatformToolError
    from workmate_runtime.assembly.platform_tools import ActingCall
    import workmate_runtime.assembly.platform_tools as module
    @asynccontextmanager
    async def transaction(): yield None
    async def check(): pass
    async def rejected(*args,**kwargs): raise PlatformToolError(status,'private identity details')
    monkeypatch.setattr(module,'execute_platform_read',rejected)
    guard=SimpleNamespace(owner=ExecutionOwner('s','t','worker',1),reply_id='reply',node_id='node',check=check)
    context=SimpleNamespace(session_id='s',task_id='t',agent_id='a',actor_user_id='u',conversation_id='c')
    adapter=NativePlatformTools(SimpleNamespace(begin=transaction),context,guard,None)
    args={'scope_id':'dept'}
    token=adapter.current.set(ActingCall('call','list_scope_users',args,'reply'))
    try:
        result=await adapter.read('list_scope_users',args)
    finally:
        adapter.current.reset(token)
    assert result['ok'] is False and result['error']['code']==code
    assert 'private identity' not in str(result)
