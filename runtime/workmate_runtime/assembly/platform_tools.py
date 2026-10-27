"""Native Core platform mutations with task-local exact acting-call identity."""
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
import os

from agentscope.message import ToolCallState
from agentscope._utils._common import _json_loads_with_repair
from agentscope.middleware import MiddlewareBase
from agentscope.tool import FunctionTool
from workmate_product.application.platform_types import PlatformToolRequest
from workmate_product.application.platform_tools import execute_platform_mutation
from workmate_product.application.platform_reads import execute_platform_read
from workmate_product.application.user_profiles import USER_PROFILE_TOOLS, ListScopeUsersInput
from workmate_product.application.platform_types import PlatformToolError,_AgentCreateArgs,_AgentUpdateArgs
from workmate_runtime.platform_tool_schemas import (AgentCreateInput,AgentUpdateInput,SkillInstallInput,SkillValidateInput,
    EmptyInput,CapabilityListInput,AgentListInput,AgentGetInput,AgentValidateInput)


@dataclass(frozen=True)
class ActingCall:
    call_id:str
    tool_name:str
    arguments:dict
    reply_id:str


class PlatformCallBinding(MiddlewareBase):
    """Bind the exact Core ToolCallBlock; never search state by tool name."""
    def __init__(self,adapter):
        self.adapter=adapter

    async def on_acting(self,agent,input_kwargs,next_handler):
        call=input_kwargs['tool_call']
        if call.name not in self.adapter.schemas:
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream:
                    yield event
            return
        owner=self.adapter.guard.owner
        if (agent.state.session_id!=owner.session_id or agent.state.reply_id!=self.adapter.guard.reply_id
                or call.state!=ToolCallState.ALLOWED):
            raise RuntimeError('Platform acting context is stale')
        arguments=_json_loads_with_repair(call.input,self.adapter.schemas[call.name].model_json_schema())
        if not isinstance(arguments,dict):
            raise RuntimeError('Platform acting input is not an object')
        token=self.adapter.current.set(ActingCall(call.id,call.name,arguments,agent.state.reply_id))
        try:
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream:
                    yield event
        finally:
            self.adapter.current.reset(token)


