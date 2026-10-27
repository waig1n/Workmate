"""Concrete Core assembly for the currently supported Worker capability subset.

This component is not the production role entrypoint. Configured capabilities
without a Worker adapter fail explicitly; no App factory or empty replacement is
installed. Start materializes Product inputs once, Resume restores exact state,
and cleanup uses a local binding or a verified cold claim after authority revocation.
"""
from contextlib import aclosing
from dataclasses import dataclass, replace

from agentscope.agent import Agent
from agentscope.credential import CredentialBase
from agentscope.event import UserInterruptEvent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.model import ChatModelBase
from agentscope.permission import AdditionalWorkingDirectory,PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from pydantic import BaseModel

from workmate_product.application.execution_context import read_execution_context
from workmate_product.prompts import finalize_system_prompt
from workmate_contracts import snapshot_digest
from workmate_runtime.artifact_delivery import make_present_files_tool_factory
from workmate_runtime.engines.agentscope import CoreEngine,CoreSnapshot
from workmate_runtime.engines.agentscope.tool_output import ToolOutputCharacters
from workmate_runtime.execution.controller import ExecutionAborted,ExecutionGuard,PreparedExecution
from workmate_runtime.execution.stopping import StopGuard
from workmate_runtime.permission_policy import ExecutionPermissionPolicy,ProductPermissionMiddleware,_tool_group
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.web_tools import make_web_tool_factory
from workmate_runtime.observability.preparation import preparation_stage
from .models import build_selected_model
from .workspaces import WorkspaceIdentity,WorkerWorkspaceManager


class AgentAssemblyError(ExecutionAborted):
    """A complete, currently authorized Core assembly cannot be constructed."""


_MODES = {'standard':PermissionMode.ACCEPT_EDITS,'strict':PermissionMode.DEFAULT,
          'read_only':PermissionMode.EXPLORE,'full_access':PermissionMode.BYPASS,
          'unattended':PermissionMode.DONT_ASK}


def _policy(context):
    return ExecutionPermissionPolicy(task_id=context.task_id,
        approval_profile=context.initial_spec.approval_profile,
        denied_groups=list(context.effective_tool_policy.denied_groups),
        denied_tools=list(context.effective_tool_policy.denied_tools))


class _ExecutionConfiguration(MiddlewareBase):
    """Apply the Task profile only when Core actually advances, after assembly.

    Constructing Resume must preserve the complete snapshot byte-for-byte. The
    permission engine references this same context object, so no stale engine is
    created by applying its mode at the beginning of reply execution.
    """
    def __init__(self,profile,workdir):
        self.mode,self.workdir = _MODES[profile],workdir

    async def on_reply(self,agent,input_kwargs,next_handler):
        agent.state.permission_context.mode = self.mode
        agent.state.permission_context.working_directories = {
            self.workdir:AdditionalWorkingDirectory(path=self.workdir,source='session')}
        try:
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream:
                    yield event
        finally:
            if self.mode is PermissionMode.BYPASS and not agent.state.has_awaiting_tool_calls(agent.name):
                agent.state.permission_context.mode=PermissionMode.DEFAULT


class _CurrentAuthority(ProductPermissionMiddleware):
    def __init__(self,assembler,owner,guard,context):
        super().__init__(_policy(context))
        self.assembler,self.owner,self.guard,self.original_spec = assembler,owner,guard,context.initial_spec
        self.permission_version=context.permission_version

    async def _current(self):
        # Running Stop must get a chance to cancel Core before a Product
        # cancelling status makes ordinary authorization reads fail.
        await self.guard.check()
        try:
            context = await self.assembler._context(self.owner)
            if context.initial_spec != self.original_spec or context.permission_version!=self.permission_version:
                raise AgentAssemblyError('Initial execution descriptor changed')
            self.assembler._supported(context)
            return _policy(context)
        except Exception as error:
            # Stop may have committed after the first check and made the
            # ordinary Product read reject cancelling. Let RunningGuard claim
            # it before turning the read failure into an orphaned execution.
            await self.guard.check()
            self.guard.failure = error
            raise

    async def on_check_permission(self,agent,input_kwargs,next_handler):
        policy = await self._current()
        return await ProductPermissionMiddleware(policy).on_check_permission(agent,input_kwargs,next_handler)

    async def on_model_call(self,agent,input_kwargs,next_handler):
        await self._current()
        return await next_handler(**input_kwargs)

    async def on_compress_context(self,agent,input_kwargs,next_handler):
        await self._current()
        return await next_handler(**input_kwargs)

    async def on_acting(self,agent,input_kwargs,next_handler):
        policy = await self._current()
        tool = await agent.toolkit.get_tool(input_kwargs['tool_call'].name)
        if tool is None:
            raise AgentAssemblyError('The admitted tool is no longer assembled')
        if (tool.name.strip().casefold() in {n.strip().casefold() for n in policy.denied_tools}
                or _tool_group(tool) in policy.denied_groups):
            error = AgentAssemblyError('Current policy revoked the admitted tool')
            self.guard.failure = error
            raise error
        async with aclosing(next_handler(**input_kwargs)) as stream:
            async for event in stream:
                yield event


