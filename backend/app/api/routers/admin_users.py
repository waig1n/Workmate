# -*- coding: utf-8 -*-
"""Admin user management endpoints (user management plan §5.2).

Routes require the corresponding account, department, or role management
authority; services own transactions and the last-admin invariant.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.deps import get_current_user, require_user_reader, require_user_manager
from ...services.bootstrap import get_root_scope
from ...services.access_decision import AccessDecisionService
from ...services.scoped_administration import ScopedAdministrationService
from ...db import get_db
from workmate_product.persistence.models import User, UserStatus
from ...services.user_management import (
    UserManagementError,
    UserManagementService,
)
from ..schemas import (
    AdminAccessRequest,
    AdminResetPasswordRequest,
    AdminRoleBindingOut,
    AdminUserCreateRequest,
    AdminUserDetail,
    AdminUserOut,
    AdminUserPage,
    AdminUserUpdateRequest,
)

router = APIRouter(
    prefix="/admin/users",
    tags=["admin-users"],
)


async def _require_access_manager(actor: User, db: AsyncSession) -> None:
    root = await get_root_scope(db)
    permissions = await AccessDecisionService(db).permissions_for_scope(actor.id, root.id) if root else set()
    if not {"membership.manage", "role.bind"} & permissions:
        raise HTTPException(status_code=403, detail={"code": "access_manage_required", "message": "Membership or role assignment permission required."})


def _service(
    request: Request, db: AsyncSession, actor: User | None = None,
) -> UserManagementService:
    return UserManagementService(
        db, request.app.state.product_runtime,
        actor_id=actor.id if actor else None,
    )


def _raise_http(error: UserManagementError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


async def _load_user(user_id: str, db: AsyncSession) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "user_not_found", "message": "User not found."},
        )
    return user


@router.get("", response_model=AdminUserPage)
async def list_users(
    q: str | None = None,
    status: UserStatus | None = None,
    scope_id: str | None = None,
    role_key: str | None = None,
    include_descendants: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    actor: User = Depends(require_user_reader),
    db: AsyncSession = Depends(get_db),
):
    service = UserManagementService(db)
    try:
        items, next_cursor = await service.list_users(
            q=q, status=status, scope_id=scope_id,
            role_key=role_key, include_descendants=include_descendants,
            cursor=cursor, limit=limit,
        )
    except UserManagementError as error:
        _raise_http(error)
    summaries = await service.access_summaries([user.id for user in items])
    return AdminUserPage(
        items=[AdminUserOut.model_validate(user).model_copy(update=summaries[user.id]) for user in items],
        next_cursor=next_cursor,
    )


@router.post("", response_model=AdminUserOut, status_code=201)
async def create_user(
    body: AdminUserCreateRequest,
    request: Request,
    actor: User = Depends(require_user_manager),
    db: AsyncSession = Depends(get_db),
):
    service = _service(request, db, actor)
    root = await get_root_scope(db)
    permissions = await AccessDecisionService(db).permissions_for_scope(actor.id, root.id) if root else set()
    if body.membership_scope_ids and "membership.manage" not in permissions:
        raise HTTPException(403, detail={"code": "membership_manage_required"})
    if body.role_bindings and "role.bind" not in permissions:
        raise HTTPException(403, detail={"code": "role_bind_required"})
    try:
        user = await service.create_user(
            email=body.email,
            display_name=body.display_name,
            password=body.initial_password,
            must_change_password=body.must_change_password,
            membership_scope_ids=body.membership_scope_ids,
            role_bindings=[binding.model_dump() for binding in body.role_bindings],
        )
    except UserManagementError as error:
        _raise_http(error)
    return AdminUserOut.model_validate(user)


async def _user_detail(service: UserManagementService, user: User) -> AdminUserDetail:
    memberships, bindings, root_scope_id = await service.user_access(user)
    summary = (await service.access_summaries([user.id]))[user.id]
    detail = AdminUserDetail.model_validate(user)
    detail.memberships = memberships
    detail.role_bindings = [AdminRoleBindingOut.model_validate(binding) for binding in bindings]
    detail.root_scope_id = root_scope_id
    detail.departments = summary["departments"]
    detail.roles = summary["roles"]
    return detail


@router.get("/{user_id}", response_model=AdminUserDetail)
async def get_user(
    user_id: str,
    actor: User = Depends(require_user_reader),
    db: AsyncSession = Depends(get_db),
):
    user = await _load_user(user_id, db)
    service = UserManagementService(db)
    return await _user_detail(service, user)


@router.get("/{user_id}/effective-access")
async def effective_access(user_id: str, scope_id: str,
                           actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, actor.id).effective_access(user_id, scope_id)
    except UserManagementError as error:
        _raise_http(error)


@router.patch("/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: str,
    body: AdminUserUpdateRequest,
    actor: User = Depends(require_user_manager),
    db: AsyncSession = Depends(get_db),
):
    user = await _load_user(user_id, db)
    service = UserManagementService(db, actor_id=actor.id)
    try:
        user = await service.update_user(
            user, email=body.email, display_name=body.display_name,
            expected_version=body.expected_version,
        )
    except UserManagementError as error:
        _raise_http(error)
    return AdminUserOut.model_validate(user)


@router.post("/{user_id}/disable", response_model=AdminUserOut)
async def disable_user(
    user_id: str,
    request: Request,
    actor: User = Depends(require_user_manager),
    db: AsyncSession = Depends(get_db),
):
    user = await _load_user(user_id, db)
    service = _service(request, db, actor)
    try:
        user = await service.disable_user(actor=actor, user=user)
    except UserManagementError as error:
        _raise_http(error)
    return AdminUserOut.model_validate(user)


@router.post("/{user_id}/enable", response_model=AdminUserOut)
async def enable_user(
    user_id: str,
    actor: User = Depends(require_user_manager),
    db: AsyncSession = Depends(get_db),
):
    user = await _load_user(user_id, db)
    service = UserManagementService(db, actor_id=actor.id)
    try:
        user = await service.enable_user(user)
    except UserManagementError as error:
        _raise_http(error)
    return AdminUserOut.model_validate(user)


@router.post("/{user_id}/reset-password", response_model=AdminUserOut)
async def reset_password(
    user_id: str,
    body: AdminResetPasswordRequest,
    actor: User = Depends(require_user_manager),
    db: AsyncSession = Depends(get_db),
):
    user = await _load_user(user_id, db)
    service = UserManagementService(db, actor_id=actor.id)
    try:
        user = await service.reset_password(
            user,
            new_password=body.new_password,
            must_change_password=body.must_change_password,
        )
    except UserManagementError as error:
        _raise_http(error)
    return AdminUserOut.model_validate(user)


@router.put("/{user_id}/access", response_model=AdminUserDetail)
async def update_access(
    user_id: str,
    body: AdminAccessRequest,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Desired-state convergence of memberships and role bindings
    (plan §5.2): all-or-nothing within one transaction."""
    await _require_access_manager(actor, db)
    user = await _load_user(user_id, db)
    service = UserManagementService(db, actor_id=actor.id)
    try:
        user = await service.update_access(
            user,
            expected_version=body.expected_version,
            membership_scope_ids=body.membership_scope_ids,
            role_bindings=[
                {
                    "scope_id": binding.scope_id,
                    "role_key": binding.role_key,
                    "include_descendants": binding.include_descendants,
                }
                for binding in body.role_bindings
            ] if body.role_bindings is not None else None,
            retain_departure_roles=body.retain_departure_roles,
        )
    except UserManagementError as error:
        _raise_http(error)
    return await _user_detail(service, user)
