"""Native Product platform reads with current Actor/Task authorization."""
from sqlalchemy import select
from pydantic import ValidationError
from ..persistence.models import Agent,Scope,ScopeKind,CapabilityInstallation
from .execution_context import _fresh_identity_map
from .platform_authority import authorize_platform_call
from .platform_types import PlatformToolRequest,PlatformToolError,_EmptyArgs,_CapabilityListArgs,_AgentListArgs,_AgentGetArgs,_AgentValidateArgs,_AgentUpdateArgs
from .user_profiles import ListScopeUsersInput, get_current_user, list_scope_users
from .platform_views import accessible_scopes,agent_view,capability_view
from .agent_mutations import AgentCreateDraft,authorize_create,check_agent_version


async def visible_agent(db,access,user,identity):
    agent=await db.get(Agent,identity)
    if agent is None or not await access.can_view_agent(user.id,identity):
        raise PlatformToolError(404,'Agent not found')
    return agent


async def capability_candidates(db,access,user,*,scope_id=None,agent_id=None):
    if (scope_id is None)==(agent_id is None):
        raise PlatformToolError(422,'Provide exactly one of scope_id or agent_id')
    if agent_id is not None:
        agent=await visible_agent(db,access,user,agent_id)
        if not await access.can_manage_agent(user.id,agent.id):
            raise PlatformToolError(403,'Not allowed to manage this agent')
        scope=await db.get(Scope,agent.scope_id)
    else:
        scope=await db.get(Scope,scope_id)
        if scope is None:
            raise PlatformToolError(404,'Scope not found')
        if scope.kind==ScopeKind.PERSONAL:
            if scope.owner_user_id!=user.id:
                raise PlatformToolError(403,'Not scope owner')
        elif not await access.has_permission(user.id,scope.id,'agent.create'):
            raise PlatformToolError(403,'Permission required in this scope: agent.create')
    if scope is None:
        raise PlatformToolError(404,'Scope not found')
    usable=await access.accessible_capability_ids(user.id)
    allowed=await access.bindable_capability_ids(scope)
    rows=(await db.scalars(select(CapabilityInstallation).join(Scope,Scope.id==CapabilityInstallation.scope_id)
        .where(CapabilityInstallation.id.in_(allowed),CapabilityInstallation.enabled,
            Scope.kind.in_((ScopeKind.ORGANIZATION,ScopeKind.DEPARTMENT)))
        .order_by(CapabilityInstallation.capability_type,CapabilityInstallation.name,CapabilityInstallation.id))).all()
    permissions=await access.permissions_for_scopes(user.id,{c.scope_id for c in rows})
    return [capability_view(c,('skill.manage' if c.capability_type=='skill' else 'mcp.manage') in permissions[c.scope_id],
        c.id in usable,await db.get(Scope,c.scope_id)) for c in rows]


async def validate_draft(db,access,user,args):
    if args.operation=='create':
        draft=AgentCreateDraft.model_validate(args.draft)
        scope=await authorize_create(db,access,user,draft)
        target_scope_id,target_agent_id=scope.id,None
        bindings=draft.skill_binding_ids+draft.mcp_binding_ids
    else:
        if not args.agent_id:
            raise PlatformToolError(422,'agent_id is required')
        draft=_AgentUpdateArgs.model_validate({'agent_id':args.agent_id,
            'expected_updated_at':args.expected_updated_at,**args.draft})
        agent=await visible_agent(db,access,user,draft.agent_id)
        if not await access.can_manage_agent(user.id,agent.id):
            raise PlatformToolError(403,'agent.update required')
        check_agent_version(agent.updated_at,draft.expected_updated_at)
        target_scope_id,target_agent_id=agent.scope_id,agent.id
        bindings=(draft.skill_binding_ids or [])+(draft.mcp_binding_ids or [])
    candidates=await capability_candidates(db,access,user,scope_id=target_scope_id if target_agent_id is None else None,
        agent_id=target_agent_id)
    invalid=sorted(set(bindings)-{item['id'] for item in candidates})
    if invalid:
        raise PlatformToolError(422,{'message':'Capabilities cannot be bound','ids':invalid})
    return {'valid':True,'operation':args.operation,'scope_id':target_scope_id,
            'agent_id':target_agent_id,'binding_ids':sorted(set(bindings))}


@_fresh_identity_map
async def execute_platform_read(db,request,*,model_profiles,knowledge_enabled=False):
    """model_profiles is the trusted composition root's sanitized Product catalog."""
    req=PlatformToolRequest.model_validate(request).model_copy(deep=True)
    schemas={'platform_get_context':_EmptyArgs,'model_list':_EmptyArgs,'capability_list':_CapabilityListArgs,
             'agent_list':_AgentListArgs,'agent_get':_AgentGetArgs,'agent_validate':_AgentValidateArgs,
             'get_current_user':_EmptyArgs,'list_scope_users':ListScopeUsersInput}
    if req.tool_name not in schemas:
        raise PlatformToolError(404,'Unknown read tool')
    user,access=await authorize_platform_call(db,req,knowledge_enabled=knowledge_enabled,mutating=False)
    try:
        args=schemas[req.tool_name].model_validate(req.arguments)
        if req.tool_name=='get_current_user':
            return await get_current_user(db,access,user)
        if req.tool_name=='list_scope_users':
            return await list_scope_users(db,access,user,args)
        if req.tool_name=='platform_get_context':
            scopes=await accessible_scopes(db,access,user)
            return {'user_id':user.id,'calling_agent_id':req.agent_id,'scopes':scopes,
                'agent_creation':{'personal_scope_id':next((scope['id'] for scope in scopes if scope['kind']=='personal'),None),
                    'team_scopes':[scope for scope in scopes if scope['kind']!='personal' and 'agent.create' in scope['permissions']]}}
        if req.tool_name=='model_list':
            return list(model_profiles)
        if req.tool_name=='capability_list':
            return await capability_candidates(db,access,user,scope_id=args.scope_id,
                agent_id=args.agent_id or (None if args.scope_id else req.agent_id))
        if req.tool_name=='agent_get':
            return await agent_view(db,access,user,await visible_agent(db,access,user,args.agent_id),knowledge_enabled=knowledge_enabled)
        if req.tool_name=='agent_list':
            agents=await access.list_visible_agents(user.id)
            if args.scope!='all':
                kinds={'personal':{ScopeKind.PERSONAL},'organization':{ScopeKind.ORGANIZATION,ScopeKind.DEPARTMENT}}[args.scope]
                agents=[a for a in agents if (await db.get(Scope,a.scope_id)).kind in kinds]
            return [await agent_view(db,access,user,a,knowledge_enabled=knowledge_enabled) for a in agents]
        return await validate_draft(db,access,user,args)
    except ValidationError as error:
        raise PlatformToolError(422,error.errors(include_url=False,include_input=False,include_context=False)) from error