class _NoModelParameters(BaseModel):
    pass


class _NoExecutionModel(ChatModelBase):
    """Cleanup can never resolve a credential or call a provider."""
    def __init__(self):
        super().__init__(CredentialBase(),'cleanup-disabled',_NoModelParameters(),stream=True)
        self.formatter = OpenAIChatFormatter()

    async def __call__(self,*args,**kwargs):
        raise AgentAssemblyError('Cleanup cannot invoke a model')

    async def _call_api(self,*args,**kwargs):
        raise AgentAssemblyError('Cleanup cannot invoke a model')

    async def generate_structured_output(self,*args,**kwargs):
        raise AgentAssemblyError('Cleanup cannot invoke a model')


class _CleanupEngine(CoreEngine):
    async def reply_stream(self,inputs=None):
        if not isinstance(inputs,UserInterruptEvent) or inputs.reply_id != self.agent.state.reply_id:
            raise AgentAssemblyError('Cleanup accepts only interruption of its exact Reply')
        async with aclosing(super().reply_stream(inputs)) as stream:
            async for event in stream:
                yield event


@dataclass(frozen=True)
class _BoundAssembly:
    identity: WorkspaceIdentity
    binding: object
    policy: ExecutionPermissionPolicy
    agent_name: str
    system_prompt: str


class _TaskWorkspaceView:
    """The presentation factory sees the Task backend, never gateway resources."""
    def __init__(self,binding):
        self.workdir,self._backend = binding.workspace.workdir,binding.backend
    def get_backend(self):
        return self._backend


