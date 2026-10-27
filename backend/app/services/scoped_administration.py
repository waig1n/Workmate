"""Scope-local membership and bounded role delegation.

All writes serialize on root then user, matching account access writes.
No operation here replaces another department's membership or bindings.
"""
from sqlalchemy import select, func

from ..auth.permissions import ROOT_ONLY_PERMISSIONS
from workmate_product.persistence.models import (
    Role,
    RoleBinding,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
    User,
)
from .access_decision import AccessDecisionService
from .bootstrap import get_root_scope
from .role_management import RoleManagementService
from .security_audit import SecurityAuditService
from .user_management import UserManagementError, UserManagementService
from .administration_guard import actor_authorized


class ScopedAdministrationService:
    def __init__(self, db, actor_id):
        self.db = db
        self.actor_id = actor_id
        self.access = AccessDecisionService(db)

    async def require(self, scope_id, permission):
        scope = await self.db.get(Scope, scope_id)
        if scope is None or scope.kind == ScopeKind.PERSONAL:
            raise UserManagementError("scope_not_found", "Organization or department not found.", 404)
        if not await actor_authorized(self.db, self.actor_id, {permission}, scope_id):
            raise UserManagementError("scope_permission_required", "Permission is required in this department.", 403)
        return scope

    async def _lock(self, user_id, expected_version):
        await self.db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION).with_for_update())
        return await UserManagementService(self.db).lock_user_version(user_id, expected_version)

    async def _root_permission(self, key):
        root = await get_root_scope(self.db)
        return root is not None and await self.access.has_permission(self.actor_id, root.id, key)

    async def _visible_user(self, user_id, scope_id, *, read_only=False):
        # Root readers may select globally. Delegates only see people already
        # in departments where they have member management permission.
        if await self._root_permission("user.read"):
            return
        if await self.access.has_permission(self.actor_id, scope_id, "role.bind"):
            # A lingering local grant must remain revocable after membership
            # removal; this does not expose the user's other scopes.
            if await self.db.scalar(select(RoleBinding.user_id).where(
                RoleBinding.user_id == user_id, RoleBinding.scope_id == scope_id).limit(1)):
                return
        memberships = list((await self.db.scalars(select(ScopeMembership.scope_id)
            .where(ScopeMembership.user_id == user_id))).all())
        permissions = await self.access.permissions_for_scopes(self.actor_id, memberships)
        if read_only:
            descendants = set((await self.db.scalars(select(ScopeClosure.descendant_id)
                .where(ScopeClosure.ancestor_id == scope_id))).all())
            if any(sid in descendants and "membership.read" in grants for sid, grants in permissions.items()):
                return
        if not any("membership.manage" in grants or (sid == scope_id and "role.bind" in grants)
                   for sid, grants in permissions.items()):
            raise UserManagementError("member_not_visible", "User is outside your managed departments.", 403)

    async def members(self, scope_id, *, include_descendants=False, q=None, cursor=None, limit=50):
        await self.require(scope_id, "membership.read")
        descendants = list((await self.db.scalars(select(ScopeClosure.descendant_id)
            .where(ScopeClosure.ancestor_id == scope_id))).all())
        permissions = await self.access.permissions_for_scopes(self.actor_id, descendants)
        visible = [sid for sid, grants in permissions.items() if "membership.read" in grants]
        scope = await self.db.get(Scope, scope_id)
        unassigned = scope.kind == ScopeKind.ORGANIZATION
        page = await self._member_page(visible if include_descendants else [scope_id],
            q=q, cursor=cursor, limit=limit, unassigned=unassigned and not include_descendants)
        async def count(ids, *, unassigned=False):
            query = select(func.count(func.distinct(ScopeMembership.user_id))).where(ScopeMembership.scope_id.in_(ids))
            if unassigned:
                query = query.where(ScopeMembership.user_id.not_in(self._department_member_ids()))
            return await self.db.scalar(query)
        page["counts"] = {"direct": await count([scope_id], unassigned=unassigned), "total": await count(visible),
            "limited": len(visible) < len(descendants)}
        return page

    async def member_candidates(self, scope_id, *, q=None, cursor=None, limit=50):
        grants = await self.access.permissions_for_scope(self.actor_id, scope_id)
        permission = "membership.manage" if "membership.manage" in grants else "role.bind"
        await self.require(scope_id, permission)
        if await self._root_permission("user.read"):
            users, next_cursor = await UserManagementService(self.db).list_users(q=q, cursor=cursor, limit=limit)
            return {"items": [{"id": u.id, "display_name": u.display_name, "email": u.email,
                "status": u.status, "access_version": u.access_version, "membership_scope_ids": []} for u in users],
                "next_cursor": next_cursor}
        scopes = list((await self.db.scalars(select(Scope.id).where(Scope.kind != ScopeKind.PERSONAL))).all())
        matrix = await self.access.permissions_for_scopes(self.actor_id, scopes)
        scope_ids = [sid for sid, permissions in matrix.items() if "membership.manage" in permissions]
        if "role.bind" in grants:
            scope_ids.append(scope_id)
        return await self._member_page(scope_ids, q=q, cursor=cursor, limit=limit)

    @staticmethod
    def _department_member_ids():
        # Organization membership is identity, not direct department placement.
        # Exclude all department placements, including ones hidden from the reader.
        return select(ScopeMembership.user_id).join(Scope, Scope.id == ScopeMembership.scope_id).where(Scope.kind == ScopeKind.DEPARTMENT)

    async def _member_page(self, scope_ids, *, q, cursor, limit, unassigned=False):
        # Filter before paging so unauthorized descendant identities never appear.
        from .user_management import _decode_cursor, _encode_cursor
        from sqlalchemy import and_, or_
        query = select(User).where(User.id.in_(select(ScopeMembership.user_id)
            .where(ScopeMembership.scope_id.in_(scope_ids))))
        if unassigned:
            query = query.where(User.id.not_in(self._department_member_ids()))
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(or_(User.display_name.ilike(f"%{escaped}%", escape="\\"), User.email.ilike(f"%{escaped}%", escape="\\")))
        if cursor:
            created, uid = _decode_cursor(cursor)
            query = query.where(or_(User.created_at < created, and_(User.created_at == created, User.id < uid)))
        users = list((await self.db.scalars(query.order_by(User.created_at.desc(), User.id.desc()).limit(limit + 1))).all())
        more = len(users) > limit
        users = users[:limit]
        memberships = (await self.db.execute(select(ScopeMembership.user_id, ScopeMembership.scope_id)
            .where(ScopeMembership.user_id.in_([u.id for u in users]), ScopeMembership.scope_id.in_(scope_ids)))).all()
        return {"items": [{"id": user.id, "display_name": user.display_name, "email": user.email,
            "status": user.status, "access_version": user.access_version,
            "membership_scope_ids": [sid for uid, sid in memberships if uid == user.id]} for user in users],
            "next_cursor": _encode_cursor(users[-1].created_at, users[-1].id) if more else None}

    async def set_member(self, scope_id, user_id, expected_version, *, present, retain_departure_roles=False):
        await self.require(scope_id, "membership.manage")
        await self._visible_user(user_id, scope_id)
        user = await self._lock(user_id, expected_version)
        await self.require(scope_id, "membership.manage")
        await self._visible_user(user_id, scope_id)
        scope = await self.db.get(Scope, scope_id)
        if scope.kind == ScopeKind.ORGANIZATION and not present:
            raise UserManagementError("organization_membership_managed",
                "企业成员身份由账号自动维护，不能通过移出部门操作撤销。", 422)
        membership = await self.db.get(ScopeMembership, (scope_id, user_id))
        departure_bindings = []
        if not present and membership is not None:
            departure_bindings = list((await self.db.scalars(select(RoleBinding).where(
                RoleBinding.user_id == user_id, RoleBinding.scope_id == scope_id,
            ))).all())
            if departure_bindings or retain_departure_roles:
                if not await actor_authorized(self.db, self.actor_id, {"role.bind"}, scope_id):
                    raise UserManagementError("department_roles_require_authorization",
                        "调出部门涉及管理职责，请由有角色授权权限的管理员处理。", 403)
                assignable = {role.id for role, _, _ in await self.assignable_roles(scope_id)}
                if any(binding.role_id not in assignable for binding in departure_bindings):
                    raise UserManagementError("role_not_assignable",
                        "该成员的管理职责超出你的授权范围，请联系组织管理员。", 403)
            if not retain_departure_roles:
                for binding in departure_bindings:
                    await self.db.delete(binding)
        root = await get_root_scope(self.db)
        if root is not None and root.id != scope_id and await self.db.get(ScopeMembership, (root.id, user_id)) is None:
            self.db.add(ScopeMembership(scope_id=root.id, user_id=user_id))
        if present and membership is None:
            self.db.add(ScopeMembership(scope_id=scope_id, user_id=user_id))
        elif not present and membership is not None:
            await self.db.delete(membership)
        user.access_version += 1
        SecurityAuditService(self.db).record("membership.add" if present else "membership.remove",
            actor_user_id=self.actor_id, target_user_id=user_id, scope_id=scope_id,
            metadata={"retain_departure_roles": retain_departure_roles,
                "departure_role_ids": [binding.role_id for binding in departure_bindings]})
        await self.db.commit()
        return {"access_version": user.access_version}

    async def assignable_roles(self, scope_id, *, include_descendants=False):
        await self.require(scope_id, "role.bind")
        root_binder = await self._root_permission("role.bind")
        allowed = await self.access.permissions_for_scope(self.actor_id, scope_id)
        if include_descendants and not root_binder:
            # Use only grants explicitly inheritable from this scope or an
            # ancestor. Merely having authority on current children is not a
            # durable right to delegate authority to future children.
            from workmate_product.persistence.models import Permission, RolePermission
            allowed = set((await self.db.scalars(select(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(RoleBinding, RoleBinding.role_id == RolePermission.role_id)
                .join(ScopeClosure, ScopeClosure.ancestor_id == RoleBinding.scope_id)
                .where(RoleBinding.user_id == self.actor_id, ScopeClosure.descendant_id == scope_id,
                       RoleBinding.include_descendants.is_(True)))).all())
            if "role.bind" not in allowed:
                return []
        scope = await self.db.get(Scope, scope_id)
        result = []
        for role, keys, count in await RoleManagementService(self.db).list_roles():
            if scope.kind != ScopeKind.ORGANIZATION and (set(keys) & ROOT_ONLY_PERMISSIONS or role.key == "admin"):
                continue
            if not root_binder and (not role.delegatable or role.is_system or not set(keys) <= allowed):
                continue
            result.append((role, keys, count))
        return result

    async def set_binding(self, scope_id, user_id, role_key, expected_version, *, include_descendants=False, present=True):
        await self.require(scope_id, "role.bind")
        await self._visible_user(user_id, scope_id)
        user = await self._lock(user_id, expected_version)
        await self.require(scope_id, "role.bind")
        await self._visible_user(user_id, scope_id)
        role = await self.db.scalar(select(Role).where(Role.key == role_key))
        if role is None:
            raise UserManagementError("role_not_found", "Role not found.", 404)
        roles = await self.assignable_roles(scope_id, include_descendants=include_descendants if present else False)
        candidate = next((row for row in roles if row[0].id == role.id), None)
        if candidate is None:
            raise UserManagementError("role_not_assignable", "Role exceeds your delegation boundary.", 403)
        binding = await self.db.get(RoleBinding, (user_id, role.id, scope_id))
        if present:
            root_binder = await self._root_permission("role.bind")
            if binding is None:
                binding = RoleBinding(user_id=user_id, role_id=role.id, scope_id=scope_id)
                self.db.add(binding)
            binding.include_descendants = include_descendants
            binding.granted_by = self.actor_id
            binding.delegated_permission_keys = None if root_binder else candidate[1]
        elif binding is not None:
            await self.db.delete(binding)
        user.access_version += 1
        await self.db.flush()
        if not await self.access.active_root_admin_ids():
            await self.db.rollback()
            raise UserManagementError("last_root_admin", "Operation would remove the last active root administrator.")
        SecurityAuditService(self.db).record("role_binding.set" if present else "role_binding.remove",
            actor_user_id=self.actor_id, target_user_id=user_id, scope_id=scope_id,
            metadata={"role_key": role_key, "include_descendants": include_descendants})
        await self.db.commit()
        return {"access_version": user.access_version}

    async def bindings(self, scope_id, cursor=0, limit=50):
        await self.require(scope_id, "role.bind")
        rows = (await self.db.execute(select(RoleBinding, User, Role)
            .join(User, User.id == RoleBinding.user_id).join(Role, Role.id == RoleBinding.role_id)
            .where(RoleBinding.scope_id == scope_id).order_by(User.id, Role.key)
            .offset(cursor).limit(limit + 1))).all()
        return {"items": [{"user_id": user.id, "display_name": user.display_name, "email": user.email,
            "access_version": user.access_version, "role_key": role.key, "role_name": role.display_name,
            "include_descendants": binding.include_descendants} for binding, user, role in rows[:limit]],
            "next_cursor": str(cursor + limit) if len(rows) > limit else None}

    async def effective_access(self, user_id, scope_id):
        await self.require(scope_id, "membership.read")
        await self._visible_user(user_id, scope_id, read_only=True)
        if await self.db.get(User, user_id) is None:
            raise UserManagementError("user_not_found", "User not found.", 404)
        rows = (await self.db.execute(select(RoleBinding, Role, Scope, ScopeClosure.depth)
            .join(Role, Role.id == RoleBinding.role_id).join(Scope, Scope.id == RoleBinding.scope_id)
            .join(ScopeClosure, ScopeClosure.ancestor_id == Scope.id)
            .where(RoleBinding.user_id == user_id, ScopeClosure.descendant_id == scope_id,
                   (ScopeClosure.depth == 0) | RoleBinding.include_descendants))).all()
        keys = {role.id: permissions for role, permissions, _ in await RoleManagementService(self.db).list_roles()}
        effective = await self.access.permissions_for_scope(user_id, scope_id)
        source_permissions = await self.access.permissions_for_scopes(self.actor_id, [scope.id for _, _, scope, _ in rows])
        root_reader = await self._root_permission("user.read")
        sources = []
        for binding, role, scope, depth in rows:
            visible = root_reader or bool({"membership.read", "role.bind"} & source_permissions[scope.id])
            sources.append({"role_key": role.key if visible else "inherited",
                "role_name": role.display_name if visible else "上级部门授权",
                "scope_id": scope.id if visible else None,
                "scope_name": scope.name if visible else "上级部门",
                "inherited": depth > 0, "include_descendants": binding.include_descendants,
                "permission_keys": sorted(set(keys[role.id]) & effective)})
        return {"scope_id": scope_id,
            "permissions": sorted(effective), "sources": sources,
            "resource_access_note": "管理职责与资源使用分开；使用取决于个人所有权、所属部门和显式共享。调出部门默认撤销本部门直接管理职责；跨部门保留需有授权权限的管理员明确选择。"}
