# -*- coding: utf-8 -*-
"""Centralized authorization (RBAC design §7).

Routers, the orchestrator and recovery jobs must go through this
service - no ad-hoc permission SQL anywhere else.

Error semantics live at the router layer: invisible resources are 404
(no enumeration leak), visible-but-forbidden actions are 403, invalid
capability bindings 422, referenced objects 409.
"""

from collections.abc import Callable

from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from ..authorization.permissions import (
    AGENT_CREATE,
    AGENT_DELETE,
    AGENT_PUBLISH,
    AGENT_UPDATE,
    MCP_MANAGE,
    ROLE_BIND,
    SCOPE_MANAGE,
    SKILL_MANAGE,
    ROOT_ONLY_PERMISSIONS, ROOT_ADMIN_PERMISSIONS,
    MEMBERSHIP_READ, MEMBERSHIP_MANAGE, AUDIT_READ, USAGE_READ,
)
from ..persistence.models import (
    Agent,
    AgentCapabilityBinding,
    AgentDefault,
    CapabilityInstallation,
    Permission,
    ResourceShare,
    Role,
    RoleBinding,
    RolePermission,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
    User,
    UserStatus,
    Visibility,
)


_MANAGEMENT_PERMISSIONS = (
    "knowledge.manage",
    AGENT_CREATE,
    AGENT_UPDATE,
    AGENT_DELETE,
    AGENT_PUBLISH,
    SKILL_MANAGE,
    MCP_MANAGE,
    ROLE_BIND,
    SCOPE_MANAGE,
    MEMBERSHIP_READ, MEMBERSHIP_MANAGE, AUDIT_READ, USAGE_READ,
)