class WorkerAgentAssembler:
    def __init__(self,sessions,settings,workspace_manager,*,artifact_storage_dir=None,artifact_object_store=None,delivery_coordinator=None,knowledge_resolver=None,memory_manager=None):
        if not isinstance(workspace_manager,WorkerWorkspaceManager):
            raise TypeError('A Worker-owned Task workspace manager is required')
        self.sessions,self.settings,self.workspaces = sessions,settings,workspace_manager
        self.artifact_storage_dir = artifact_storage_dir
        self.artifact_object_store = artifact_object_store
        self.delivery_coordinator = delivery_coordinator
        self.knowledge_resolver = knowledge_resolver
        self.memory_manager = memory_manager
        from pathlib import Path
        from .skill_cache import SkillDirectoryCache
        cache_path = Path(settings.skill_cache_dir).absolute()
        workspace_path = Path(workspace_manager.basedir).absolute() if hasattr(workspace_manager, 'basedir') else None
        if workspace_path is not None and (cache_path.is_relative_to(workspace_path)
                                         or workspace_path.is_relative_to(cache_path)):
            raise AgentAssemblyError('Skill cache and Workspace directories must be separate')
        self.skill_cache = SkillDirectoryCache(cache_path)
        self._bound,self._assembling,self._preparation_started = {},set(),set()
        self._web_factory = make_web_tool_factory(settings)

    def identity(self,owner):
        if owner not in self._bound:
            raise AgentAssemblyError('No process-local assembly binding exists')
        return self._bound[owner].identity

    def _owner(self,owner,guard):
        if (not isinstance(owner,ExecutionOwner) or not isinstance(guard,ExecutionGuard)
                or guard.owner != owner or type(owner.execution_epoch) is not int or owner.execution_epoch<1):
            raise AgentAssemblyError('A matching claimed owner and guard are required')
        if guard.failure is not None:
            raise AgentAssemblyError('Execution guard has already failed')

    async def _context(self,owner):
        async with self.sessions.begin() as db:
            context = await read_execution_context(db,session_id=owner.session_id,task_id=owner.task_id,
                worker_id=owner.worker_id,execution_epoch=owner.execution_epoch,
                storage_node_id=self.workspaces.storage_node_id,knowledge_enabled=self.settings.knowledge.enabled)
        if context.task_id != owner.task_id or context.session_id != owner.session_id:
            raise AgentAssemblyError('Product assembly context belongs to another owner')
        return context

    async def _cold_recovery(self, owner, snapshot):
        """A missing local binding alone never authorizes fresh resources."""
        from workmate_runtime.transport.suspension import read_recovery_bridge
        from workmate_runtime.transport.product_pool import ProductConnection
        if snapshot is None or self.workspaces.provider.name not in {'bubblewrap', 'docker', 'kubernetes'}:
            return False
        async with self.sessions.begin() as db:
            if db is None:
                return False
            connection = await db.connection()
            await connection.exec_driver_sql('SELECT 1')
            raw = await connection.get_raw_connection()
            bridge = await read_recovery_bridge(ProductConnection(connection, raw.driver_connection), owner)
            if (bridge is not None and bridge['storage_node_id'] == self.workspaces.storage_node_id
                    and bridge['snapshot_digest'] == snapshot_digest(snapshot.model_dump(mode='json'))):
                from workmate_runtime.transport.suspension import CleanupReceipt
                proof = CleanupReceipt.model_validate_json(bridge['cleanup_receipt'])
                if self.workspaces.provider.name != 'bubblewrap' and proof.storage_proof is None:
                    return None
                return dict(bridge, storage_proof=proof.storage_proof)
            return None

    def _supported(self,context):
        if context.platform_tool_profile not in {"none", "workspace"}:
            raise AgentAssemblyError("Unknown platform tool profile")
        if context.platform_tool_profile != context.initial_spec.platform_tool_profile:
            raise AgentAssemblyError('Admitted platform tool profile changed')
        if self.settings.reme_enabled and self.memory_manager is None:
            raise AgentAssemblyError('Worker ReMe adapter is not connected')
        if (self.settings.knowledge.enabled or context.knowledge_config.knowledge_base_ids) and self.knowledge_resolver is None:
            raise AgentAssemblyError('Worker knowledge adapter is not connected')
        if any(cap.capability_type not in {'skill','mcp'} for cap in context.capabilities):
            raise AgentAssemblyError('Unknown Worker capability type')
        if {cap.id for cap in context.capabilities} != set(context.initial_spec.capability_ids):
            raise AgentAssemblyError('Worker capability descriptors are incomplete')

    def _state(self,owner,snapshot,agent_name,*,resume):
        if snapshot is not None and not isinstance(snapshot,CoreSnapshot):
            raise AgentAssemblyError('A validated exact Core snapshot is required')
        state = snapshot.to_state() if snapshot is not None else AgentState(session_id=owner.session_id)
        if state.session_id != owner.session_id:
            raise AgentAssemblyError('Snapshot belongs to another Session')
        if any(msg.role=='assistant' and msg.name!=agent_name for msg in state.context):
            raise AgentAssemblyError('Snapshot contains a different Core Agent identity')
        if not resume and state.has_awaiting_tool_calls(agent_name):
            raise AgentAssemblyError('Start cannot consume pending work from a previous Task')
        return state

    @staticmethod
    def _inputs(messages,user_id):
        if not messages:
            raise AgentAssemblyError('Start requires Product input messages')
        inputs = []
        for message in messages:
            content = message.content
            if (not isinstance(content,dict) or set(content)-{'text','attachments'}
                    or not isinstance(content.get('text'),str) or not content['text'].strip()):
                raise AgentAssemblyError('Unsupported Product input message format')
            inputs.append(UserMsg(id=message.id,name=user_id,content=content['text']))
        return inputs

    async def _prepare(self,owner,snapshot,guard,*,resume):
        with preparation_stage(owner, 'total', resume=resume):
            return await self._prepare_impl(owner,snapshot,guard,resume=resume)

    async def _prepare_impl(self,owner,snapshot,guard,*,resume):
        self._owner(owner,guard)
        cold = resume and owner not in self._bound
        if owner in self._assembling or (not resume and owner in self._bound):
            raise AgentAssemblyError('Start/Resume does not match the process-local assembly binding')
        if resume and not isinstance(snapshot,CoreSnapshot):
            raise AgentAssemblyError('Resume requires an exact validated snapshot')
        recovery = await self._cold_recovery(owner, snapshot) if cold else None
        if cold and not recovery:
            raise AgentAssemblyError('Cold Resume requires exact durable recovery authority')
        self._assembling.add(owner)
        if not resume or cold:
            self._preparation_started.add(owner)
        try:
            with preparation_stage(owner, 'context', resume=resume):
                context = await self._context(owner)
            self._supported(context)
            state = self._state(owner,snapshot,context.agent_id,resume=resume)
            identity = WorkspaceIdentity(context.actor_user_id,context.agent_id,context.conversation_id,
                context.session_id,self.workspaces.storage_node_id)
            if resume and not cold and self._bound[owner].identity != identity:
                raise AgentAssemblyError('Process-local workspace identity changed')
            # Validate unsupported message shapes before opening a resource scope.
            if not resume:
                for message in context.input_messages:
                    if not isinstance(message.content,dict) or set(message.content)-{'text','attachments'}:
                        raise AgentAssemblyError('Unsupported Product input message format')
            with preparation_stage(owner, 'model', resume=resume):
                model = build_selected_model(self.settings,context.initial_spec.selected_model)
            with preparation_stage(owner, 'workspace', resume=resume):
                binding = await self.workspaces.bind_task(identity,owner,fresh_claim=not resume or cold,
                    all_resource_paths_tracked=True,read_model_input_types=model.formatter.input_types,
                    expected_storage_proof=recovery.get('storage_proof') if recovery else None)
            if cold:
                self._verify_recovered_workspace(identity,recovery)
            if not resume:
                from .workspace_recovery import restore_recovery_workspace
                with preparation_stage(owner, 'file_recovery', resume=False):
                    await restore_recovery_workspace(context, binding, self.sessions,
                        self.artifact_object_store, self.settings.artifact_storage,
                        storage_node_id=self.workspaces.storage_node_id)
            if not resume and self.delivery_coordinator is not None:
                with preparation_stage(owner, 'delivery', resume=resume):
                    await self.delivery_coordinator.prepare(owner,binding)
            if cold and self.delivery_coordinator is not None:
                await self.delivery_coordinator.rebind(owner, binding)
            if not resume or cold:
                from .sandbox_proof import attest_task_sandbox
                with preparation_stage(owner, 'sandbox_attestation', resume=resume):
                    await attest_task_sandbox(binding,owner,self.sessions)
            from .skills import materialize_execution_skills
            with preparation_stage(owner, 'skills', resume=resume):
                skills = await materialize_execution_skills(context,binding,self.sessions,resume=resume,
                                                           cache=self.skill_cache)
            inputs = None
            if not resume:
                from .attachments import materialize_start_attachments
                with preparation_stage(owner, 'attachments', resume=resume):
                    messages = await materialize_start_attachments(context,binding,
                        artifact_storage_dir=self.artifact_storage_dir,object_store=self.artifact_object_store)
                    recovery_text = context.initial_spec.recovery_context_text
                    if recovery_text is not None:
                        if context.initial_spec.recovery_fork_id is None or not messages:
                            raise AgentAssemblyError('Recovery input has no frozen execution identity')
                        # Keep persisted chat messages unchanged. Only the new Core
                        # receives the bounded history and recovered-file inventory.
                        messages = (replace(messages[0], content={'text': recovery_text}),)
                    inputs = self._inputs(messages,context.actor_user_id)
            async def resolve(user_id,agent_id,session_id):
                if (user_id,agent_id,session_id)!=(identity.user_id,identity.agent_id,identity.session_id):
                    raise AgentAssemblyError('Presentation workspace identity mismatch')
                return _TaskWorkspaceView(binding)
            presentation = make_present_files_tool_factory(resolve,
                enabled=self.settings.artifact_delivery_mode=='hybrid')
            tools = [*binding.tools,*await presentation(identity.user_id,identity.agent_id,identity.session_id),
                     *await self._web_factory(identity.user_id,identity.agent_id,identity.session_id)]
            from workmate_runtime.mcp.assembly import assemble_mcp_tools
            with preparation_stage(owner, 'mcp', resume=resume):
                mcp_tools,mcp_binding = await assemble_mcp_tools(context,binding,self.sessions,guard,
                    self.workspaces.resource_registry,storage_node_id=identity.storage_node_id,
                    knowledge_enabled=self.settings.knowledge.enabled,
                    product_api_url=self.settings.product_api_url)
            tools.extend(mcp_tools)
            current = _CurrentAuthority(self,owner,guard,context)
            from .platform_tools import NativePlatformTools,PlatformCallBinding,public_model_profiles
            native = NativePlatformTools(self.sessions,context,guard,binding,
                knowledge_enabled=self.settings.knowledge.enabled,
                model_profiles=public_model_profiles(self.settings),storage_node_id=identity.storage_node_id)
            tools.extend(native.tools(profile_only=context.platform_tool_profile != 'workspace'))
            platform = PlatformCallBinding(native)
            memory = None
            if (context.initial_spec.recovery_fork_id is None
                    and self.settings.reme_enabled and self.memory_manager is not None
                    and self.memory_manager.chat_model is not None):
                async def authorize_memory():
                    policy=await current._current()
                    if 'memory_search' in {name.strip().casefold() for name in policy.denied_tools}:
                        raise AgentAssemblyError('Memory is denied by Product policy')
                async def authorize_memory_preparation():
                    latest=await self._context(owner)
                    if (guard.failure is not None or latest.initial_spec!=context.initial_spec
                            or latest.permission_version!=context.permission_version
                            or 'memory_search' in {name.strip().casefold()
                                for name in latest.effective_tool_policy.denied_tools}):
                        raise AgentAssemblyError('Memory preparation authority changed')
                memory=self.memory_manager.middleware(identity.user_id,identity.agent_id,
                    task_input_ids=tuple(message.id for message in context.input_messages),
                    authorize=authorize_memory,authorize_preparation=authorize_memory_preparation)
                with preparation_stage(owner, 'memory', resume=resume):
                    tools.extend(await memory.list_tools())
            rag = None
            if self.knowledge_resolver is not None:
                async def authorize_knowledge():
                    policy = await current._current()
                    if 'search_knowledge' in {name.strip().casefold() for name in policy.denied_tools}:
                        raise AgentAssemblyError('Knowledge retrieval is denied by Product policy')
                async def authorize_preparation():
                    latest = await self._context(owner)
                    if (guard.failure is not None or latest.initial_spec != context.initial_spec
                            or latest.permission_version != context.permission_version
                            or latest.knowledge_config != context.knowledge_config
                            or 'search_knowledge' in {name.strip().casefold()
                                for name in latest.effective_tool_policy.denied_tools}):
                        raise AgentAssemblyError('Knowledge preparation authority changed')
                with preparation_stage(owner, 'rag', resume=resume):
                    rag = await self.knowledge_resolver.prepare(context,authorize_knowledge,
                        authorize_preparation=authorize_preparation)
                    if rag is not None:
                        tools.extend(await rag.list_tools())
            if len({t.name for t in tools})!=len(tools):
                raise AgentAssemblyError('Duplicate tool names cannot be assembled')
            prompt = finalize_system_prompt(context.system_prompt,binding.instructions)
            engine = CoreEngine(Agent(name=context.agent_id,system_prompt=prompt,model=model,
                state=state,toolkit=Toolkit(tools=tools,skills_or_loaders=skills),offloader=binding.offloader,
                middlewares=[ToolOutputCharacters(),current,_ExecutionConfiguration(context.effective_approval_profile or context.initial_spec.approval_profile,binding.workspace.workdir),*([rag] if rag is not None else []),*([memory] if memory is not None else []),*([platform] if platform is not None else []),*([mcp_binding] if mcp_binding is not None else []),guard]))
            self._bound[owner] = _BoundAssembly(identity,binding,_policy(context),context.agent_id,prompt)
            return engine,inputs
        finally:
            self._assembling.discard(owner)

    async def prepare_start(self,owner,snapshot,guard):
        engine,inputs = await self._prepare(owner,snapshot,guard,resume=False)
        return PreparedExecution(engine,inputs)

    async def prepare_resume(self,owner,snapshot,guard):
        engine,_ = await self._prepare(owner,snapshot,guard,resume=True)
        return engine

    def _verify_recovered_workspace(self, identity, recovery):
        lease = self.workspaces._ownership.get(identity)
        if lease is None or lease.generation != recovery['resource_generation']:
            raise AgentAssemblyError('Recovered Workspace generation differs from the suspended working files')

    async def _bind_cold_cleanup(self, owner, snapshot):
        """Restore cancellation resources without current-user execution access."""
        recovery = await self._cold_recovery(owner, snapshot)
        if not recovery:
            raise AgentAssemblyError('Cold cleanup requires exact durable recovery authority')
        from workmate_product.persistence.models import RuntimeExecutionSpec
        from workmate_product.application.execution_context import _stored_spec
        async with self.sessions.begin() as db:
            spec = _stored_spec(await db.get(RuntimeExecutionSpec, owner.task_id))
        if spec.task_id != owner.task_id or spec.session_id != owner.session_id:
            raise AgentAssemblyError('Cleanup admission belongs to another execution')
        identity = WorkspaceIdentity(spec.actor_user_id,spec.agent_id,spec.conversation_id,
            owner.session_id,self.workspaces.storage_node_id)
        self._state(owner,snapshot,spec.agent_id,resume=True)
        self._preparation_started.add(owner)
        binding = await self.workspaces.bind_task(identity,owner,fresh_claim=True,
            all_resource_paths_tracked=True,read_model_input_types=spec.selected_model.model_input_types,
            expected_storage_proof=recovery.get('storage_proof'))
        self._verify_recovered_workspace(identity,recovery)
        if self.delivery_coordinator is not None:
            await self.delivery_coordinator.rebind(owner,binding)
        policy = ExecutionPermissionPolicy(task_id=owner.task_id,approval_profile='strict')
        self._bound[owner] = _BoundAssembly(identity,binding,policy,spec.agent_id,'')

    async def prepare_cleanup(self,owner,snapshot,guard):
        self._owner(owner,guard)
        if not isinstance(guard,StopGuard) or snapshot is None:
            raise AgentAssemblyError('Cleanup requires an exact snapshot and Stop guard')
        if owner not in self._bound:
            await self._bind_cold_cleanup(owner,snapshot)
        bound = self._bound[owner]
        self.workspaces.resource_registry._scope(owner)
        state = self._state(owner,snapshot,bound.agent_name,resume=True)
        if state.reply_id != guard.reply_id:
            raise AgentAssemblyError('Cleanup snapshot belongs to another Reply')
        return _CleanupEngine(Agent(name=bound.agent_name,system_prompt=bound.system_prompt,
            model=_NoExecutionModel(),state=state,toolkit=Toolkit(),
            middlewares=[ProductPermissionMiddleware(bound.policy),
                _ExecutionConfiguration('strict',bound.binding.workspace.workdir),guard]))

    async def verify_preparation_cleanup(self,owner):
        """Only for the controller after it has cancelled and joined preparation.

        Before bind_task, this concrete assembler performs DB reads, constructs
        a model object, and may initialize shared workspace infrastructure. None
        can launch Task work; every Task I/O path requires a registered backend
        scope. Shared gateway shutdown belongs to WorkerWorkspaceManager.close.
        """
        if owner not in self._preparation_started or owner in self._assembling:
            return False
        if owner not in self.workspaces.resource_registry._scopes:
            return True
        return await self.workspaces.resource_registry.verify_preparation_cleanup(owner)
