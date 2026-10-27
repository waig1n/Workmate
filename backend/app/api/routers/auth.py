# -*- coding: utf-8 -*-
"""Auth router."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_db
from workmate_product.persistence.models import Scope, ScopeKind, User, UserStatus, utcnow
from ...config import get_settings
from ...services.bootstrap import (
    _ensure_user_scopes,
    get_personal_scope,
    get_root_scope,
)
from ...services.access_decision import AccessDecisionService
from ...services.security_audit import SecurityAuditService
from ...auth import (
    create_access_token,
    get_access_decision,
    get_current_user,
    hash_password,
    normalize_email,
    verify_password,
)
from ..schemas import (
    ChangePasswordRequest,
    LoginRequest,
    PermissionsOut,
    RegisterRequest,
    RegistrationStatusOut,
    TokenResponse,
    UserOut,
    UserUpdateRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])


async def _registration_available(db: AsyncSession, *, first: bool | None = None) -> bool:
    mode = get_settings().registration_mode
    if mode == "open":
        return True
    if mode == "closed":
        return False
    # bootstrap_only: open only while the user directory is empty.
    return first if first is not None else await db.scalar(select(User.id).limit(1)) is None


@router.get("/registration-status", response_model=RegistrationStatusOut)
async def registration_status(db: AsyncSession = Depends(get_db)):
    first = await db.scalar(select(User.id).limit(1)) is None
    return RegistrationStatusOut(
        registration_available=await _registration_available(db, first=first),
        bootstrap_required=first,
    )


@router.post("/register", response_model=TokenResponse)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    # Startup seeds this row. Serialize before observing whether bootstrap is
    # still available, including open-mode registrations and admin provisioning.
    root = await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION)
                           .with_for_update().execution_options(populate_existing=True))
    if root is None:
        raise HTTPException(503, detail={"code": "organization_not_ready", "message": "Organization initialization is not ready."})
    first = await db.scalar(select(User.id).limit(1)) is None
    if get_settings().registration_mode == "closed":
        raise HTTPException(status_code=403, detail="Registration is disabled")
    if not first and "organization_name" in body.model_fields_set:
        raise HTTPException(409, detail={"code": "organization_already_initialized",
                                       "message": "Organization setup has already been completed."})
    if not await _registration_available(db, first=first):
        raise HTTPException(status_code=403, detail="Registration is disabled")
    if first and body.organization_name is None:
        raise HTTPException(422, detail={"code": "organization_name_required",
                                       "message": "Organization name is required for the first account."})

    normalized = normalize_email(body.email)
    existing = await db.scalar(
        select(User).where(User.normalized_email == normalized),
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        id=str(uuid.uuid4()),
        email=body.email,
        normalized_email=normalized,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    await db.flush()
    if first:
        old_name = root.name
        root.name = body.organization_name
        root.normalized_name = body.organization_name.casefold()
        root.tree_version += 1
    # Naming, account identity and the first administrator have one commit.
    await _ensure_user_scopes(db, user, first_user=first)
    if first:
        SecurityAuditService(db).record("organization.initialize", actor_user_id=user.id,
            scope_id=root.id, metadata={"old_name": old_name, "new_name": root.name})
    await db.commit()
    await db.refresh(user)
    return TokenResponse(
        access_token=create_access_token(user.id, user.token_version),
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(
        select(User).where(
            User.normalized_email == normalize_email(body.email),
        ),
    )
    # Uniform 401 for unknown email, wrong password and disabled
    # accounts alike - never leak account existence (plan §3.2).
    if (
        user is None
        or user.status != UserStatus.ACTIVE
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user.last_login_at = utcnow()
    await db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.token_version),
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


async def _lock_self(db: AsyncSession, user: User) -> User:
    """Serialize with administrative changes and reject a revoked login."""
    token_version = user.token_version
    await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION).with_for_update())
    current = await db.scalar(select(User).where(User.id == user.id)
        .with_for_update().execution_options(populate_existing=True))
    if current is None or current.status != UserStatus.ACTIVE or current.token_version != token_version:
        raise HTTPException(401, detail="Not authenticated")
    return current


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: UserUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await _lock_self(db, user)
    if user.must_change_password:
        from ...auth.deps import PASSWORD_CHANGE_REQUIRED_DETAIL
        raise HTTPException(403, detail=PASSWORD_CHANGE_REQUIRED_DETAIL)
    user.display_name = body.display_name
    user.profile_version += 1
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/me/change-password", response_model=TokenResponse)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await _lock_self(db, user)
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_current_password",
                "message": "Current password is incorrect.",
            },
        )
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    # Revokes every previously issued token (plan §3.2).
    user.token_version += 1
    user.profile_version += 1
    await db.commit()
    await db.refresh(user)
    return TokenResponse(
        access_token=create_access_token(user.id, user.token_version),
        user=UserOut.model_validate(user),
    )


@router.get("/me/permissions", response_model=PermissionsOut)
async def me_permissions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    """Effective permission keys plus the user's scopes (RBAC §11.2)."""
    personal = await get_personal_scope(db, user.id)
    root = await get_root_scope(db)
    from ...auth.permissions import ROOT_ADMIN_PERMISSIONS

    root_permissions = await access.permissions_for_scope(user.id, root.id) if root else set()
    root_admin = ROOT_ADMIN_PERMISSIONS <= root_permissions
    scope_permissions = await access.permissions_for_scopes(user.id, await access.accessible_scope_ids(user.id))
    management_keys = {"scope.manage", "membership.read", "membership.manage", "role.bind", "audit.read"}
    return PermissionsOut(
        permissions=sorted(await access.effective_permissions(user.id)),
        personal_scope_id=personal.id if personal else None,
        root_scope_id=root.id if root else None,
        is_root_admin=root_admin,
        root_permissions=sorted(root_permissions),
        managed_scope_ids=sorted(scope_id for scope_id, keys in scope_permissions.items() if keys & management_keys),
    )
