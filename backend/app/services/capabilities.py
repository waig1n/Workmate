# -*- coding: utf-8 -*-
"""Shared capability visibility and knowledge-storage principal helpers."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.permissions import MCP_MANAGE, SKILL_MANAGE
from workmate_product.persistence.models import (
    CapabilityInstallation,
    CapabilityType,
    Scope,
    ScopeKind,
    User,
)
from ..services.access_decision import AccessDecisionService

SERVICE_AGENT_PRINCIPAL = "workmate-service"


def agent_runtime_principal(scope: Scope) -> str:
    """Runtime owner for an agent - never an authorization fact."""
    if scope.kind == ScopeKind.PERSONAL:
        return scope.owner_user_id
    return SERVICE_AGENT_PRINCIPAL


async def list_visible(
    db: AsyncSession,
    access: AccessDecisionService,
    user: User,
    capability_type: CapabilityType | None = None,
) -> list[tuple[CapabilityInstallation, bool, bool]]:
    """Visible organization installations paired with manage rights.

    Members see usable capabilities. A manager can also see a target
    scope without membership, keeping manage authority independent
    from runtime availability.
    """
    query = select(CapabilityInstallation, Scope).join(
        Scope, Scope.id == CapabilityInstallation.scope_id,
    ).order_by(CapabilityInstallation.created_at)
    if capability_type is not None:
        query = query.where(
            CapabilityInstallation.capability_type == capability_type,
        )
    rows = (await db.execute(query)).unique().all()
    pairs = []
    accessible_ids = await access.accessible_capability_ids(user.id)
    for installation, scope in rows:
        if scope.kind == ScopeKind.PERSONAL:
            continue
        permission = (
            SKILL_MANAGE
            if installation.capability_type == CapabilityType.SKILL
            else MCP_MANAGE
        )
        manageable = await access.has_permission(
            user.id, scope.id, permission,
        )
        if installation.id not in accessible_ids and not manageable:
            continue
        pairs.append((
            installation,
            manageable,
            installation.id in accessible_ids,
        ))
    return pairs
