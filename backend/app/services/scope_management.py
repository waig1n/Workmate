# -*- coding: utf-8 -*-
"""Department scope tree management with closure-table maintenance."""

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.permissions import SCOPE_MANAGE
from workmate_product.persistence.models import (
    Agent,
    AgentDefault,
    CapabilityInstallation,
    RoleBinding,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
)
from .access_decision import AccessDecisionService
from .security_audit import SecurityAuditService
from .administration_guard import actor_authorized
from .scope_access_impact import move_access_impact


class ScopeManagementError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409, **data):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


def normalize_scope_name(name: str) -> str:
    return name.strip().casefold()


class ScopeManagementService:
    def __init__(self, db: AsyncSession, actor_id: str) -> None:
        self._db = db
        self._actor_id = actor_id
        self._access = AccessDecisionService(db)

    def _audit(self, event_type: str, scope: Scope, **metadata) -> None:
        SecurityAuditService(self._db).record(
            event_type, actor_user_id=self._actor_id, scope_id=scope.id,
            metadata=metadata,
        )

    async def _lock_tree(self, expected_version: int | None = None) -> None:
        """Serialize tree mutations on the root scope row so concurrent
        create/rename/move/delete cannot validate against stale state
        (user management plan Task 7).  PostgreSQL blocks concurrent
        writers here; SQLite ignores FOR UPDATE, so only the branch
        semantics are covered there - the concurrency evidence lives in
        the PostgreSQL integration tests."""
        root = await self._db.scalar(
            select(Scope)
            .where(Scope.kind == ScopeKind.ORGANIZATION)
            .with_for_update().execution_options(populate_existing=True),
        )
        if root is None or (expected_version is not None and root.tree_version != expected_version):
            raise ScopeManagementError("version_conflict", "Organization structure changed; reload before saving.")
        result = await self._db.execute(update(Scope).where(
            Scope.id == root.id, Scope.tree_version == root.tree_version,
        ).values(tree_version=root.tree_version + 1))
        if result.rowcount != 1:
            raise ScopeManagementError("version_conflict", "Organization structure changed; reload before saving.")

    async def _commit(self) -> None:
        """Commit, mapping the sibling-name partial unique index (the
        backstop behind _ensure_name_available) to a stable 409."""
        try:
            await self._db.commit()
        except IntegrityError:
            await self._db.rollback()
            raise ScopeManagementError(
                "scope_name_taken", "Department name already exists.", 409,
            ) from None

    async def create_department(self, *, parent_id: str, name: str) -> Scope:
        await self._lock_tree()
        parent = await self._load_parent(parent_id)
        await self._require_manage(parent.id)
        normalized = normalize_scope_name(name)
        if not normalized:
            raise ScopeManagementError("invalid_scope_name", "Department name must not be blank.", 422)
        await self._ensure_name_available(parent.id, normalized)
        scope = Scope(
            id=str(uuid.uuid4()), kind=ScopeKind.DEPARTMENT,
            parent_id=parent.id, name=name.strip(),
            normalized_name=normalized,
        )
        self._db.add(scope)
        await self._db.flush()
        ancestors = (await self._db.scalars(
            select(ScopeClosure).where(
                ScopeClosure.descendant_id == parent.id,
            ),
        )).all()
        self._db.add(ScopeClosure(
            ancestor_id=scope.id, descendant_id=scope.id, depth=0,
        ))
        for edge in ancestors:
            self._db.add(ScopeClosure(
                ancestor_id=edge.ancestor_id, descendant_id=scope.id,
                depth=edge.depth + 1,
            ))
        self._audit("scope.create", scope, parent_id=parent.id)
        await self._commit()
        await self._db.refresh(scope)
        return scope

    async def rename(self, scope: Scope, name: str, expected_version: int | None = None) -> Scope:
        await self._lock_tree(expected_version)
        await self._db.refresh(scope)
        self._require_department(scope)
        await self._require_manage(scope.id)
        normalized = normalize_scope_name(name)
        if not normalized:
            raise ScopeManagementError("invalid_scope_name", "Department name must not be blank.", 422)
        await self._ensure_name_available(
            scope.parent_id, normalized, exclude_id=scope.id,
        )
        old_name = scope.name
        scope.name = name.strip()
        scope.normalized_name = normalized
        self._audit("scope.rename", scope, old_name=old_name, new_name=scope.name)
        await self._commit()
        await self._db.refresh(scope)
        return scope

    async def move(self, scope: Scope, new_parent_id: str, expected_version: int | None = None) -> Scope:
        await self._lock_tree(expected_version)
        await self._db.refresh(scope)
        self._require_department(scope)
        old_parent = await self._db.get(Scope, scope.parent_id)
        new_parent = await self._load_parent(new_parent_id)
        for target in (old_parent, scope, new_parent):
            await self._require_manage(target.id)
        if scope.id == new_parent.id or await self._db.get(
            ScopeClosure, (scope.id, new_parent.id),
        ) is not None:
            raise ScopeManagementError(
                "scope_cycle", "A scope cannot move below itself.", 409,
            )
        await self._ensure_name_available(
            new_parent.id, scope.normalized_name, exclude_id=scope.id,
        )
        if scope.parent_id == new_parent.id:
            await self._db.commit()
            return scope

        impact = await self.move_preview(scope, new_parent_id)
        if impact["capability_blockers"]:
            raise ScopeManagementError("scope_capability_conflict", "Moving would invalidate existing Agent capability bindings.",
                                       blockers={"capability_bindings": impact["capability_blockers"]})

        subtree_edges = (await self._db.scalars(
            select(ScopeClosure).where(ScopeClosure.ancestor_id == scope.id),
        )).all()
        descendants = {edge.descendant_id: edge.depth for edge in subtree_edges}
        outside_ancestors = (await self._db.scalars(
            select(ScopeClosure).where(
                ScopeClosure.descendant_id == scope.id,
                ScopeClosure.ancestor_id != scope.id,
            ),
        )).all()
        outside_ids = {edge.ancestor_id for edge in outside_ancestors}
        if outside_ids:
            await self._db.execute(delete(ScopeClosure).where(
                ScopeClosure.ancestor_id.in_(outside_ids),
                ScopeClosure.descendant_id.in_(descendants),
            ))
        new_ancestors = (await self._db.scalars(
            select(ScopeClosure).where(
                ScopeClosure.descendant_id == new_parent.id,
            ),
        )).all()
        for ancestor in new_ancestors:
            for descendant_id, relative_depth in descendants.items():
                self._db.add(ScopeClosure(
                    ancestor_id=ancestor.ancestor_id,
                    descendant_id=descendant_id,
                    depth=ancestor.depth + 1 + relative_depth,
                ))
        scope.parent_id = new_parent.id
        self._audit("scope.move", scope, old_parent_id=old_parent.id,
                    new_parent_id=new_parent.id)
        await self._commit()
        await self._db.refresh(scope)
        return scope

    async def delete(self, scope: Scope, expected_version: int | None = None) -> None:
        await self._lock_tree(expected_version)
        await self._db.refresh(scope)
        self._require_department(scope)
        await self._require_manage(scope.id)
        blockers = {
            "children": await self._count(Scope, Scope.parent_id == scope.id),
            "memberships": await self._count(
                ScopeMembership, ScopeMembership.scope_id == scope.id,
            ),
            "role_bindings": await self._count(
                RoleBinding, RoleBinding.scope_id == scope.id,
            ),
            "agents": await self._count(Agent, Agent.scope_id == scope.id),
            "capability_installations": await self._count(
                CapabilityInstallation,
                CapabilityInstallation.scope_id == scope.id,
            ),
            "agent_defaults": await self._count(
                AgentDefault, AgentDefault.scope_id == scope.id,
            ),
        }
        if any(blockers.values()):
            raise ScopeManagementError(
                "scope_not_empty", "Scope has dependent records.",
                blockers=blockers,
            )
        await self._db.execute(delete(ScopeClosure).where(
            (ScopeClosure.ancestor_id == scope.id)
            | (ScopeClosure.descendant_id == scope.id),
        ))
        await self._db.delete(scope)
        # The audited scope is being deleted in this same transaction;
        # writing its id would leave a dangling reference wherever the
        # database does not rewrite it (SQLite never enforces the FK).
        # metadata preserves the deleted scope's identity instead.
        SecurityAuditService(self._db).record(
            "scope.delete",
            actor_user_id=self._actor_id,
            scope_id=None,
            metadata={
                "parent_id": scope.parent_id,
                "deleted_scope_id": scope.id,
            },
        )
        await self._db.commit()

    async def _load_parent(self, scope_id: str) -> Scope:
        scope = await self._db.get(Scope, scope_id)
        if scope is None:
            raise ScopeManagementError(
                "scope_not_found", "Scope not found.", 404,
            )
        if scope.kind not in (ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT):
            raise ScopeManagementError(
                "invalid_scope_parent", "Personal scopes cannot be parents.", 422,
            )
        return scope

    @staticmethod
    def _require_department(scope: Scope) -> None:
        if scope.kind == ScopeKind.ORGANIZATION:
            raise ScopeManagementError(
                "root_scope_immutable", "Root scope is immutable.", 409,
            )
        if scope.kind != ScopeKind.DEPARTMENT:
            raise ScopeManagementError(
                "personal_scope_forbidden", "Personal scope is immutable.", 409,
            )

    async def _require_manage(self, scope_id: str) -> None:
        if not await actor_authorized(self._db, self._actor_id, [SCOPE_MANAGE], scope_id):
            raise ScopeManagementError(
                "scope_manage_required", "Scope management permission required.",
                403, scope_id=scope_id,
            )

    async def _ensure_name_available(
        self, parent_id: str, normalized: str, exclude_id: str | None = None,
    ) -> None:
        query = select(Scope.id).where(
            Scope.kind == ScopeKind.DEPARTMENT,
            Scope.parent_id == parent_id,
            Scope.normalized_name == normalized,
        )
        if exclude_id:
            query = query.where(Scope.id != exclude_id)
        if await self._db.scalar(query.limit(1)):
            raise ScopeManagementError(
                "scope_name_taken", "Department name already exists.", 409,
            )

    async def move_preview(self, scope: Scope, new_parent_id: str) -> dict:
        self._require_department(scope)
        parent = await self._load_parent(new_parent_id)
        for scope_id in (scope.id, scope.parent_id, parent.id):
            await self._require_manage(scope_id)
        descendants = set((await self._db.scalars(select(ScopeClosure.descendant_id).where(
            ScopeClosure.ancestor_id == scope.id,
        ))).all())
        if parent.id in descendants:
            raise ScopeManagementError("scope_cycle", "A scope cannot move below itself.")
        await self._ensure_name_available(parent.id, scope.normalized_name, exclude_id=scope.id)
        new_ancestors = set((await self._db.scalars(select(ScopeClosure.ancestor_id).where(
            ScopeClosure.descendant_id == parent.id,
        ))).all())
        old_ancestors = set((await self._db.scalars(select(ScopeClosure.ancestor_id).where(
            ScopeClosure.descendant_id == scope.id, ScopeClosure.ancestor_id != scope.id,
        ))).all())
        access_impact = await move_access_impact(
            self._db, descendants, old_ancestors, new_ancestors,
        )
        root_version = await self._db.scalar(select(Scope.tree_version).where(Scope.kind == ScopeKind.ORGANIZATION))
        return {
            "tree_version": root_version, "old_parent_id": scope.parent_id, "new_parent_id": parent.id,
            "descendant_count": len(descendants),
            "member_count": int(await self._db.scalar(select(func.count(func.distinct(ScopeMembership.user_id))).where(ScopeMembership.scope_id.in_(descendants))) or 0),
            "agent_count": await self._count(Agent, Agent.scope_id.in_(descendants)),
            "removed_inherited_bindings": await self._count(RoleBinding, RoleBinding.scope_id.in_(old_ancestors - new_ancestors) & RoleBinding.include_descendants),
            "added_inherited_bindings": await self._count(RoleBinding, RoleBinding.scope_id.in_(new_ancestors - old_ancestors) & RoleBinding.include_descendants),
            **access_impact,
        }

    async def _count(self, model, condition) -> int:
        return int(await self._db.scalar(
            select(func.count()).select_from(model).where(condition),
        ) or 0)
