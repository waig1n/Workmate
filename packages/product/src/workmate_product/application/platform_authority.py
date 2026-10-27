"""Shared platform-call authorization inside the caller-owned transaction.

Read and mutation entrypoints use the same execution fencing. Mutation calls
also acquire sharing and Scope locks before the common Agent-first lock order.
"""
from sqlalchemy import select, text
from ..persistence.models import Agent, Conversation, Task, User, Scope, CapabilityInstallation
from .authorization import AccessDecisionService
from .execution_context import read_execution_context, ExecutionContextRejected
from .platform_types import PlatformToolError
from .resource_sharing import lock_sharing
from .user_profiles import USER_PROFILE_TOOLS


async def _row(db,sql,**params):
    return (await db.execute(text(sql),params)).mappings().first()


async def authorize_platform_call(db,req,*,knowledge_enabled=False,mutating=True):
    ids={req.agent_id}
    if req.tool_name=='agent_update' and isinstance(req.arguments.get('agent_id'),str):
        ids.add(req.arguments['agent_id'])
    if mutating:
        # Match Product binding/default mutations: global sharing coordination,
        # every participating Scope, then Agent rows. Discovery is not authority.
        # Agent/capability scope ownership has no mutable Product API operation.
        await lock_sharing(db)
        scopes=set((await db.scalars(select(Agent.scope_id).where(Agent.id.in_(ids)))).all())
        explicit=req.arguments.get('scope_id')
        if isinstance(explicit,str):
            scopes.add(explicit)
        if req.tool_name=='agent_create' and explicit is None:
            scopes.update((await db.scalars(select(Scope.id).where(Scope.kind=='personal',
                Scope.owner_user_id==req.actor_user_id))).all())
        bindings=[]
        for key in ('skill_binding_ids','mcp_binding_ids'):
            value=req.arguments.get(key)
            if isinstance(value,list):
                bindings.extend(item for item in value if isinstance(item,str))
        if bindings:
            scopes.update((await db.scalars(select(CapabilityInstallation.scope_id).where(
                CapabilityInstallation.id.in_(bindings)))).all())
        await db.execute(select(Scope.id).where(Scope.id.in_(scopes)).order_by(Scope.id).with_for_update())
    # Mutations take exclusive Agent locks up front, including agent_update's
    # target. Never upgrade a shared authority lock after taking child locks.
    await db.execute(select(Agent.id).where(Agent.id.in_(ids)).order_by(Agent.id)
                     .with_for_update(read=not mutating))
    conv=await db.scalar(select(Conversation).where(Conversation.id==req.conversation_id).with_for_update())
    task=await db.scalar(select(Task).where(Task.id==req.task_id).with_for_update())
    session=await _row(db,'SELECT * FROM runtime_sessions WHERE id=:id FOR UPDATE',id=req.session_id)
    execution=await _row(db,'SELECT * FROM runtime_task_executions WHERE task_id=:id FOR UPDATE',id=req.task_id)
    user=await db.get(User,req.actor_user_id)
    agent=await db.get(Agent,req.agent_id)
    if (not conv or not task or not session or not execution or not user or not agent
            or user.status!='active' or agent.deleted_at is not None
            or conv.deleted_at is not None or conv.user_id!=user.id or conv.agent_id!=agent.id or conv.active_product_task_id!=task.id
            or task.conversation_id!=conv.id or task.status not in {'created','running','awaiting_permission'}
            or task.run_id not in {None,req.reply_id}
            or session['conversation_id']!=conv.id or session['active_task_id']!=task.id
            or session['owner_id']!=req.worker_id or session['execution_epoch']!=req.execution_epoch
            or session['storage_node_id']!=req.storage_node_id or session['reply_id']!=req.reply_id
            or session['status']!='running' or execution['status']!='running'
            or execution['session_id']!=session['id']
            or any(execution[k]!=session[k] for k in ('reply_id','execution_epoch','state_version'))
            or not await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id'),{'id':req.session_id})):
        raise PlatformToolError(409,'Platform tool execution authority is stale')
    access=AccessDecisionService(db,knowledge_enabled=knowledge_enabled)
    try:
        context=await read_execution_context(db,session_id=req.session_id,task_id=req.task_id,
            worker_id=req.worker_id,execution_epoch=req.execution_epoch,storage_node_id=req.storage_node_id,
            knowledge_enabled=knowledge_enabled)
    except ExecutionContextRejected as error:
        raise PlatformToolError(409,str(error)) from error
    profile_read = not mutating and req.tool_name in USER_PROFILE_TOOLS
    if not profile_read and context.platform_tool_profile!='workspace':
        raise PlatformToolError(403,'Platform tools are disabled')
    group = None if profile_read else ('skill_management' if req.tool_name=='skill_install' else 'agent_management')
    policy=context.effective_tool_policy
    if (req.tool_name.casefold() in {t.strip().casefold() for t in policy.denied_tools}
            or group in policy.denied_groups):
        raise PlatformToolError(403,'Agent policy denies this tool')
    if mutating and context.initial_spec.approval_profile=='read_only':
        raise PlatformToolError(403,'Read-only task cannot submit changes')
    return user,access
