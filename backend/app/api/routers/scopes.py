# -*- coding: utf-8 -*-
"""Scopes router (RBAC design §11.2).

Only the read-only endpoint is open in this phase; department and
membership management APIs wait for enterprise org integration.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_access_decision, get_current_user
from ...db import get_db
from workmate_product.persistence.models import Scope, ScopeKind, User
from ...services.access_decision import AccessDecisionService
from ..schemas import ScopeOut

router = APIRouter(prefix="/scopes", tags=["scopes"])


@router.get("/accessible", response_model=list[ScopeOut])
async def accessible_scopes(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    """Personal plus membership- or management-reachable tree scopes."""
    accessible_ids = await access.accessible_scope_ids(user.id)
    scopes = (
        await db.scalars(
            select(Scope)
            .where(Scope.id.in_(accessible_ids))
            .order_by(Scope.created_at, Scope.id),
        )
    ).all()
    scopes.sort(
        key=lambda scope: (
            scope.kind != ScopeKind.PERSONAL,
            scope.created_at,
            scope.id,
        ),
    )
    permissions = await access.permissions_for_scopes(
        user.id, {scope.id for scope in scopes},
    )
    return [
        ScopeOut(
            id=scope.id,
            kind=scope.kind,
            name=scope.name,
            parent_id=scope.parent_id,
            permissions=sorted(permissions[scope.id]),
        )
        for scope in scopes
    ]
