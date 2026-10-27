"""Product Agent management for HTTP composition; caller owns the transaction.

Complete management fields remain separate from the intentionally narrower tool
arguments. No App registration, engine import or independently committed write.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import delete, select
from .authorization import AccessDecisionService
from .execution_context import _fresh_identity_map
from .agent_mutations import authorize_create, resolve_scope
from .agent_bindings import _apply_bindings, _lock_scope_ids, _upsert_default, _clear_default
from .resource_sharing import lock_sharing, check_binding_shares, revoke_all_agent_shares
from .knowledge_access import validate_knowledge_bindings
from .security_audit import SecurityAuditService
from .platform_types import PlatformToolError
from ..prompts import DEFAULT_AGENT_PROMPT, DEFAULT_WORKSPACE_ASSISTANT_PROMPT
from ..persistence.models import (
    Agent, AgentCapabilityBinding, AgentDefault, CapabilityInstallation,
    Conversation, ResourceShare, Scope, ScopeKind, Task, User, Visibility,
    ACTIVE_TASK_STATUSES, RuntimeSession,
)


async def _actor(db, actor_id, knowledge_enabled):
    user = await db.get(User, actor_id)
    if user is None or user.status != 'active':
        raise PlatformToolError(403, 'Active user required')
    return user, AccessDecisionService(db, knowledge_enabled=knowledge_enabled)


async def _lock_scopes(db, scope_id, body=None):
    identifiers = set()
    if body is not None:
        identifiers.update(body.skill_binding_ids or [])
        identifiers.update(body.mcp_binding_ids or [])
    scopes = set((await db.scalars(select(CapabilityInstallation.scope_id).where(
        CapabilityInstallation.id.in_(identifiers)))).all()) if identifiers else set()
    await _lock_scope_ids(db, scopes | {scope_id})


async def _existing(db, access, actor_id, agent_id, body=None, *, require_view=True):
    scope_id = await db.scalar(select(Agent.scope_id).where(Agent.id == agent_id))
    if scope_id is None:
        raise PlatformToolError(404, 'Agent not found')
    await _lock_scopes(db, scope_id, body)
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id).with_for_update()
                            .execution_options(populate_existing=True))
    if (agent is None or agent.deleted_at is not None
            or (require_view and not await access.can_view_agent(actor_id, agent_id))):
        raise PlatformToolError(404, 'Agent not found')
    return agent


async def _platform_profile(access, user, scope, profile):
    if profile != 'workspace':
        return
    if scope.kind == ScopeKind.PERSONAL:
        raise PlatformToolError(422, 'Platform tools cannot be enabled for personal agents')
    if not await access.has_permission(user.id, scope.id, 'agent.update'):
        raise PlatformToolError(403, 'Permission required in this scope: agent.update')


def _audit(db, operation, agent, actor_id):
    SecurityAuditService(db).record('agent.' + operation, actor_user_id=actor_id,
        scope_id=agent.scope_id, metadata={'agent_id': agent.id})


@_fresh_identity_map
async def create_agent(db, *, actor_id, body, knowledge_enabled=False):
    await lock_sharing(db)
    user, access = await _actor(db, actor_id, knowledge_enabled)
    scope = await resolve_scope(db, user, body.scope_id)
    await _lock_scopes(db, scope.id, body)
    await authorize_create(db, access, user, body)
    await _platform_profile(access, user, scope, body.platform_tool_profile)
    if await db.scalar(select(Agent.id).where(Agent.scope_id == scope.id, Agent.slug == body.slug)):
        raise PlatformToolError(409, 'Slug already exists in this scope')
    agent = Agent(id=str(uuid.uuid4()), slug=body.slug, display_name=body.display_name,
        system_prompt=body.system_prompt or DEFAULT_AGENT_PROMPT, description=body.description,
        knowledge_config=body.knowledge_config.model_dump(mode='json'),
        tool_policy=body.tool_policy.model_dump(mode='json'), platform_tool_profile=body.platform_tool_profile,
        scope_id=scope.id, visibility=body.visibility, include_descendants=body.include_descendants,
        created_by=actor_id)
    await validate_knowledge_bindings(db, actor_id, agent, body.knowledge_config,
                                      enabled=knowledge_enabled, access=access, require_user_access=False)
    db.add(agent)
    await db.flush()
    await _apply_bindings(db, access, agent, body.skill_binding_ids, body.mcp_binding_ids)
    if body.is_default:
        await _upsert_default(db, scope.id, agent.id, actor_id)
    _audit(db, 'create', agent, actor_id)
    await db.flush()
    return agent.id


@_fresh_identity_map
async def update_agent(db, *, actor_id, agent_id, body, knowledge_enabled=False):
    await lock_sharing(db)
    user, access = await _actor(db, actor_id, knowledge_enabled)
    agent = await _existing(db, access, actor_id, agent_id, body)
    if not await access.can_manage_agent(actor_id, agent_id):
        raise PlatformToolError(403, 'Not allowed to manage this agent')
    scope = await db.get(Scope, agent.scope_id)
    await _platform_profile(access, user, scope, body.platform_tool_profile)
    if scope.kind == ScopeKind.PERSONAL and body.visibility == Visibility.PUBLIC:
        raise PlatformToolError(422, 'Personal agents cannot be public')
    if (((body.visibility == Visibility.PUBLIC and agent.visibility == Visibility.PRIVATE)
            or (body.include_descendants is True and not agent.include_descendants
                and (body.visibility or agent.visibility) == Visibility.PUBLIC))
            and scope.kind != ScopeKind.PERSONAL
            and not await access.has_permission(actor_id, scope.id, 'agent.publish')):
        raise PlatformToolError(403, 'Permission required in this scope: agent.publish')
    if body.knowledge_config is not None:
        await validate_knowledge_bindings(db, actor_id, agent, body.knowledge_config,
                                          enabled=knowledge_enabled, access=access, require_user_access=False)
        agent.knowledge_config = body.knowledge_config.model_dump(mode='json')
    for field in ('display_name', 'system_prompt', 'description', 'platform_tool_profile',
                  'visibility', 'include_descendants'):
        value = getattr(body, field)
        if value is not None:
            setattr(agent, field, value)
    if body.visibility == Visibility.PRIVATE:
        await revoke_all_agent_shares(db, agent.id, actor_id)
    if body.tool_policy is not None:
        agent.tool_policy = body.tool_policy.model_dump(mode='json')
    if body.skill_binding_ids is not None or body.mcp_binding_ids is not None:
        await _apply_bindings(db, access, agent, body.skill_binding_ids, body.mcp_binding_ids)
    if (body.skill_binding_ids is not None or body.mcp_binding_ids is not None
            or body.knowledge_config is not None):
        await check_binding_shares(db, access, actor_id, agent, body.sharing_confirmation)
    if body.is_default is True:
        await _upsert_default(db, scope.id, agent.id, actor_id)
    elif body.is_default is False:
        await _clear_default(db, scope.id, agent.id)
    _audit(db, 'update', agent, actor_id)
    await db.flush()
    return agent.id


@_fresh_identity_map
async def restore_default_prompt(db, *, actor_id, agent_id, knowledge_enabled=False):
    await lock_sharing(db)
    _, access = await _actor(db, actor_id, knowledge_enabled)
    agent = await _existing(db, access, actor_id, agent_id)
    if agent.system_key != 'workspace_assistant':
        raise PlatformToolError(404, 'Workspace assistant not found')
    if not await access.can_manage_agent(actor_id, agent_id):
        raise PlatformToolError(403, 'Not allowed to manage this agent')
    agent.system_prompt = DEFAULT_WORKSPACE_ASSISTANT_PROMPT
    _audit(db, 'restore_default_prompt', agent, actor_id)
    await db.flush()
    return agent.id


@_fresh_identity_map
async def delete_agent(db, *, actor_id, agent_id, knowledge_enabled=False):
    await lock_sharing(db)
    _, access = await _actor(db, actor_id, knowledge_enabled)
    agent = await _existing(db, access, actor_id, agent_id, require_view=False)
    if not await access.can_delete_agent(actor_id, agent_id):
        if not await access.can_view_agent(actor_id, agent_id):
            raise PlatformToolError(404, 'Agent not found')
        raise PlatformToolError(403, 'Permission required in this scope: agent.delete')
    if agent.system_key is not None:
        raise PlatformToolError(409, 'Platform-seeded agents cannot be deleted')
    active = await db.scalar(select(Task.id).join(Conversation, Conversation.id == Task.conversation_id)
        .where(Conversation.agent_id == agent_id, Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
    # A projected terminal Task may still own its delivery/workspace gate.
    held = await db.scalar(select(RuntimeSession.id).join(Conversation,
        Conversation.id == RuntimeSession.conversation_id).where(Conversation.agent_id == agent_id,
        RuntimeSession.active_task_id.is_not(None)).limit(1))
    delivery = await db.scalar(select(Conversation.id).where(Conversation.agent_id == agent_id,
        Conversation.active_product_task_id.is_not(None)).limit(1))
    if active or held or delivery:
        raise PlatformToolError(409, '该智能体仍有运行中或等待审批的任务，请先结束任务后再删除')
    default = await db.get(AgentDefault, agent.scope_id)
    if default is not None and default.agent_id == agent_id:
        raise PlatformToolError(409, 'Agent is a scope default; clear the default first')
    history = await db.scalar(select(Conversation.id).where(Conversation.agent_id == agent_id).limit(1))
    await db.execute(delete(AgentCapabilityBinding).where(AgentCapabilityBinding.agent_id == agent_id))
    await db.execute(delete(ResourceShare).where(ResourceShare.agent_id == agent_id))
    _audit(db, 'delete', agent, actor_id)
    if history:
        agent.deleted_at = datetime.now(timezone.utc)
    else:
        await db.delete(agent)
    await db.flush()
