# -*- coding: utf-8 -*-
"""Auth FastAPI dependencies."""

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from workmate_product.persistence.models import User, UserStatus
from ..services.access_decision import AccessDecisionService
from ..services.bootstrap import get_root_scope
from .security import decode_access_token
from .permissions import ROOT_ADMIN_PERMISSIONS

# While must_change_password is set, only these paths respond normally;
# every other product API fails with the stable code below (plan §3.2).
PASSWORD_CHANGE_ALLOWED_PATHS = frozenset({
    "/api/auth/me",
    "/api/auth/me/permissions",
    "/api/auth/me/change-password",
})

PASSWORD_CHANGE_REQUIRED_DETAIL = {
    "code": "password_change_required",
    "message": "Set a new password before continuing.",
}


async def get_current_token(request: Request) -> dict:
    """Decode the bearer token; 401 for missing/invalid tokens."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    payload = decode_access_token(token) if token else None
    if payload is None or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return payload


async def _load_active_user(
    db: AsyncSession, payload: dict,
) -> User:
    """Load the token's user, rejecting disabled accounts and stale
    token versions with a uniform 401 (no existence leak, plan §3.2)."""
    user = await db.get(User, payload["sub"])
    if user is None or user.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Pre-migration JWTs carry no ver claim - treat them as version 0.
    if payload.get("ver", 0) != user.token_version:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def get_current_user_id(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> str:
    payload = await get_current_token(request)
    user = await _load_active_user(db, payload)
    return user.id


async def get_current_user(
    request: Request,
    payload: dict = Depends(get_current_token),
    db: AsyncSession = Depends(get_db),
) -> User:
    user = await _load_active_user(db, payload)
    if (
        user.must_change_password
        and request.url.path not in PASSWORD_CHANGE_ALLOWED_PATHS
    ):
        raise HTTPException(
            status_code=403, detail=PASSWORD_CHANGE_REQUIRED_DETAIL,
        )
    return user


def get_access_decision(db: AsyncSession = Depends(get_db)) -> AccessDecisionService:
    """AccessDecisionService bound to the request's db session."""
    return AccessDecisionService(db)


async def require_root_admin(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Recovery administrator: the complete root management permission set."""
    root = await get_root_scope(db)
    if root is not None and ROOT_ADMIN_PERMISSIONS <= await AccessDecisionService(db).permissions_for_scope(user.id, root.id):
        return user
    raise HTTPException(
        status_code=403,
        detail={
            "code": "root_admin_required",
            "message": "Root administrator privileges required.",
        },
    )


async def require_root_role_binder(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Require effective ``role.bind`` on the organization root."""
    root = await get_root_scope(db)
    if root is not None and await AccessDecisionService(db).has_permission(
        user.id, root.id, "role.bind",
    ):
        return user
    raise HTTPException(
        status_code=403,
        detail={
            "code": "role_bind_required",
            "message": "Root role management privileges required.",
        },
    )


def require_root_permission(permission: str):
    """Build a dependency for one root-only management duty."""
    async def dependency(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        root = await get_root_scope(db)
        if root and await AccessDecisionService(db).has_permission(user.id, root.id, permission):
            return user
        raise HTTPException(status_code=403, detail={
            "code": "permission_required", "permission": permission,
            "message": "You do not have permission for this management operation.",
        })
    return dependency


require_user_reader = require_root_permission("user.read")
require_user_manager = require_root_permission("user.manage")
require_role_reader = require_root_permission("role.read")
require_role_manager = require_root_permission("role.manage")