class NativePlatformTools:
    """Trusted assembler supplies Product context, live guard and Task binding.

    Returned tools require the paired middleware in the same Core Agent. All
    Product reads and mutations use shared services; Skill packaging uses only
    the tracked Task backend.
    """
    schemas={'platform_get_context':EmptyInput,'model_list':EmptyInput,'capability_list':CapabilityListInput,
        'agent_list':AgentListInput,'agent_get':AgentGetInput,'agent_validate':AgentValidateInput,
        'agent_create':AgentCreateInput,'agent_update':AgentUpdateInput,'skill_install':SkillInstallInput,
        'skill_validate':SkillValidateInput,'get_current_user':EmptyInput,'list_scope_users':ListScopeUsersInput}

    def __init__(self,sessions,context,guard,binding,*,knowledge_enabled=False,model_profiles=(),storage_node_id=None):
        if (context.task_id!=guard.owner.task_id or context.session_id!=guard.owner.session_id
                or (binding is not None and binding.owner!=guard.owner)):
            raise RuntimeError('Platform adapter requires matching Task ownership')
        self.storage_node_id=storage_node_id or getattr(guard,'node_id',None)
        if (not isinstance(self.storage_node_id,str) or not self.storage_node_id.strip()
                or getattr(guard,'node_id',self.storage_node_id)!=self.storage_node_id):
            raise RuntimeError('Platform adapter requires its trusted storage node identity')
        self.sessions,self.context,self.guard,self.binding=sessions,context,guard,binding
        self.knowledge_enabled,self.model_profiles=knowledge_enabled,tuple(model_profiles)
        self.current=ContextVar('workmate_platform_acting',default=None)

    def _call(self,name,arguments,schema):
        call=self.current.get()
        if call is None or call.tool_name!=name or call.reply_id!=self.guard.reply_id:
            raise RuntimeError('Platform tool requires its current Core acting identity')
        if schema.model_validate(call.arguments)!=schema.model_validate(arguments):
            raise RuntimeError('Platform tool input differs from its original Core call')
        return call

    async def _execute(self,call,name,arguments,*,mutation=True,skill_store=None):
        await self.guard.check()
        owner=self.guard.owner
        request=PlatformToolRequest(actor_user_id=self.context.actor_user_id,agent_id=self.context.agent_id,
            conversation_id=self.context.conversation_id,session_id=owner.session_id,task_id=owner.task_id,
            worker_id=owner.worker_id,storage_node_id=self.storage_node_id,execution_epoch=owner.execution_epoch,
            reply_id=call.reply_id,tool_call_id=call.call_id,tool_name=name,arguments=arguments)
        async with self.sessions.begin() as db:
            if skill_store is not None:
                db.info['skill_blob_store'] = skill_store
            if mutation:
                result=await execute_platform_mutation(db,request,knowledge_enabled=self.knowledge_enabled)
            else:
                try:
                    result=await execute_platform_read(db,request,model_profiles=self.model_profiles,
                        knowledge_enabled=self.knowledge_enabled)
                except PlatformToolError as error:
                    if name not in USER_PROFILE_TOOLS or error.status_code not in {403,404,422}:
                        raise
                    return {'ok':False,'error':{'code':{403:'permission_denied',404:'not_found',422:'invalid_arguments'}[error.status_code],
                        'message':'无法读取用户资料，请检查查询范围、参数和当前权限。',
                        'guidance':'Use get_current_user to discover readable_scopes. Ask before changing the requested scope.'}}
        # Failed business outcome and conflict budget survive this exception.
        if mutation and result.get('error') is True:
            if result['status_code']==403:
                return {'ok':False,'error':{'code':'permission_denied','status':403,
                    'message':'当前用户没有执行该操作所需的权限。',
                    'guidance':'Explain the denial and query platform_get_context for allowed scopes. Ask before changing scope; changed submissions require fresh validation and approval.'}}
            raise PlatformToolError(result['status_code'],result['detail'])
        return result

    async def mutate(self,name,arguments):
        schemas={'agent_create':_AgentCreateArgs,'agent_update':_AgentUpdateArgs}
        if name not in schemas:
            raise RuntimeError('Unsupported native Agent mutation')
        call=self._call(name,arguments,schemas[name])
        return await self._execute(call,name,arguments)

    async def read(self,name,arguments):
        if name not in USER_PROFILE_TOOLS | {'platform_get_context','model_list','capability_list','agent_list','agent_get','agent_validate'}:
            raise RuntimeError('Unknown native read tool')
        call=self._call(name,arguments,self.schemas[name])
        return await self._execute(call,name,arguments,mutation=False)

    async def skill_validate(self,skill_directory,_agent_state=None):
        from .platform_skill_package import package_skill
        await self.guard.check()
        _,summary=await package_skill(self.binding,skill_directory)
        await self.guard.check()
        return summary

    async def skill_install(self,_agent_state=None,**arguments):
        import base64
        from .platform_skill_package import package_skill
        call=self._call('skill_install',arguments,SkillInstallInput)
        args=SkillInstallInput.model_validate(arguments)
        await self.guard.check()
        archive,summary=await package_skill(self.binding,args.skill_directory)
        if summary['sha256']!=args.expected_sha256:
            raise RuntimeError('Skill draft changed after validation')
        from workmate_product.skill_storage import PreparedSkillBlobStore, get_skill_blob_store
        # No business locks while uploading. The guarded mutation below still
        # rechecks owner, approval, actor and Scope before publishing any pointer.
        async with self.sessions() as db:
            prepared = PreparedSkillBlobStore(get_skill_blob_store(db))
        await prepared.prepare(archive)
        return await self._execute(call,'skill_install',{'scope_id':args.scope_id,
            'filename':f"{summary['name']}.zip",'archive_base64':base64.b64encode(archive).decode('ascii'),
            'archive_sha256':summary['sha256'],'display_name':args.display_name,'description':args.description},
            skill_store=prepared)

    def tools(self, *, profile_only=False):
        def mutation(name,schema,description):
            async def invoke(_agent_state=None,**arguments):
                return await self.mutate(name,arguments)
            return FunctionTool(invoke,name=name,input_schema=schema,description=description,
                is_read_only=False,is_concurrency_safe=False,is_state_injected=True)
        def read(name,description):
            async def invoke(_agent_state=None,**arguments):
                return await self.read(name,arguments)
            return FunctionTool(invoke,name=name,input_schema=self.schemas[name],description=description,is_read_only=True,is_state_injected=True)
        profiles = [read('get_current_user','Read the current authenticated user basic profile and readable scopes. No user ID input. This is not an external identity credential.'),
            read('list_scope_users','List basic user profiles in an explicitly selected authorized scope. Use get_current_user for readable scopes; never infer authority from Agent ownership. Supports bounded search and pagination.')]
        if profile_only:
            return profiles
        return [*profiles,read('platform_get_context','List the current Product scope context.'),
            read('model_list','List configured model profiles.'),
            read('capability_list','List Skills and MCPs bindable to an Agent.'),
            read('agent_list','List Agents visible to the current user.'),
            read('agent_get','Get one visible Agent and its current version.'),
            read('agent_validate','Validate an Agent create or edit draft.'),
            mutation('agent_create',AgentCreateInput,'Create an Agent with governed capability bindings.'),
            mutation('agent_update',AgentUpdateInput,'Update an Agent using its exact Product version.'),
            FunctionTool(self.skill_validate,name='skill_validate',input_schema=SkillValidateInput,is_read_only=True,is_state_injected=True),
            FunctionTool(self.skill_install,name='skill_install',input_schema=SkillInstallInput,
                is_read_only=False,is_concurrency_safe=False,is_state_injected=True)]


def public_model_profiles(settings):
    """Same Product metadata as /models; never serialize model/provider/secrets."""
    result=[]
    for name,profile in sorted(settings.chat_model_profiles.items()):
        types=profile.model_input_types
        supports=lambda kind:any(t.startswith(kind+'/') for t in types)
        result.append({'id':name,'display_name':name,'media_profile':profile.media_profile,
            'supports_multimodal_input':any(t!='text/plain' for t in types) or profile.pdf_strategy=='native',
            'supports_images':supports('image'),'supports_audio':supports('audio'),'supports_video':supports('video'),
            'supports_documents':True,'supports_native_pdf':profile.pdf_strategy=='native',
            'supports_thinking':profile.supports_thinking,'is_default':name==settings.default_chat_profile,
            'available':bool(os.getenv(profile.api_key_env,''))})
    return result
