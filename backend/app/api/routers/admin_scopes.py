# -*- coding: utf-8 -*-
"""Admin department tree endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Response, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...db import get_db
from workmate_product.persistence.models import Scope, ScopeKind, User
from ...services.access_decision import AccessDecisionService
from ...services.scoped_administration import ScopedAdministrationService
from ...services.user_management import UserManagementError
from ...services.scope_management import (
    ScopeManagementError,
    ScopeManagementService,
)
from ..schemas import (
    AdminScopeCreateRequest,
    AdminScopeMoveRequest,
    AdminScopeTreeNode,
    AdminScopeUpdateRequest,
)

router = APIRouter(prefix="/admin/scopes", tags=["admin-scopes"])


class MemberUpdate(BaseModel):
    expected_version: int = Field(ge=1)


class BindingUpdate(MemberUpdate):
    include_descendants: bool = False


def _access_error(error):
    raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message})


@router.get("/{scope_id}/members")
async def members(scope_id: str, include_descendants: bool = False, q: str | None = None,
                  cursor: str | None = None, limit: int = Query(default=50, ge=1, le=100),
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).members(scope_id,
            include_descendants=include_descendants, q=q, cursor=cursor, limit=limit)
    except UserManagementError as error:
        _access_error(error)


@router.put("/{scope_id}/members/{user_id}")
async def add_member(scope_id: str, user_id: str, body: MemberUpdate,
                     user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).set_member(
            scope_id, user_id, body.expected_version, present=True)
    except UserManagementError as error:
        _access_error(error)


@router.get("/{scope_id}/member-candidates")
async def member_candidates(scope_id: str, q: str | None = None, cursor: str | None = None,
                            limit: int = Query(default=50, ge=1, le=100),
                            user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).member_candidates(scope_id,
            q=q, cursor=cursor, limit=limit)
    except UserManagementError as error:
        _access_error(error)


@router.delete("/{scope_id}/members/{user_id}")
async def remove_member(scope_id: str, user_id: str, expected_version: int = Query(ge=1),
                        retain_departure_roles: bool = False,
                        user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).set_member(
            scope_id, user_id, expected_version, present=False, retain_departure_roles=retain_departure_roles)
    except UserManagementError as error:
        _access_error(error)


@router.get("/{scope_id}/assignable-roles")
async def assignable_roles(scope_id: str, include_descendants: bool = False,
                           user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from .admin_roles import _role_out
    try:
        return [_role_out(role, keys, count) for role, keys, count in
            await ScopedAdministrationService(db, user.id).assignable_roles(scope_id, include_descendants=include_descendants)]
    except UserManagementError as error:
        _access_error(error)


@router.get("/{scope_id}/role-bindings")
async def scope_bindings(scope_id: str, cursor: int = Query(default=0, ge=0),
                         limit: int = Query(default=50, ge=1, le=100),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).bindings(scope_id, cursor, limit)
    except UserManagementError as error:
        _access_error(error)


@router.put("/{scope_id}/role-bindings/{user_id}/{role_key}")
async def set_binding(scope_id: str, user_id: str, role_key: str, body: BindingUpdate,
                      user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).set_binding(scope_id,
            user_id, role_key, body.expected_version, include_descendants=body.include_descendants)
    except UserManagementError as error:
        _access_error(error)


@router.delete("/{scope_id}/role-bindings/{user_id}/{role_key}")
async def remove_binding(scope_id: str, user_id: str, role_key: str, expected_version: int = Query(ge=1),
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    try:
        return await ScopedAdministrationService(db, user.id).set_binding(scope_id,
            user_id, role_key, expected_version, present=False)
    except UserManagementError as error:
        _access_error(error)


def _raise_http(error: ScopeManagementError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message, **error.data},
    )


async def _scope(scope_id: str, db: AsyncSession) -> Scope:
    scope = await db.get(Scope, scope_id)
    if scope is None:
        raise HTTPException(status_code=404, detail={
            "code": "scope_not_found", "message": "Scope not found.",
        })
    return scope


def _node(scope: Scope, permissions: set[str], tree_version: int = 1) -> AdminScopeTreeNode:
    return AdminScopeTreeNode(
        id=scope.id, kind=scope.kind, name=scope.name,
        parent_id=scope.parent_id, permissions=sorted(permissions), tree_version=tree_version,
    )


@router.get("/tree", response_model=list[AdminScopeTreeNode])
async def tree(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    access = AccessDecisionService(db)
    accessible = await access.accessible_scope_ids(user.id)
    scopes = list((await db.scalars(
        select(Scope).where(
            Scope.id.in_(accessible),
            Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)),
        ).order_by(Scope.name, Scope.id),
    )).all())
    permissions = await access.permissions_for_scopes(
        user.id, {scope.id for scope in scopes},
    )
    version = await _tree_version(db)
    nodes = {scope.id: _node(scope, permissions[scope.id], version) for scope in scopes}
    roots = []
    for scope in scopes:
        node = nodes[scope.id]
        if scope.parent_id in nodes:
            nodes[scope.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


@router.post("", response_model=AdminScopeTreeNode, status_code=201)
async def create_scope(
    body: AdminScopeCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = ScopeManagementService(db, user.id)
    try:
        scope = await service.create_department(
            parent_id=body.parent_id, name=body.name,
        )
    except ScopeManagementError as error:
        _raise_http(error)
    permissions = await AccessDecisionService(db).permissions_for_scope(
        user.id, scope.id,
    )
    return _node(scope, permissions, await _tree_version(db))


@router.patch("/{scope_id}", response_model=AdminScopeTreeNode)
async def rename_scope(
    scope_id: str, body: AdminScopeUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scope = await _scope(scope_id, db)
    try:
        scope = await ScopeManagementService(db, user.id).rename(
            scope, body.name, body.expected_tree_version,
        )
    except ScopeManagementError as error:
        _raise_http(error)
    permissions = await AccessDecisionService(db).permissions_for_scope(
        user.id, scope.id,
    )
    return _node(scope, permissions, await _tree_version(db))


@router.post("/{scope_id}/move", response_model=AdminScopeTreeNode)
async def move_scope(
    scope_id: str, body: AdminScopeMoveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scope = await _scope(scope_id, db)
    try:
        scope = await ScopeManagementService(db, user.id).move(
            scope, body.new_parent_id, body.expected_tree_version,
        )
    except ScopeManagementError as error:
        _raise_http(error)
    permissions = await AccessDecisionService(db).permissions_for_scope(
        user.id, scope.id,
    )
    return _node(scope, permissions, await _tree_version(db))


@router.delete("/{scope_id}", status_code=204)
async def delete_scope(
    scope_id: str, user: User = Depends(get_current_user),
    expected_tree_version: int = Query(ge=1),
    db: AsyncSession = Depends(get_db),
):
    scope = await _scope(scope_id, db)
    try:
        await ScopeManagementService(db, user.id).delete(scope, expected_tree_version)
    except ScopeManagementError as error:
        _raise_http(error)
    return Response(status_code=204)


async def _tree_version(db: AsyncSession) -> int:
    return await db.scalar(select(Scope.tree_version).where(Scope.kind == ScopeKind.ORGANIZATION)) or 1


class MovePreviewRequest(BaseModel):
    new_parent_id: str


@router.post("/{scope_id}/move-preview")
async def move_preview(scope_id: str, body: MovePreviewRequest,
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    scope = await _scope(scope_id, db)
    try:
        return await ScopeManagementService(db, user.id).move_preview(scope, body.new_parent_id)
    except ScopeManagementError as error:
        _raise_http(error)
