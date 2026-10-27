"""Actual Product Agent writes; no Runtime App registration or internal commits."""
import uuid
from datetime import datetime,timezone
from sqlalchemy import select
from pydantic import Field
from ..persistence.models import Agent,Scope,ScopeKind,Visibility
from ..prompts import DEFAULT_AGENT_PROMPT
from ..config.knowledge import KnowledgeConfig
from .platform_types import PlatformToolError,_AgentCreateArgs,_AgentUpdateArgs
from .agent_bindings import _apply_bindings,_upsert_default,_clear_default
from .resource_sharing import check_binding_shares, revoke_all_agent_shares
from .security_audit import SecurityAuditService


class AgentCreateDraft(_AgentCreateArgs):
    slug:str=Field(min_length=2,max_length=64,pattern=r'^[a-z0-9][a-z0-9-]*$')
    display_name:str=Field(min_length=1,max_length=100)


def check_agent_version(current,expected):
    try:
        supplied=datetime.fromisoformat(expected.replace('Z','+00:00'))
    except ValueError as error:
        raise PlatformToolError(422,'expected_updated_at must be an ISO 8601 timestamp') from error
    actual=current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current
    supplied=supplied.replace(tzinfo=timezone.utc) if supplied.tzinfo is None else supplied
    if actual!=supplied:
        raise PlatformToolError(409,'Agent version changed')


async def resolve_scope(db,user,scope_id):
    scope=await db.get(Scope,scope_id) if scope_id else await db.scalar(select(Scope).where(
        Scope.kind==ScopeKind.PERSONAL,Scope.owner_user_id==user.id))
    if scope is None:
        raise PlatformToolError(404 if scope_id else 409,'Scope not found' if scope_id else 'User has no personal scope yet')
    return scope


async def authorize_create(db,access,user,args):
    scope=await resolve_scope(db,user,args.scope_id)
    if scope.kind==ScopeKind.PERSONAL:
        if scope.owner_user_id!=user.id:
            raise PlatformToolError(403,'Not the personal scope owner')
        if args.visibility==Visibility.PUBLIC:
            raise PlatformToolError(422,'Personal agents cannot be public')
    else:
        if not await access.has_permission(user.id,scope.id,'agent.create'):
            raise PlatformToolError(403,'Permission required in this scope: agent.create')
        if args.visibility==Visibility.PUBLIC and not await access.has_permission(user.id,scope.id,'agent.publish'):
            raise PlatformToolError(403,'Permission required in this scope: agent.publish')
    return scope


async def create_agent(db,access,user,args):
    args=AgentCreateDraft.model_validate(args)
    scope=await authorize_create(db,access,user,args)
    if await db.scalar(select(Agent.id).where(Agent.scope_id==scope.id,Agent.slug==args.slug)):
        raise PlatformToolError(409,'Slug already exists in this scope')
    agent=Agent(id=str(uuid.uuid4()),slug=args.slug,display_name=args.display_name,
        system_prompt=args.system_prompt or DEFAULT_AGENT_PROMPT,
        description=args.description if args.description.strip() else f'{args.display_name}，根据你的需求提供任务协助。',
        knowledge_config=KnowledgeConfig().model_dump(mode='json'),tool_policy={'denied_groups':[],'denied_tools':[]},
        platform_tool_profile='none',scope_id=scope.id,visibility=args.visibility,
        include_descendants=args.include_descendants,created_by=user.id)
    db.add(agent)
    await db.flush()
    await _apply_bindings(db,access,agent,args.skill_binding_ids,args.mcp_binding_ids)
    if args.is_default:
        await _upsert_default(db,scope.id,agent.id,user.id)
    SecurityAuditService(db).record('agent.create',actor_user_id=user.id,scope_id=scope.id,metadata={'agent_id':agent.id})
    await db.flush()
    return {'created':True,'runtime_ready':True,'agent':{'id':agent.id,'updated_at':agent.updated_at.isoformat(),
        'url':f'/workspace/agents?agent_id={agent.id}'}}


async def update_agent(db,access,user,args):
    args=_AgentUpdateArgs.model_validate(args)
    agent=await db.get(Agent,args.agent_id,populate_existing=True)
    if agent is None or not await access.can_view_agent(user.id,agent.id):
        raise PlatformToolError(404,'Agent not found')
    if not await access.can_manage_agent(user.id,agent.id):
        raise PlatformToolError(403,'Not allowed to manage this agent')
    check_agent_version(agent.updated_at,args.expected_updated_at)
    scope=await db.get(Scope,agent.scope_id)
    if scope.kind==ScopeKind.PERSONAL and args.visibility==Visibility.PUBLIC:
        raise PlatformToolError(422,'Personal agents cannot be public')
    if (((args.visibility==Visibility.PUBLIC and agent.visibility==Visibility.PRIVATE)
            or (args.include_descendants is True and not agent.include_descendants
                and (args.visibility or agent.visibility)==Visibility.PUBLIC))
            and scope.kind!=ScopeKind.PERSONAL and not await access.has_permission(user.id,scope.id,'agent.publish')):
        raise PlatformToolError(403,'Permission required in this scope: agent.publish')
    for field in ('display_name','system_prompt','description','visibility','include_descendants'):
        value=getattr(args,field)
        if value is not None:
            setattr(agent,field,value)
    if args.visibility==Visibility.PRIVATE:
        await revoke_all_agent_shares(db,agent.id,user.id)
    if args.skill_binding_ids is not None or args.mcp_binding_ids is not None:
        await _apply_bindings(db,access,agent,args.skill_binding_ids,args.mcp_binding_ids)
        # Tool input has never accepted sharing_confirmation: preserve mandatory
        # explicit cross-department sharing preflight, without implicit grants.
        await check_binding_shares(db,access,user.id,agent,None)
    if args.is_default is True:
        await _upsert_default(db,scope.id,agent.id,user.id)
    elif args.is_default is False:
        await _clear_default(db,scope.id,agent.id)
    SecurityAuditService(db).record('agent.update',actor_user_id=user.id,scope_id=scope.id,metadata={'agent_id':agent.id})
    await db.flush()
    await db.refresh(agent)
    return {'updated':True,'runtime_ready':True,'agent':{'id':agent.id,'updated_at':agent.updated_at.isoformat(),
        'url':f'/workspace/agents?agent_id={agent.id}'}}
