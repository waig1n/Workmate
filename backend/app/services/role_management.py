# -*- coding: utf-8 -*-
"""Transactional custom role and permission management."""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.permissions import ALL_PERMISSIONS, ROOT_ADMIN_PERMISSIONS, ROOT_ONLY_PERMISSIONS
from workmate_product.persistence.models import (
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    Scope,
    ScopeKind,
)
from .access_decision import AccessDecisionService
from .security_audit import SecurityAuditService
from .administration_guard import actor_authorized


class RoleManagementError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409, **data):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


class RoleManagementService:
    def __init__(self, db: AsyncSession, actor_id: str | None = None) -> None:
        self._db = db
        self._actor_id = actor_id

    def _audit(self, event_type: str, **metadata) -> None:
        SecurityAuditService(self._db).record(
            event_type, actor_user_id=self._actor_id, metadata=metadata,
        )

    async def permission_role_counts(self) -> dict[str, int]:
        """Roles granting each permission key (plan §5.3 reference
        counts) in one grouped query."""
        rows = (await self._db.execute(
            select(Permission.key, func.count(RolePermission.role_id))
            .outerjoin(
                RolePermission,
                RolePermission.permission_id == Permission.id,
            )
            .group_by(Permission.key),
        )).all()
        return {key: int(count or 0) for key, count in rows}

    async def list_permissions(self) -> list[Permission]:
        return list((await self._db.scalars(
            select(Permission).order_by(Permission.key),
        )).all())

    async def create_permission(
        self, *, key: str, display_name: str, description: str,
    ) -> Permission:
        await self._lock_root()
        if await self._db.scalar(select(Permission.id).where(Permission.key == key)):
            raise RoleManagementError(
                "permission_key_taken", "Permission key already exists.",
            )
        permission = Permission(
            id=str(uuid.uuid4()), key=key, display_name=display_name,
            description=description, is_system=False,
        )
        self._db.add(permission)
        self._audit("permission.create", permission_key=key)
        await self._db.commit()
        await self._db.refresh(permission)
        return permission

    async def update_permission(
        self, permission: Permission, *, display_name: str | None,
        description: str | None,
    ) -> Permission:
        await self._lock_root()
        permission = await self._locked_permission(permission.id)
        if display_name is not None:
            permission.display_name = display_name
        if description is not None:
            permission.description = description
        self._audit("permission.update", permission_key=permission.key)
        await self._db.commit()
        await self._db.refresh(permission)
        return permission

    async def delete_permission(self, permission: Permission) -> None:
        await self._lock_root()
        permission = await self._locked_permission(permission.id)
        if permission.is_system:
            raise RoleManagementError(
                "system_permission", "System permissions cannot be deleted.",
            )
        roles = list((await self._db.scalars(
            select(Role.key)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .where(RolePermission.permission_id == permission.id)
            .order_by(Role.key),
        )).all())
        if roles:
            raise RoleManagementError(
                "permission_in_use", "Permission is granted to roles.",
                roles_using=roles,
            )
        await self._db.delete(permission)
        self._audit("permission.delete", permission_key=permission.key)
        await self._db.commit()

    async def list_roles(self) -> list[tuple[Role, list[str], int]]:
        roles = list((await self._db.scalars(
            select(Role).where(Role.key != "member").order_by(Role.key),
        )).all())
        result = []
        for role in roles:
            keys = list((await self._db.scalars(
                select(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
                .order_by(Permission.key),
            )).all())
            count = await self._db.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.role_id == role.id,
                ),
            )
            result.append((role, keys, int(count or 0)))
        return result

    async def create_role(
        self, *, key: str, display_name: str, description: str,
        permission_keys: list[str],
        delegatable: bool = False,
    ) -> tuple[Role, list[str]]:
        await self._lock_root()
        if key == "member":
            raise RoleManagementError("reserved_role_key", "普通成员是账号身份，无需创建角色。", 422)
        if await self._db.scalar(select(Role.id).where(Role.key == key)):
            raise RoleManagementError("role_key_taken", "Role key already exists.")
        permissions = await self._permissions_by_key(permission_keys)
        self._validate_delegatable(delegatable, set(permissions))
        role = Role(
            id=str(uuid.uuid4()), key=key, display_name=display_name,
            description=description, is_system=False,
            delegatable=delegatable,
        )
        self._db.add(role)
        await self._db.flush()
        for permission in permissions.values():
            self._db.add(RolePermission(
                id=str(uuid.uuid4()), role_id=role.id,
                permission_id=permission.id,
            ))
        self._audit("role.create", role_key=key,
                    permission_keys=sorted(permissions))
        await self._db.commit()
        await self._db.refresh(role)
        return role, sorted(permissions)

    async def update_role(
        self, role: Role, *, display_name: str | None,
        description: str | None,
        permission_keys: list[str] | None = None,
        expected_version: int | None = None,
        delegatable: bool | None = None,
    ) -> Role:
        await self._lock_root()
        role = await self._locked_role(role.id, expected_version)
        if role.is_system:
            raise RoleManagementError("system_role", "System roles are read-only.")
        before = {"display_name": role.display_name, "description": role.description,
                  "delegatable": role.delegatable}
        if permission_keys is not None:
            await self._replace_permissions(role, permission_keys)
        if delegatable is not None:
            keys = set((await self._db.scalars(select(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id))).all())
            self._validate_delegatable(delegatable, keys)
            role.delegatable = delegatable
        if display_name is not None:
            role.display_name = display_name
        if description is not None:
            role.description = description
        self._audit("role.update", role_key=role.key, before=before,
                    after={"display_name": role.display_name, "description": role.description,
                           "delegatable": role.delegatable})
        role.version += 1
        await self._db.commit()
        await self._db.refresh(role)
        return role

    async def set_role_permissions(
        self, role: Role, permission_keys: list[str],
        expected_version: int | None = None,
    ) -> tuple[Role, list[str]]:
        await self._lock_root()
        role = await self._locked_role(role.id, expected_version)
        keys = await self._replace_permissions(role, permission_keys)
        role.version += 1
        await self._db.commit()
        await self._db.refresh(role)
        return role, keys

    async def _replace_permissions(self, role: Role, permission_keys: list[str]) -> list[str]:
        role_key = role.key
        if role.is_system:
            raise RoleManagementError(
                "system_role", "System role grants are managed by code.",
            )
        permissions = await self._permissions_by_key(permission_keys)
        self._validate_delegatable(role.delegatable, set(permissions))
        snapshots = (await self._db.scalars(select(RoleBinding.delegated_permission_keys)
            .where(RoleBinding.role_id == role.id, RoleBinding.delegated_permission_keys.is_not(None)))).all()
        if any(not set(permissions) <= set(snapshot) for snapshot in snapshots):
            raise RoleManagementError("delegation_ceiling_exceeded", "Existing delegated grants do not allow these additional permissions.")
        if set(permissions) & ROOT_ONLY_PERMISSIONS:
            department_binding = await self._db.scalar(select(RoleBinding.user_id)
                .join(Scope, Scope.id == RoleBinding.scope_id)
                .where(RoleBinding.role_id == role.id, Scope.kind != ScopeKind.ORGANIZATION).limit(1))
            if department_binding:
                raise RoleManagementError("root_permission_scope", "Root-only permissions cannot be added to a role assigned outside the organization.")
        current_keys = set((await self._db.scalars(
            select(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role.id),
        )).all())
        removing_scope_manage = bool((current_keys - set(permissions)) & ROOT_ADMIN_PERMISSIONS)
        if removing_scope_manage:
            await self._lock_root()
        await self._db.execute(delete(RolePermission).where(
            RolePermission.role_id == role.id,
        ))
        for permission in permissions.values():
            self._db.add(RolePermission(
                id=str(uuid.uuid4()), role_id=role.id,
                permission_id=permission.id,
            ))
        await self._db.flush()
        if removing_scope_manage and not await AccessDecisionService(
            self._db,
        ).active_root_admin_ids():
            await self._db.rollback()
            self._audit("role.permissions_update_denied", role_key=role_key,
                        failure="last_root_admin")
            await self._db.commit()
            raise RoleManagementError(
                "last_root_admin",
                "Operation would remove the last active root administrator.",
            )
        self._audit("role.permissions_update", role_key=role.key,
                    permission_keys=sorted(permissions), before_permission_keys=sorted(current_keys))
        return sorted(permissions)

    async def _locked_role(self, role_id, expected_version):
        if not await actor_authorized(self._db, self._actor_id, {"role.manage"}):
            raise RoleManagementError("permission_required", "Role management permission was revoked.", 403)
        role = await self._db.scalar(select(Role).where(Role.id == role_id)
            .with_for_update().execution_options(populate_existing=True))
        if role is None:
            raise RoleManagementError("role_not_found", "Role no longer exists.", 404)
        if expected_version is not None and role.version != expected_version:
            raise RoleManagementError("version_conflict", "This role changed. Reload before saving.")
        return role

    @staticmethod
    def _validate_delegatable(delegatable, keys):
        if delegatable and keys & ROOT_ONLY_PERMISSIONS:
            raise RoleManagementError("root_permission_scope", "Root-only roles cannot be delegated.", 422)

    async def delete_role(self, role: Role) -> None:
        role_key = role.key
        if role.is_system:
            raise RoleManagementError(
                "system_role", "System roles cannot be deleted.",
            )
        # Access desired-state writes use the same root lock.  Lock before
        # checking bindings so a concurrent assignment cannot race the
        # role deletion and surface as an unhandled foreign-key error.
        await self._lock_root()
        role = await self._locked_role(role.id, None)
        count = int(await self._db.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.role_id == role.id,
            ),
        ) or 0)
        if count:
            raise RoleManagementError(
                "role_in_use", "Role has active bindings.",
                binding_count=count,
            )
        await self._db.execute(delete(RolePermission).where(
            RolePermission.role_id == role.id,
        ))
        await self._db.delete(role)
        self._audit("role.delete", role_key=role.key)
        await self._db.commit()

    async def _permissions_by_key(
        self, keys: list[str],
    ) -> dict[str, Permission]:
        unique = set(keys)
        rows = list((await self._db.scalars(
            select(Permission).where(Permission.key.in_(unique)),
        )).all()) if unique else []
        result = {permission.key: permission for permission in rows}
        missing = sorted(unique - result.keys())
        if missing:
            raise RoleManagementError(
                "permission_not_found",
                f"Unknown permission keys: {', '.join(missing)}.", 422,
                permission_keys=missing,
            )
        return result

    async def _locked_permission(self, permission_id):
        permission = await self._db.scalar(select(Permission).where(Permission.id == permission_id)
            .with_for_update().execution_options(populate_existing=True))
        if permission is None:
            raise RoleManagementError("permission_not_found", "Permission no longer exists.", 404)
        return permission

    async def _lock_root(self) -> None:
        await self._db.scalar(
            select(Scope).where(
                Scope.kind == ScopeKind.ORGANIZATION,
            ).with_for_update(),
        )
        if not await actor_authorized(self._db, self._actor_id, {"role.manage"}):
            raise RoleManagementError("permission_required", "Role management permission was revoked.", 403)


def permission_has_policy_consumer(permission: Permission) -> bool:
    return permission.key in ALL_PERMISSIONS and permission.key not in {"agent.view", "agent.use"}