class AccessDecisionService:
    def __init__(self, db: AsyncSession, *, knowledge_enabled: bool | Callable[[], bool] = False) -> None:
        self._db = db
        self._knowledge_enabled = knowledge_enabled

    # ------------------------------------------------------------------
    # Role / permission checks
    # ------------------------------------------------------------------

    async def has_permission(
        self, user_id: str, scope_id: str, permission: str,
    ) -> bool:
        """True when the user holds ``permission`` in ``scope_id`` via
        a role binding on the scope itself or on an ancestor scope with
        ``include_descendants`` set."""
        db = self._db
        if permission in ROOT_ONLY_PERMISSIONS:
            scope = await db.get(Scope, scope_id)
            if scope is None or scope.kind != ScopeKind.ORGANIZATION:
                return False
        ancestors = select(ScopeClosure.ancestor_id).where(
            ScopeClosure.descendant_id == scope_id,
        ).scalar_subquery()
        return await db.scalar(
            select(RoleBinding.user_id)
            .join(Role, Role.id == RoleBinding.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                RoleBinding.user_id == user_id,
                Permission.key == permission,
                # binding on the scope itself, or on an ancestor with
                # descendant inheritance
                (RoleBinding.scope_id == scope_id)
                | (
                    RoleBinding.include_descendants
                    & RoleBinding.scope_id.in_(ancestors)
                ),
            )
            .limit(1),
        ) is not None

    async def can_manage_scope(
        self, user_id: str, scope_id: str, permission: str,
    ) -> bool:
        return await self.has_permission(user_id, scope_id, permission)

    async def active_root_admin_ids(
        self, *, exclude_user_id: str | None = None,
    ) -> set[str]:
        """Active users whose combined root roles allow system recovery."""
        db = self._db
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        if root is None:
            return set()
        query = (
            select(User.id)
            .join(RoleBinding, RoleBinding.user_id == User.id)
            .join(Role, Role.id == RoleBinding.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                User.status == UserStatus.ACTIVE,
                Permission.key.in_(ROOT_ADMIN_PERMISSIONS),
                RoleBinding.scope_id == root.id,
            )
            .group_by(User.id)
            .having(func.count(func.distinct(Permission.key)) == len(ROOT_ADMIN_PERMISSIONS))
        )
        if exclude_user_id is not None:
            query = query.where(User.id != exclude_user_id)
        return set((await db.scalars(query)).all())

    async def effective_permissions(self, user_id: str) -> set[str]:
        """All permission keys the user holds in any scope (used by
        ``GET /api/auth/me/permissions``; scope-level decisions still
        go through has_permission)."""
        db = self._db
        rows = (
            await db.scalars(
                select(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(Role, Role.id == RolePermission.role_id)
                .join(RoleBinding, RoleBinding.role_id == Role.id)
                .where(RoleBinding.user_id == user_id),
            )
        ).all()
        return set(rows)

    async def permissions_for_scope(
        self, user_id: str, scope_id: str,
    ) -> set[str]:
        """Effective permission keys in one exact scope.

        Unlike ``effective_permissions``, this preserves the scope boundary
        needed by management UIs and never turns a grant in one department
        into an action in another visible department.
        """
        return (await self.permissions_for_scopes(user_id, {scope_id})).get(
            scope_id, set(),
        )

    async def permissions_for_scopes(
        self, user_id: str, scope_ids: set[str] | list[str],
    ) -> dict[str, set[str]]:
        """Resolve an exact-scope permission matrix in one query."""
        requested = set(scope_ids)
        result = {scope_id: set() for scope_id in requested}
        if not requested:
            return result
        rows = (
            await self._db.execute(
                select(ScopeClosure.descendant_id, Permission.key)
                .join(
                    RoleBinding,
                    (RoleBinding.scope_id == ScopeClosure.ancestor_id)
                    & (
                        (ScopeClosure.depth == 0)
                        | RoleBinding.include_descendants
                    ),
                )
                .join(Role, Role.id == RoleBinding.role_id)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(
                    Permission,
                    Permission.id == RolePermission.permission_id,
                )
                .where(
                    RoleBinding.user_id == user_id,
                    ScopeClosure.descendant_id.in_(requested),
                )
                .distinct(),
            )
        ).all()
        for scope_id, permission in rows:
            result[scope_id].add(permission)
        organization_ids = set((await self._db.scalars(
            select(Scope.id).where(Scope.id.in_(requested), Scope.kind == ScopeKind.ORGANIZATION),
        )).all())
        for scope_id in requested - organization_ids:
            result[scope_id].difference_update(ROOT_ONLY_PERMISSIONS)
        return result

    # ------------------------------------------------------------------
    # Membership helpers
    # ------------------------------------------------------------------

    async def member_scope_ids(self, user_id: str) -> set[str]:
        rows = (
            await self._db.scalars(
                select(ScopeMembership.scope_id).where(
                    ScopeMembership.user_id == user_id,
                ),
            )
        ).all()
        return set(rows)

    async def accessible_scope_ids(self, user_id: str) -> set[str]:
        """Scopes the user may select in product management views.

        Membership exposes the directly joined organization scope and
        its ancestors.  A management role exposes its bound scope and,
        only when requested by the binding, its descendants.  These two
        paths are deliberately independent so an inherited no-op member
        role cannot reveal sibling departments.
        """
        db = self._db
        member_scopes = select(ScopeMembership.scope_id).where(
            ScopeMembership.user_id == user_id,
        )
        membership_reachable = select(ScopeClosure.ancestor_id).where(
            ScopeClosure.descendant_id.in_(member_scopes),
        )

        managed_bindings = (
            select(RoleBinding.scope_id)
            .join(Role, Role.id == RoleBinding.role_id)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                RoleBinding.user_id == user_id,
                Permission.key.in_(_MANAGEMENT_PERMISSIONS),
            )
            .distinct()
        )
        inherited_bindings = managed_bindings.where(
            RoleBinding.include_descendants,
        )
        managed_descendants = select(ScopeClosure.descendant_id).where(
            ScopeClosure.ancestor_id.in_(inherited_bindings),
        )

        organization_ids = (
            await db.scalars(
                select(Scope.id).where(
                    Scope.kind.in_((
                        ScopeKind.ORGANIZATION,
                        ScopeKind.DEPARTMENT,
                    )),
                    Scope.id.in_(membership_reachable)
                    | Scope.id.in_(managed_bindings)
                    | Scope.id.in_(managed_descendants),
                ),
            )
        ).all()
        result = set(organization_ids)
        personal_id = await self.personal_scope_id(user_id)
        if personal_id is not None:
            result.add(personal_id)
        return result

    async def accessible_capability_scope_ids(
        self, user_id: str,
    ) -> set[str]:
        """Membership-visible scope names for management error disclosure.

        This is not resource use authority. Skill and MCP use and bindings
        always require accessible_capability_ids / bindable_capability_ids.
        """
        return (
            await self.accessible_capability_scope_ids_for_users({user_id})
        )[user_id]

    async def accessible_capability_scope_ids_for_users(
        self, user_ids: set[str] | list[str],
    ) -> dict[str, set[str]]:
        """Membership-derived capability scopes for several owners."""
        requested = set(user_ids)
        result = {user_id: set() for user_id in requested}
        if not requested:
            return result
        rows = (
            await self._db.execute(
                select(
                    ScopeMembership.user_id,
                    ScopeClosure.ancestor_id,
                )
                .join(
                    ScopeClosure,
                    ScopeClosure.descendant_id
                    == ScopeMembership.scope_id,
                )
                .join(Scope, Scope.id == ScopeClosure.ancestor_id)
                .where(
                    ScopeMembership.user_id.in_(requested),
                    Scope.kind.in_((
                        ScopeKind.ORGANIZATION,
                        ScopeKind.DEPARTMENT,
                    )),
                )
                .distinct(),
            )
        ).all()
        for user_id, scope_id in rows:
            result[user_id].add(scope_id)
        return result

    async def capability_ids_by_scope(self, scope_ids: set[str]) -> dict[str, set[str]]:
        """Explicit Skill and MCP grants; ownership is not use authority."""
        result = {scope_id: set() for scope_id in scope_ids}
        if not scope_ids:
            return result
        shared = (select(ResourceShare.target_scope_id, CapabilityInstallation.id)
                  .join(CapabilityInstallation, CapabilityInstallation.id == ResourceShare.capability_id)
                  .join(Scope, Scope.id == CapabilityInstallation.scope_id)
                  .where(ResourceShare.target_scope_id.in_(scope_ids), Scope.kind != ScopeKind.PERSONAL,
                         CapabilityInstallation.capability_type.in_(("skill", "mcp"))))
        organization_shared = (
            select(ScopeClosure.descendant_id, CapabilityInstallation.id)
            .select_from(ResourceShare)
            .join(Scope, Scope.id == ResourceShare.target_scope_id)
            .join(ScopeClosure, ScopeClosure.ancestor_id == ResourceShare.target_scope_id)
            .join(CapabilityInstallation, CapabilityInstallation.id == ResourceShare.capability_id)
            .where(Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)), ResourceShare.include_descendants,
                   ScopeClosure.descendant_id.in_(scope_ids),
                   CapabilityInstallation.capability_type.in_(("skill", "mcp")),
                   CapabilityInstallation.scope_id.in_(select(Scope.id).where(Scope.kind != ScopeKind.PERSONAL)))
        )
        for scope_id, capability_id in (await self._db.execute(union_all(shared, organization_shared))).all():
            result[scope_id].add(capability_id)
        return result

    async def capability_ids_for_scopes(self, scope_ids: set[str]) -> set[str]:
        matrix = await self.capability_ids_by_scope(scope_ids)
        return set().union(*matrix.values())

    async def accessible_capability_ids(self, user_id: str) -> set[str]:
        return await self.capability_ids_for_scopes(await self.member_scope_ids(user_id))

    async def bindable_capability_ids(self, scope: Scope) -> set[str]:
        if scope.kind == ScopeKind.PERSONAL:
            return await self.accessible_capability_ids(scope.owner_user_id) if scope.owner_user_id else set()
        return await self.capability_ids_for_scopes({scope.id})

    async def shared_agent_ids(self, user_id: str) -> set[str]:
        members = await self.member_scope_ids(user_id)
        direct = select(ResourceShare.agent_id).where(
            ResourceShare.target_scope_id.in_(members), ResourceShare.agent_id.is_not(None),
        )
        organization = (select(ResourceShare.agent_id)
            .join(Scope, Scope.id == ResourceShare.target_scope_id)
            .join(ScopeClosure, ScopeClosure.ancestor_id == Scope.id)
            .where(Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)), ResourceShare.include_descendants,
                   ScopeClosure.descendant_id.in_(members), ResourceShare.agent_id.is_not(None)))
        return set((await self._db.scalars(union_all(direct, organization))).all())

    async def _descendant_ids(self, scope_id: str) -> set[str]:
        rows = (
            await self._db.scalars(
                select(ScopeClosure.descendant_id).where(
                    ScopeClosure.ancestor_id == scope_id,
                    ScopeClosure.depth > 0,
                ),
            )
        ).all()
        return set(rows)

    async def personal_scope_id(self, user_id: str) -> str | None:
        scope = await self._db.scalar(
            select(Scope).where(
                Scope.kind == ScopeKind.PERSONAL,
                Scope.owner_user_id == user_id,
            ),
        )
        return scope.id if scope else None

    # ------------------------------------------------------------------
    # Agent decisions (RBAC design §7)
    # ------------------------------------------------------------------

    async def can_view_agent(self, user_id: str, agent_id: str) -> bool:
        agent = await self._db.get(Agent, agent_id)
        if agent is None or agent.deleted_at is not None:
            return False
        return await self._agent_visible(user_id, agent)

    async def can_use_agent(self, user_id: str, agent_id: str) -> bool:
        agent = await self._db.get(Agent, agent_id)
        if agent is None or agent.deleted_at is not None:
            return False
        scope = await self._db.get(Scope, agent.scope_id)
        if scope is None:
            return False
        if scope.kind == ScopeKind.PERSONAL:
            return scope.owner_user_id == user_id
        if agent.visibility == Visibility.PRIVATE:
            # Creators may test their own drafts; management alone is not use.
            return agent.created_by == user_id
        return agent.id in await self.shared_agent_ids(user_id)

    async def can_manage_agent(self, user_id: str, agent_id: str) -> bool:
        agent = await self._db.get(Agent, agent_id)
        if agent is None or agent.deleted_at is not None:
            return False
        return await self._agent_manageable(user_id, agent)

    async def can_delete_agent(self, user_id: str, agent_id: str) -> bool:
        """Personal Agents are owner-deletable; organization Agents
        require the dedicated agent.delete permission."""
        agent = await self._db.get(Agent, agent_id)
        if agent is None or agent.deleted_at is not None:
            return False
        scope = await self._db.get(Scope, agent.scope_id)
        if scope is None:
            return False
        if scope.kind == ScopeKind.PERSONAL:
            return scope.owner_user_id == user_id
        return await self.has_permission(
            user_id, agent.scope_id, AGENT_DELETE,
        )

    async def _agent_manageable(self, user_id: str, agent: Agent) -> bool:
        if agent.deleted_at is not None:
            return False
        scope = await self._db.get(Scope, agent.scope_id)
        if scope is None:
            return False
        if scope.kind == ScopeKind.PERSONAL:
            return scope.owner_user_id == user_id
        return await self.has_permission(
            user_id, agent.scope_id, AGENT_UPDATE,
        )

    async def _agent_visible(self, user_id: str, agent: Agent) -> bool:
        if agent.deleted_at is not None:
            return False
        db = self._db
        scope = await db.get(Scope, agent.scope_id)
        if scope is None:
            return False

        if scope.kind == ScopeKind.PERSONAL:
            # Personal agents are strictly private to their owner.
            return scope.owner_user_id == user_id

        if agent.created_by == user_id:
            return True
        if await self.has_permission(user_id, agent.scope_id, AGENT_UPDATE):
            return True
        return (agent.visibility == Visibility.PUBLIC
                and agent.id in await self.shared_agent_ids(user_id))

    async def list_visible_agents(self, user_id: str) -> list[Agent]:
        """List visible Agents with a fixed number of set-based queries."""
        rows = (
            await self._db.execute(
                select(Agent, Scope)
                .join(Scope, Scope.id == Agent.scope_id)
                .where(Agent.deleted_at.is_(None))
                .order_by(Agent.created_at),
            )
        ).all()
        organization_scope_ids = {
            scope.id for _, scope in rows
            if scope.kind != ScopeKind.PERSONAL
        }
        permission_matrix = await self.permissions_for_scopes(
            user_id, organization_scope_ids,
        )
        managed_scope_ids = {
            scope_id for scope_id, permissions in permission_matrix.items()
            if AGENT_UPDATE in permissions
        }
        shared_ids = await self.shared_agent_ids(user_id)
        visible = []
        for agent, scope in rows:
            if scope.kind == ScopeKind.PERSONAL:
                if scope.owner_user_id == user_id:
                    visible.append(agent)
                continue
            if (agent.scope_id in managed_scope_ids or agent.created_by == user_id
                    or (agent.visibility == Visibility.PUBLIC and agent.id in shared_ids)):
                visible.append(agent)
        return visible

    # ------------------------------------------------------------------
    # Default agent resolution (RBAC design §6.1)
    # ------------------------------------------------------------------

    async def resolve_default_agent(self, user_id: str) -> Agent | None:
        """Resolve defaults through deterministic organization ancestry."""
        db = self._db
        personal_id = await self.personal_scope_id(user_id)

        candidates: list[str] = []
        if personal_id:
            candidates.append(personal_id)

        # Direct department memberships: deeper scopes first, then id.
        direct_rows = (
            await db.execute(
                select(
                    ScopeMembership.scope_id,
                    func.max(ScopeClosure.depth).label("tree_depth"),
                )
                .join(Scope, Scope.id == ScopeMembership.scope_id)
                .join(
                    ScopeClosure,
                    ScopeClosure.descendant_id == ScopeMembership.scope_id,
                )
                .where(
                    ScopeMembership.user_id == user_id,
                    Scope.kind == ScopeKind.DEPARTMENT,
                )
                .group_by(ScopeMembership.scope_id)
                .order_by(
                    func.max(ScopeClosure.depth).desc(),
                    ScopeMembership.scope_id,
                ),
            )
        ).all()
        direct_ids = [row.scope_id for row in direct_rows]
        candidates.extend(direct_ids)

        # Then all department ancestors, globally nearest first.  The
        # minimum distance handles shared ancestors and candidate
        # de-duplication without per-membership queries.
        if direct_ids:
            ancestor_rows = (
                await db.execute(
                    select(
                        ScopeClosure.ancestor_id,
                        func.min(ScopeClosure.depth).label("distance"),
                    )
                    .join(Scope, Scope.id == ScopeClosure.ancestor_id)
                    .where(
                        ScopeClosure.descendant_id.in_(direct_ids),
                        ScopeClosure.depth > 0,
                        Scope.kind == ScopeKind.DEPARTMENT,
                    )
                    .group_by(ScopeClosure.ancestor_id)
                    .order_by(
                        func.min(ScopeClosure.depth),
                        ScopeClosure.ancestor_id,
                    ),
                )
            ).all()
            candidates.extend(row.ancestor_id for row in ancestor_rows)

        roots = (
            await db.scalars(
                select(Scope.id)
                .where(Scope.kind == ScopeKind.ORGANIZATION)
                .order_by(Scope.id),
            )
        ).all()
        candidates.extend(roots)

        defaults = {
            default.scope_id: default.agent_id
            for default in (
                await db.scalars(
                    select(AgentDefault).where(
                        AgentDefault.scope_id.in_(candidates),
                    ),
                )
            ).all()
        } if candidates else {}

        seen: set[str] = set()
        for scope_id in candidates:
            if scope_id in seen:
                continue
            seen.add(scope_id)
            agent_id = defaults.get(scope_id)
            if agent_id is None:
                continue
            if await self.can_use_agent(user_id, agent_id):
                return await db.get(Agent, agent_id)

        # A platform-seeded assistant is only a final fallback. It remains a
        # normal scoped Agent and must pass the same visibility check.
        workspace_assistants = (
            await db.scalars(
                select(Agent)
                .where(
                    Agent.system_key == "workspace_assistant",
                    Agent.scope_id.in_(roots),
                )
                .order_by(Agent.scope_id)
            )
        ).all() if roots else []
        for assistant in workspace_assistants:
            if await self.can_use_agent(user_id, assistant.id):
                return assistant
        return None

    # ------------------------------------------------------------------
    # Capabilities (RBAC design §8)
    # ------------------------------------------------------------------

    async def unavailable_capability_ids(
        self, user_id: str, agent_id: str,
    ) -> list[str]:
        """Recheck both the Agent binding scope and the current caller's access."""
        agent = await self._db.get(Agent, agent_id)
        if agent is None:
            return []
        from .knowledge_access import unavailable_knowledge_bindings
        enabled = self._knowledge_enabled() if callable(self._knowledge_enabled) else self._knowledge_enabled
        return ((await self.unavailable_capabilities_for_agents(user_id, [agent]))[agent_id]
                + await unavailable_knowledge_bindings(self._db, user_id, agent,
                    enabled=enabled, access=self))

    async def unavailable_capabilities_for_agents(
        self, user_id: str, agents: list[Agent],
    ) -> dict[str, list[str]]:
        result = {agent.id: [] for agent in agents}
        if not agents:
            return result
        scopes = {scope.id: scope for scope in (await self._db.scalars(
            select(Scope).where(Scope.id.in_({agent.scope_id for agent in agents})),
        )).all()}
        user_ids = {user_id} | {scope.owner_user_id for scope in scopes.values() if scope.owner_user_id}
        memberships = {uid: set() for uid in user_ids}
        for uid, sid in (await self._db.execute(select(ScopeMembership.user_id, ScopeMembership.scope_id)
                                               .where(ScopeMembership.user_id.in_(user_ids)))).all():
            memberships[uid].add(sid)
        matrix = await self.capability_ids_by_scope(set(scopes) | set().union(*memberships.values()))
        caller_ids = set().union(*(matrix[sid] for sid in memberships[user_id]))
        bindable = {
            sid: (set().union(*(matrix[member_id] for member_id in memberships.get(scope.owner_user_id, set())))
                  if scope.kind == ScopeKind.PERSONAL else matrix[sid])
            for sid, scope in scopes.items()
        }
        by_id = {agent.id: agent for agent in agents}
        rows = (await self._db.execute(
            select(AgentCapabilityBinding.agent_id, CapabilityInstallation.id, CapabilityInstallation.enabled)
            .join(CapabilityInstallation, CapabilityInstallation.id == AgentCapabilityBinding.capability_installation_id)
            .where(AgentCapabilityBinding.agent_id.in_(by_id)),
        )).all()
        for row in rows:
            allowed = bindable.get(by_id[row.agent_id].scope_id, set())
            if not row.enabled or row.id not in allowed or row.id not in caller_ids:
                result[row.agent_id].append(row.id)
        return {key: sorted(value) for key, value in result.items()}

    async def resolve_accessible_capabilities(
        self, user_id: str, agent_id: str,
    ) -> list[CapabilityInstallation]:
        """Enabled bindings with current Agent, caller and binding-scope access."""
        agent = await self._db.get(Agent, agent_id)
        if agent is None or not await self.can_use_agent(user_id, agent_id):
            return []
        unavailable = set(await self.unavailable_capability_ids(
            user_id, agent_id,
        ))
        rows = (
            await self._db.scalars(
                select(CapabilityInstallation)
                .join(
                    AgentCapabilityBinding,
                    AgentCapabilityBinding.capability_installation_id
                    == CapabilityInstallation.id,
                )
                .where(
                    AgentCapabilityBinding.agent_id == agent_id,
                    CapabilityInstallation.enabled,
                    CapabilityInstallation.id.not_in(unavailable),
                )
            )
        ).all()
        return list(rows)
