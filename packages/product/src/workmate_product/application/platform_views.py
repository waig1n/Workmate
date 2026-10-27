"""Product views used by native platform tools; mirror existing API fields."""
from sqlalchemy import select
from ..config.knowledge import KnowledgeConfig
from ..persistence.models import Scope,ScopeKind,AgentCapabilityBinding,CapabilityInstallation,CapabilityType
from .knowledge_access import unavailable_knowledge_bindings
from .execution_context import _policy


def scope_view(scope,permissions):
    return {'id':scope.id,'kind':scope.kind,'name':scope.name,'parent_id':scope.parent_id,
            'permissions':sorted(permissions)}


async def accessible_scopes(db,access,user):
    ids=await access.accessible_scope_ids(user.id)
    scopes=list((await db.scalars(select(Scope).where(Scope.id.in_(ids)).order_by(Scope.created_at,Scope.id))).all())
    scopes.sort(key=lambda s:(s.kind!=ScopeKind.PERSONAL,s.created_at,s.id))
    permissions=await access.permissions_for_scopes(user.id,{s.id for s in scopes})
    return [scope_view(s,permissions[s.id]) for s in scopes]


def capability_view(installation,manageable,usable,scope):
    return {'id':installation.id,'capability_type':installation.capability_type,'scope_id':installation.scope_id,
        'name':installation.name,'display_name':installation.display_name,'enabled':installation.enabled,
        'metadata':installation.metadata_,'manageable':manageable,'usable':usable,'scope_kind':scope.kind if scope else None}


async def agent_view(db,access,user,agent,*,knowledge_enabled):
    scope=await db.get(Scope,agent.scope_id)
    permissions=await access.permissions_for_scope(user.id,agent.scope_id)
    personal_owner=scope.kind==ScopeKind.PERSONAL and scope.owner_user_id==user.id
    default=await access.resolve_default_agent(user.id)
    bindings=(await db.execute(select(CapabilityInstallation.id,CapabilityInstallation.capability_type)
        .join(AgentCapabilityBinding,AgentCapabilityBinding.capability_installation_id==CapabilityInstallation.id)
        .where(AgentCapabilityBinding.agent_id==agent.id))).all()
    return {'id':agent.id,'slug':agent.slug,'display_name':agent.display_name,'system_prompt':agent.system_prompt if personal_owner or 'agent.update' in permissions else '',
        'description':agent.description,'knowledge_config':KnowledgeConfig.model_validate(agent.knowledge_config or {}).model_dump(mode='json'),
        'unavailable_knowledge_base_ids':await unavailable_knowledge_bindings(db,user.id,agent,enabled=knowledge_enabled,access=access),
        'tool_policy':_policy(agent.tool_policy).model_dump(mode='json'),'system_key':agent.system_key,
        'platform_tool_profile':agent.platform_tool_profile,'scope_id':scope.id,'scope':scope_view(scope,permissions),
        'visibility':agent.visibility,'include_descendants':agent.include_descendants,'created_by':agent.created_by,
        'skill_binding_ids':sorted(r.id for r in bindings if r.capability_type==CapabilityType.SKILL),
        'mcp_binding_ids':sorted(r.id for r in bindings if r.capability_type==CapabilityType.MCP),
        'unavailable_capability_ids':await access.unavailable_capability_ids(user.id,agent.id),
        'actions':{'use':await access.can_use_agent(user.id,agent.id),'update':personal_owner or 'agent.update' in permissions,
            'delete':agent.system_key is None and (personal_owner or 'agent.delete' in permissions),
            'publish':not personal_owner and 'agent.update' in permissions and 'agent.publish' in permissions},
        'is_default':default is not None and default.id==agent.id,
        'created_at':agent.created_at.isoformat(),'updated_at':agent.updated_at.isoformat()}
