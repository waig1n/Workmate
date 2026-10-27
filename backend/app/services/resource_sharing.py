"""Explicit sharing plans and atomic grants, separate from access evaluation."""
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, select

from workmate_product.persistence.models import (
    Agent,
    AgentCapabilityBinding,
    CapabilityInstallation,
    CapabilityType,
    KnowledgeBase,
    ResourceShare,
    Scope,
    ScopeClosure,
    ScopeKind,
    Visibility,
)
from .knowledge import KnowledgeService
from workmate_product.application.knowledge_access import KnowledgeAccess
from .security_audit import SecurityAuditService


def resource_column(kind):
    return {'agents': ResourceShare.agent_id, 'skills': ResourceShare.capability_id, 'mcps': ResourceShare.capability_id,
            'knowledge-bases': ResourceShare.knowledge_base_id}[kind]


async def lock_sharing(db):
    # Lock before scope/installation locks, also used by publishing and binding.
    await db.execute(select(Scope.id).where(Scope.kind == ScopeKind.ORGANIZATION).order_by(Scope.id).with_for_update())


def confirmation_for(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def managed_resource(db, access, user_id, kind, resource_id):
    model = {'agents': Agent, 'skills': CapabilityInstallation, 'mcps': CapabilityInstallation, 'knowledge-bases': KnowledgeBase}[kind]
    resource = await db.get(model, resource_id)
    if (resource is None or (kind in ('skills', 'mcps') and resource.capability_type != {'skills': CapabilityType.SKILL, 'mcps': CapabilityType.MCP}[kind])
            or (kind == 'agents' and resource.deleted_at is not None)):
        raise HTTPException(404, 'Resource not found')
    scope = await db.get(Scope, resource.scope_id)
    if scope is None or scope.kind == ScopeKind.PERSONAL:
        raise HTTPException(403, 'Personal resources cannot be shared')
    if kind == 'agents' and not await access.can_view_agent(user_id, resource.id):
        raise HTTPException(404, 'Resource not found')
    if kind == 'knowledge-bases':
        await KnowledgeService(db, user_id).get(resource.id, manage=True)
    if kind in ('skills', 'mcps') and not await access.has_permission(user_id, scope.id,
            'skill.manage' if kind == 'skills' else 'mcp.manage'):
        if resource.id not in await access.accessible_capability_ids(user_id):
            raise HTTPException(404, 'Resource not found')
    permissions = {'agents': ('agent.update', 'agent.publish'), 'skills': ('skill.manage',), 'mcps': ('mcp.manage',),
                   'knowledge-bases': ('knowledge.manage',)}[kind]
    for permission in permissions:
        if not await access.has_permission(user_id, scope.id, permission):
            raise HTTPException(403, f'Permission required in source scope: {permission}')
    return resource


async def target_departments(db, source_scope_id, kind='agents'):
    roots = select(ScopeClosure.ancestor_id).join(Scope, Scope.id == ScopeClosure.ancestor_id).where(
        ScopeClosure.descendant_id == source_scope_id, Scope.kind == ScopeKind.ORGANIZATION,
    )
    return list((await db.scalars(select(Scope).join(ScopeClosure, ScopeClosure.descendant_id == Scope.id).where(
        ScopeClosure.ancestor_id.in_(roots), Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)),
    ).order_by(Scope.name, Scope.id))).unique().all())


async def existing_shares(db, kind, resource_id):
    return list((await db.scalars(select(ResourceShare).where(
        resource_column(kind) == resource_id).order_by(ResourceShare.target_scope_id))).all())


async def _covers_branch(db, kind, resource_id, target_id):
    return await db.scalar(select(ResourceShare.id).where(
        resource_column(kind) == resource_id,
        ResourceShare.target_scope_id.in_(select(ScopeClosure.ancestor_id).where(
            ScopeClosure.descendant_id == target_id)),
        ResourceShare.include_descendants,
    )) is not None


async def _knowledge_plan(db, access, user_id, kind, resource, source, target, inherited):
    ids = ((resource.knowledge_config or {}).get('knowledge_base_ids', []) if kind == 'agents'
           else [resource.id] if kind == 'knowledge-bases' else [])
    rows = list((await db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(ids))
                                 .order_by(KnowledgeBase.id))).all())
    blockers = ['关联知识库不存在'] if len(rows) != len(set(ids)) else []
    available = await KnowledgeAccess(db).ids_for_scopes({target.id})
    bindable = await KnowledgeService(db, user_id).bindable_ids(source)
    pending = []
    for kb in rows:
        if not kb.enabled:
            blockers.append(f'{kb.name} 已停用')
        if kind == 'agents' and kb.id not in bindable:
            blockers.append(f'{kb.name} 对来源智能体已失去授权')
        covered = (await _covers_branch(db, 'knowledge-bases', kb.id, target.id)
                   if inherited else kb.id in available)
        if kind == 'agents' and covered:
            continue
        if kind == 'agents' and inherited:
            existing = await db.scalar(select(ResourceShare).where(
                ResourceShare.knowledge_base_id == kb.id, ResourceShare.target_scope_id == target.id))
            if existing is not None and existing.allow_download and not existing.include_descendants:
                blockers.append(f'{kb.name} 当前仅本部门可下载；扩展检索范围会同时扩大下载权限，请先单独调整知识库共享')
        kb_scope = await db.get(Scope, kb.scope_id)
        if kb_scope is None or kb_scope.kind == ScopeKind.PERSONAL:
            blockers.append(f'{kb.name} 不能共享')
        if not await access.has_permission(user_id, kb.scope_id, 'knowledge.manage'):
            blockers.append(f'没有共享知识库 {kb.name} 的来源范围管理权限')
        pending.append({'id': kb.id, 'name': kb.name, 'scope_id': kb.scope_id})
    return pending, blockers, rows


