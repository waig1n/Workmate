"""Explicit Knowledge use grants, independent of ownership and management."""

from sqlalchemy import select, union_all

from ..config.knowledge import KnowledgeConfig

from ..persistence.models import ResourceShare, Scope, ScopeClosure, ScopeKind, KnowledgeBase
from .authorization import AccessDecisionService


class KnowledgeAccess:
    def __init__(self, db):
        self.db = db

    async def ids_by_scope(self, scope_ids: set[str], *, download=False):
        result = {scope_id: set() for scope_id in scope_ids}
        if not scope_ids:
            return result
        # Personal libraries cannot be made public through a stray grant.
        libraries = select(KnowledgeBase.id).join(
            Scope, Scope.id == KnowledgeBase.scope_id
        ).where(Scope.kind != ScopeKind.PERSONAL)
        conditions = [ResourceShare.knowledge_base_id.in_(libraries)]
        if download:
            conditions.append(ResourceShare.allow_download.is_(True))
        direct = select(ResourceShare.target_scope_id, ResourceShare.knowledge_base_id).where(
            ResourceShare.target_scope_id.in_(scope_ids), *conditions
        )
        descendants = (
            select(ScopeClosure.descendant_id, ResourceShare.knowledge_base_id)
            .select_from(ResourceShare)
            .join(Scope, Scope.id == ResourceShare.target_scope_id)
            .join(ScopeClosure, ScopeClosure.ancestor_id == ResourceShare.target_scope_id)
            .where(Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)),
                   ResourceShare.include_descendants.is_(True),
                   ScopeClosure.descendant_id.in_(scope_ids), *conditions)
        )
        for scope_id, kb_id in (await self.db.execute(union_all(direct, descendants))).all():
            result[scope_id].add(kb_id)
        return result

    async def ids_for_scopes(self, scope_ids: set[str], *, download=False):
        matrix = await self.ids_by_scope(scope_ids, download=download)
        return set().union(*matrix.values())

    async def accessible_ids(self, user_id: str, *, download=False):
        if not user_id:
            return set()
        scopes = await AccessDecisionService(self.db).member_scope_ids(user_id)
        granted = await self.ids_for_scopes(scopes, download=download)
        personal = await self.db.scalars(
            select(KnowledgeBase.id).join(Scope, Scope.id == KnowledgeBase.scope_id)
            .where(Scope.kind == ScopeKind.PERSONAL, Scope.owner_user_id == user_id)
        )
        return granted | set(personal.all())

    async def bindable_ids(self, scope):
        if scope.kind == ScopeKind.PERSONAL:
            return await self.accessible_ids(scope.owner_user_id)
        return await self.ids_for_scopes({scope.id})


class KnowledgeBindingError(ValueError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


async def validate_knowledge_bindings(db, actor_user_id, agent, config, *, enabled, access, require_user_access=True):
    parsed = KnowledgeConfig.model_validate(config or {})
    if parsed.knowledge_base_ids and enabled is not True:
        raise KnowledgeBindingError('knowledge_disabled', 'Knowledge service is disabled', 503)
    scope = await db.get(Scope, agent.scope_id)
    allowed = await KnowledgeAccess(db).bindable_ids(scope) if scope else set()
    usable = await KnowledgeAccess(db).accessible_ids(actor_user_id)
    rows = []
    for kb_id in parsed.knowledge_base_ids:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None or not kb.enabled or (require_user_access and kb.id not in usable) or kb.id not in allowed:
            raise KnowledgeBindingError('knowledge_binding_unavailable', 'Knowledge base binding is unavailable', 422)
        rows.append(kb)
    return rows


async def unavailable_knowledge_bindings(db, actor_user_id, agent, *, enabled, access):
    parsed = KnowledgeConfig.model_validate(agent.knowledge_config or {})
    try:
        await validate_knowledge_bindings(db, actor_user_id, agent, parsed,
                                          enabled=enabled, access=access)
    except KnowledgeBindingError:
        return parsed.knowledge_base_ids
    return []
