# -*- coding: utf-8 -*-
"""Idempotent RBAC bootstrap (RBAC design §4.2/§5).

Covers both the fresh-database path (Alembic + startup call)
and ongoing registration: root organization scope, role/permission
seed, and per-user personal scope + membership + role binding.
"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.permissions import ROLE_GRANTS, SYSTEM_PERMISSION_LABELS
from workmate_product.persistence.models import (
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
    User,
)

logger = logging.getLogger(__name__)

ROOT_SCOPE_NAME = "workmate"


async def get_root_scope(db: AsyncSession) -> Scope | None:
    return await db.scalar(
        select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
    )


async def get_personal_scope(db: AsyncSession, user_id: str) -> Scope | None:
    return await db.scalar(
        select(Scope).where(
            Scope.kind == ScopeKind.PERSONAL,
            Scope.owner_user_id == user_id,
        ),
    )


async def create_scope(
    db: AsyncSession,
    *,
    kind: ScopeKind,
    name: str,
    parent: Scope | None = None,
    owner_user_id: str | None = None,
) -> Scope:
    """Create a scope plus its closure edges (self and to ancestors)."""
    scope = Scope(
        id=str(uuid.uuid4()),
        kind=kind,
        name=name,
        normalized_name=name.strip().casefold(),
        parent_id=parent.id if parent else None,
        owner_user_id=owner_user_id,
    )
    db.add(scope)
    await db.flush()
    db.add(ScopeClosure(
        ancestor_id=scope.id, descendant_id=scope.id, depth=0,
    ))
    if parent is not None:
        edges = await db.scalars(
            select(ScopeClosure).where(ScopeClosure.descendant_id == parent.id),
        )
        for edge in edges:
            db.add(ScopeClosure(
                ancestor_id=edge.ancestor_id,
                descendant_id=scope.id,
                depth=edge.depth + 1,
            ))
    await db.flush()
    return scope


async def _ensure_roles(db: AsyncSession) -> dict[str, Role]:
    for key, permission_keys in ROLE_GRANTS.items():
        role = await db.scalar(select(Role).where(Role.key == key))
        if role is None:
            role = Role(
                id=str(uuid.uuid4()), key=key,
                display_name="组织管理员" if key == "admin" else key,
                is_system=True,
            )
            db.add(role)
            await db.flush()
        else:
            role.is_system = True
            if key == "admin":
                role.display_name = "组织管理员"
        held = {
            perm.key
            for perm in (
                await db.scalars(
                    select(Permission)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id == role.id),
                )
            ).all()
        }
        for permission_key in permission_keys:
            permission = await db.scalar(
                select(Permission).where(Permission.key == permission_key),
            )
            if permission is None:
                permission = Permission(
                    id=str(uuid.uuid4()), key=permission_key,
                    display_name=SYSTEM_PERMISSION_LABELS[permission_key],
                    is_system=True,
                )
                db.add(permission)
                await db.flush()
            else:
                permission.is_system = True
                permission.display_name = SYSTEM_PERMISSION_LABELS[permission_key]
            if permission_key in held:
                continue
            db.add(RolePermission(
                id=str(uuid.uuid4()),
                role_id=role.id,
                permission_id=permission.id,
            ))
    await db.flush()
    return {
        role.key: role
        for role in (await db.scalars(select(Role))).all()
    }


async def ensure_user_scopes(
    db: AsyncSession, user: User, *, first_user: bool = False,
) -> None:
    """Registration path (RBAC design §4.2): personal scope, root
    membership, and a first-user admin binding on the root scope."""
    await _ensure_user_scopes(db, user, first_user=first_user)
    await db.commit()


async def _ensure_user_scopes(
    db: AsyncSession, user: User, *, first_user: bool = False,
) -> None:
    """Add user scope records without owning the caller's transaction."""
    root = await get_root_scope(db)
    if root is None:
        root = await create_scope(
            db, kind=ScopeKind.ORGANIZATION, name=ROOT_SCOPE_NAME,
        )

    personal = await get_personal_scope(db, user.id)
    if personal is None:
        personal = await create_scope(
            db,
            kind=ScopeKind.PERSONAL,
            name=f"personal:{user.id}",
            owner_user_id=user.id,
        )

    membership = await db.get(ScopeMembership, (root.id, user.id))
    if membership is None:
        db.add(ScopeMembership(scope_id=root.id, user_id=user.id))

    roles = await _ensure_roles(db)
    if not first_user:
        return
    role = roles["admin"]
    binding = await db.get(RoleBinding, (user.id, role.id, root.id))
    if binding is None:
        db.add(RoleBinding(
            user_id=user.id, role_id=role.id, scope_id=root.id,
        ))


async def ensure_bootstrap(db: AsyncSession) -> None:
    """Idempotently seed the root scope and system roles/permissions.

    Account scopes and grants belong to explicit user provisioning, never startup.
    This startup use case owns its existing commit; the PG lock is held only
    by that transaction and rolls back with the caller's session on failure.
    """
    if db.get_bind().dialect.name == "postgresql":
        # There may be no root row to lock on first startup. Serialize Gateway
        # seed transactions within this schema, not across isolated deployments.
        await db.execute(select(func.pg_advisory_xact_lock(
            func.hashtext(func.current_schema()), func.hashtext("workmate:rbac-bootstrap"),
        )))
    # Registration/user provisioning already locks root before role writes.
    # Honor that order during a rolling restart without changing their grants.
    root = await db.scalar(select(Scope).where(
        Scope.kind == ScopeKind.ORGANIZATION,
    ).with_for_update())
    if root is None:
        root = await create_scope(
            db, kind=ScopeKind.ORGANIZATION, name=ROOT_SCOPE_NAME,
        )
        logger.info("created root organization scope %s", root.id)

    await _ensure_roles(db)
    await db.commit()