async def build_plan(db, access, user_id, kind, resource, target_scope_id, *, draft=False,
                     allow_download=False, include_descendants=None):
    targets = await target_departments(db, resource.scope_id, kind)
    target = next((scope for scope in targets if scope.id == target_scope_id), None)
    if target is None:
        raise HTTPException(422, 'Target must be an allowed scope in the same organization')
    if allow_download and kind != 'knowledge-bases':
        raise HTTPException(422, 'Download permission applies only to knowledge bases')
    inherited = (target.kind == ScopeKind.ORGANIZATION if include_descendants is None else include_descendants)
    available = await access.capability_ids_for_scopes({target.id})
    dependencies = list((await db.scalars(select(CapabilityInstallation).join(
        AgentCapabilityBinding, AgentCapabilityBinding.capability_installation_id == CapabilityInstallation.id,
    ).where(AgentCapabilityBinding.agent_id == resource.id).order_by(CapabilityInstallation.id))).all()) if kind == 'agents' else [resource] if kind in ('skills', 'mcps') else []
    skills, mcps, blockers = [], [], []
    source = await db.get(Scope, resource.scope_id)
    bindable = await access.bindable_capability_ids(source)
    for dep in dependencies:
        if not dep.enabled:
            blockers.append(f'{dep.name} 已停用')
        if kind == 'agents' and dep.id not in bindable:
            blockers.append(f'{dep.name} 对来源智能体已失去授权')
        covered = dep.id in available
        if inherited:
            covered = await _covers_branch(db, 'skills' if dep.capability_type == CapabilityType.SKILL else 'mcps', dep.id, target.id)
        if kind == 'agents' and covered:
            continue
        dep_scope = await db.get(Scope, dep.scope_id)
        if dep_scope is None or dep_scope.kind == ScopeKind.PERSONAL:
            blockers.append(f'{dep.name} 不能共享')
        is_skill = dep.capability_type == CapabilityType.SKILL
        permission = 'skill.manage' if is_skill else 'mcp.manage'
        if not await access.has_permission(user_id, dep.scope_id, permission):
            label = '技能' if is_skill else 'MCP'
            blockers.append(f'没有共享{label} {dep.name} 的来源范围管理权限')
        (skills if is_skill else mcps).append({'id': dep.id, 'name': dep.display_name or dep.name, 'scope_id': dep.scope_id})
    knowledge, knowledge_blockers, libraries = await _knowledge_plan(
        db, access, user_id, kind, resource, source, target, inherited)
    root = await db.scalar(select(Scope).join(ScopeClosure, ScopeClosure.ancestor_id == Scope.id)
                           .where(Scope.kind == ScopeKind.ORGANIZATION, ScopeClosure.descendant_id == target.id)
                           .execution_options(populate_existing=True))
    # Include every relevant resource grant, not just missing dependencies. A
    # concurrent sharing edit or tree move must invalidate an old confirmation.
    relevant = [(kind, resource.id)] + [
        ('skills' if dep.capability_type == CapabilityType.SKILL else 'mcps', dep.id) for dep in dependencies]
    relevant += [('knowledge-bases', kb.id) for kb in libraries]
    share_state = []
    for resource_kind, rid in sorted(set(relevant)):
        share_state.append((resource_kind, rid, [
            (g.id, g.target_scope_id, g.include_descendants, g.allow_download)
            for g in await existing_shares(db, resource_kind, rid)]))
    plan = {
        'resource_id': resource.id, 'name': getattr(resource, 'display_name', None) or resource.name,
        'target_scope_id': target.id,
        'target_name': '全组织可用' if target.kind == ScopeKind.ORGANIZATION and inherited else target.name,
        'include_descendants': inherited, 'allow_download': allow_download,
        'tree_version': root.tree_version if root else None,
        'publishes_agent': kind == 'agents' and resource.visibility == Visibility.PRIVATE,
        'skills': skills, 'mcps': mcps, 'knowledge_bases': knowledge, 'blockers': blockers + knowledge_blockers,
    }
    plan['confirmation'] = confirmation_for({
        **plan, 'updated_at': None if draft else resource.updated_at,
        'share_state': share_state,
        'dependencies': [(d.id, d.enabled, d.updated_at) for d in dependencies],
        'knowledge': [(kb.id, kb.enabled, kb.updated_at) for kb in libraries],
        'knowledge_config': resource.knowledge_config if kind == 'agents' else None,
    })
    return plan


