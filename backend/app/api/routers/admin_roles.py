# -*- coding: utf-8 -*-
"""Admin custom role and permission endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Response, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.deps import require_role_reader, require_role_manager
from ...auth.permissions import ROOT_ONLY_PERMISSIONS, ROLE_TEMPLATES
from ...db import get_db
from workmate_product.persistence.models import Permission, Role, RoleBinding, Scope, User
from ...services.role_management import (
    RoleManagementError,
    RoleManagementService,
    permission_has_policy_consumer,
)
from ..schemas import (
    AdminPermissionCreateRequest,
    AdminPermissionOut,
    AdminPermissionUpdateRequest,
    AdminRoleCreateRequest,
    AdminRoleOut,
    AdminRolePermissionsRequest,
    AdminRoleUpdateRequest,
)

router = APIRouter(
    prefix="/admin", tags=["admin-roles"],
)


def _raise_http(error: RoleManagementError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message, **error.data},
    )


async def _permission(permission_id: str, db: AsyncSession) -> Permission:
    permission = await db.get(Permission, permission_id)
    if permission is None:
        raise HTTPException(status_code=404, detail={
            "code": "permission_not_found", "message": "Permission not found.",
        })
    return permission


async def _role(role_id: str, db: AsyncSession) -> Role:
    role = await db.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail={
            "code": "role_not_found", "message": "Role not found.",
        })
    return role


def _permission_out(
    permission: Permission, role_count: int = 0,
) -> AdminPermissionOut:
    result = AdminPermissionOut.model_validate(permission)
    result.has_policy_consumer = permission_has_policy_consumer(permission)
    result.enforcement = "visibility" if permission.key in {"agent.view", "agent.use"} else ("rbac" if result.has_policy_consumer else "unimplemented")
    result.applicable_scope = "organization" if permission.key in ROOT_ONLY_PERMISSIONS else "all"
    result.role_count = role_count
    return result


def _role_out(role: Role, keys: list[str], count: int = 0) -> AdminRoleOut:
    result = AdminRoleOut.model_validate(role)
    result.permission_keys = keys
    result.binding_count = count
    return result


@router.get("/permissions", response_model=list[AdminPermissionOut])
async def list_permissions(actor: User = Depends(require_role_reader), db: AsyncSession = Depends(get_db)):
    service = RoleManagementService(db)
    counts = await service.permission_role_counts()
    return [
        _permission_out(item, counts.get(item.key, 0))
        for item in await service.list_permissions()
    ]


@router.post(
    "/permissions", response_model=AdminPermissionOut, status_code=201,
)
async def create_permission(
    body: AdminPermissionCreateRequest,
    actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    try:
        item = await RoleManagementService(db, actor.id).create_permission(
            key=body.key, display_name=body.display_name,
            description=body.description,
        )
    except RoleManagementError as error:
        _raise_http(error)
    return _permission_out(item)


@router.patch("/permissions/{permission_id}", response_model=AdminPermissionOut)
async def update_permission(
    permission_id: str, body: AdminPermissionUpdateRequest,
    actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    item = await _permission(permission_id, db)
    try:
        item = await RoleManagementService(db, actor.id).update_permission(
            item, display_name=body.display_name, description=body.description,
        )
    except RoleManagementError as error:
        _raise_http(error)
    return _permission_out(item)


@router.delete("/permissions/{permission_id}", status_code=204)
async def delete_permission(
    permission_id: str,
    actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    item = await _permission(permission_id, db)
    try:
        await RoleManagementService(db, actor.id).delete_permission(item)
    except RoleManagementError as error:
        _raise_http(error)
    return Response(status_code=204)


@router.get("/roles", response_model=list[AdminRoleOut])
async def list_roles(
    include_permissions: bool = True,
    actor: User = Depends(require_role_reader),
    db: AsyncSession = Depends(get_db),
):
    rows = await RoleManagementService(db).list_roles()
    counts = dict((await db.execute(select(RoleBinding.role_id, func.count(func.distinct(RoleBinding.user_id)))
        .group_by(RoleBinding.role_id))).all())
    return [
        _role_out(role, keys if include_permissions else [], count)
        .model_copy(update={"user_count": counts.get(role.id, 0)})
        for role, keys, count in rows
    ]


@router.post("/roles", response_model=AdminRoleOut, status_code=201)
async def create_role(
    body: AdminRoleCreateRequest,
    actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    try:
        role, keys = await RoleManagementService(db, actor.id).create_role(
            key=body.key, display_name=body.display_name,
            description=body.description,
            permission_keys=body.permission_keys,
            delegatable=body.delegatable,
        )
    except RoleManagementError as error:
        _raise_http(error)
    return _role_out(role, keys)


@router.patch("/roles/{role_id}", response_model=AdminRoleOut)
async def update_role(
    role_id: str, body: AdminRoleUpdateRequest,
    actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    role = await _role(role_id, db)
    try:
        role = await RoleManagementService(db, actor.id).update_role(
            role, display_name=body.display_name, description=body.description,
            permission_keys=body.permission_keys, expected_version=body.expected_version,
            delegatable=body.delegatable,
        )
    except RoleManagementError as error:
        _raise_http(error)
    rows = await RoleManagementService(db).list_roles()
    _, keys, count = next(row for row in rows if row[0].id == role.id)
    return _role_out(role, keys, count)


@router.put("/roles/{role_id}/permissions", response_model=AdminRoleOut)
async def set_role_permissions(
    role_id: str, body: AdminRolePermissionsRequest,
    actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    role = await _role(role_id, db)
    try:
        role, keys = await RoleManagementService(
            db, actor.id,
        ).set_role_permissions(
            role, body.permission_keys,
            expected_version=body.expected_version,
        )
    except RoleManagementError as error:
        _raise_http(error)
    count = int(next(
        row[2] for row in await RoleManagementService(db).list_roles()
        if row[0].id == role.id
    ))
    return _role_out(role, keys, count)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: str, actor: User = Depends(require_role_manager),
    db: AsyncSession = Depends(get_db),
):
    role = await _role(role_id, db)
    try:
        await RoleManagementService(db, actor.id).delete_role(role)
    except RoleManagementError as error:
        _raise_http(error)
    return Response(status_code=204)


@router.get("/roles/templates")
@router.get("/role-templates", include_in_schema=False)
async def role_templates(actor: User = Depends(require_role_reader)):
    return list(ROLE_TEMPLATES)


@router.get("/roles/{role_id}/bindings")
async def role_bindings(role_id: str, cursor: int = Query(default=0, ge=0),
                        limit: int = Query(default=50, ge=1, le=100),
                        actor: User = Depends(require_role_reader), db: AsyncSession = Depends(get_db)):
    await _role(role_id, db)
    rows = (await db.execute(select(RoleBinding, User, Scope)
        .join(User, User.id == RoleBinding.user_id).join(Scope, Scope.id == RoleBinding.scope_id)
        .where(RoleBinding.role_id == role_id).order_by(User.id, Scope.id)
        .offset(cursor).limit(limit + 1))).all()
    return {"items": [{"user_id": user.id, "display_name": user.display_name,
        "email": user.email, "scope_id": scope.id, "scope_name": scope.name,
        "include_descendants": binding.include_descendants} for binding, user, scope in rows[:limit]],
        "next_cursor": str(cursor + limit) if len(rows) > limit else None}