async def grant(db, kind, resource_id, target_scope_id, user_id, *, allow_download=None,
                include_descendants=None, preserve_existing=False):
    target = await db.get(Scope, target_scope_id)
    inherited = (target.kind == ScopeKind.ORGANIZATION if include_descendants is None else include_descendants)
    column = resource_column(kind)
    existing = await db.scalar(select(ResourceShare).where(column == resource_id, ResourceShare.target_scope_id == target_scope_id))
    if existing is not None:
        if (preserve_existing and kind == 'knowledge-bases' and inherited
                and existing.allow_download and not existing.include_descendants):
            raise HTTPException(409, '请先单独调整知识库共享，避免扩大已有下载权限')
        next_inherited = (inherited or existing.include_descendants) if preserve_existing else inherited
        next_download = existing.allow_download if allow_download is None else allow_download
        if preserve_existing:
            next_download = next_download or existing.allow_download
        if existing.include_descendants == next_inherited and existing.allow_download == next_download:
            return
        existing.include_descendants = next_inherited
        existing.allow_download = next_download
    else:
        db.add(ResourceShare(id=str(uuid.uuid4()), target_scope_id=target_scope_id, created_by=user_id,
            include_descendants=inherited, allow_download=bool(allow_download), **{column.key: resource_id}))
    SecurityAuditService(db).record('resource.share', actor_user_id=user_id, scope_id=target_scope_id,
        metadata={'resource_type': kind, 'resource_id': resource_id,
                  'include_descendants': existing.include_descendants if existing is not None else inherited,
                  'allow_download': existing.allow_download if existing is not None else bool(allow_download),
                  'preserve_existing': preserve_existing})


async def apply_plan(db, plan, kind, user_id):
    options = {'include_descendants': plan['include_descendants']}
    for skill in plan['skills']:
        await grant(db, 'skills', skill['id'], plan['target_scope_id'], user_id,
                    preserve_existing=kind != 'skills', **options)
    for mcp in plan['mcps']:
        await grant(db, 'mcps', mcp['id'], plan['target_scope_id'], user_id,
                    preserve_existing=kind != 'mcps', **options)
    for kb in plan['knowledge_bases']:
        await grant(db, 'knowledge-bases', kb['id'], plan['target_scope_id'], user_id,
                    allow_download=plan['allow_download'] if kind == 'knowledge-bases' else None,
                    preserve_existing=kind != 'knowledge-bases', **options)
    if kind == 'agents':
        agent = await db.get(Agent, plan['resource_id'])
        if agent.visibility == Visibility.PRIVATE:
            agent.visibility = Visibility.PUBLIC
            SecurityAuditService(db).record('agent.publish', actor_user_id=user_id, scope_id=agent.scope_id,
                                           metadata={'agent_id': agent.id, 'source': 'sharing'})
        await grant(db, kind, agent.id, plan['target_scope_id'], user_id, **options)


async def check_binding_shares(db, access, user_id, agent, confirmation):
    plans = [await build_plan(db, access, user_id, 'agents', agent, share.target_scope_id,
                             draft=True, include_descendants=share.include_descendants)
             for share in await existing_shares(db, 'agents', agent.id)]
    blockers = [blocker for plan in plans for blocker in plan['blockers']]
    if blockers:
        raise HTTPException(409, {'code': 'sharing_blocked', 'message': '共享依赖不可用', 'blockers': blockers})
    pending = [plan for plan in plans if plan['skills'] or plan['mcps'] or plan['knowledge_bases']]
    if not pending:
        return
    if not await access.has_permission(user_id, agent.scope_id, 'agent.publish'):
        raise HTTPException(403, 'Permission required in source scope: agent.publish')
    digest = confirmation_for(pending)
    if confirmation != digest:
        raise HTTPException(409, {'code': 'sharing_confirmation_required',
                                 'message': '保存此绑定需要同时向使用范围授权新增技能、MCP 或知识库',
                                 'plans': pending, 'confirmation': digest})
    for plan in pending:
        await apply_plan(db, plan, 'agents', user_id)


async def revoke(db, kind, resource_id, target_scope_id, user_id):
    existing = await db.scalar(select(ResourceShare.id).where(
        resource_column(kind) == resource_id, ResourceShare.target_scope_id == target_scope_id,
    ))
    if existing is None:
        return
    await db.execute(delete(ResourceShare).where(ResourceShare.id == existing))
    SecurityAuditService(db).record('resource.unshare', actor_user_id=user_id, scope_id=target_scope_id,
                                    metadata={'resource_type': kind, 'resource_id': resource_id})


async def revoke_all_agent_shares(db, agent_id, user_id):
    for share in await existing_shares(db, 'agents', agent_id):
        await revoke(db, 'agents', agent_id, share.target_scope_id, user_id)


# Read-only personal grant explanations share the product authorization rules.
from workmate_product.application.resource_sharing import access_sources
