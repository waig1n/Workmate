# -*- coding: utf-8 -*-
"""User management service (user management plan §5/§3.6/§3.7).

Owns the transaction boundaries, the last-active-root-admin invariant
and the disable-time task convergence; routers stay thin and map
``UserManagementError`` to stable HTTP codes.
"""

import base64
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.security import hash_password, normalize_email
from ..auth.permissions import ROOT_ONLY_PERMISSIONS
from workmate_product.persistence.models import (
    Role,
    RolePermission,
    Permission,
    RoleBinding,
    Scope,
    ScopeKind,
    ScopeMembership,
    ScopeClosure,
    SecurityAuditEvent,
    User,
    UserStatus,
    utcnow,
)
from .access_decision import AccessDecisionService
from .bootstrap import _ensure_user_scopes, get_root_scope
from .security_audit import SecurityAuditService
from .administration_guard import actor_authorized

logger = logging.getLogger(__name__)

LAST_ROOT_ADMIN = "last_root_admin"
EMAIL_ALREADY_REGISTERED = "email_already_registered"
USER_NOT_FOUND = "user_not_found"
INVALID_CURSOR = "invalid_cursor"
SCOPE_NOT_FOUND = "scope_not_found"
ROLE_NOT_FOUND = "role_not_found"
PERSONAL_SCOPE_FORBIDDEN = "personal_scope_forbidden"


class UserManagementError(Exception):
    """Domain error mapped by the router to detail={code, message}."""

    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _encode_cursor(created_at: datetime, user_id: str) -> str:
    # Preserve the existing compact UTC cursor representation. Decode restores
    # the timezone before binding to PostgreSQL's timestamptz column.
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    payload = json.dumps({"c": created_at.isoformat(), "i": user_id})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        created_at = datetime.fromisoformat(str(payload["c"]))
        if created_at.tzinfo is None:
            # Older cursors encode UTC without an offset. Never let asyncpg
            # interpret that naive value in the API host's local timezone.
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        return created_at, str(payload["i"])
    except (ValueError, KeyError, TypeError):
        raise UserManagementError(
            INVALID_CURSOR, "Invalid pagination cursor.", 422,
        ) from None


class UserManagementService:
    def __init__(
        self, db: AsyncSession, product_runtime=None,
        actor_id: str | None = None,
    ) -> None:
        self._db = db
        self._runtime = product_runtime
        self._actor_id = actor_id

    def _audit(
        self, event_type: str, *, target_user_id: str | None = None,
        scope_id: str | None = None, **metadata,
    ) -> SecurityAuditEvent:
        return SecurityAuditService(self._db).record(
            event_type, actor_user_id=self._actor_id,
            target_user_id=target_user_id, scope_id=scope_id,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Last root admin invariant (plan §3.6)
    # ------------------------------------------------------------------

    async def ensure_root_admin_survives(
        self, *, exclude_user_id: str,
    ) -> None:
        """Reject the write when ``exclude_user_id`` is the only active
        root administrator left.

        The root scope row is locked (FOR UPDATE on PostgreSQL; SQLite
        ignores it and only the branch semantics are tested there) so
        concurrent disable/access mutations serialize on a common row.
        """
        db = self._db
        root = await db.scalar(
            select(Scope)
            .where(Scope.kind == ScopeKind.ORGANIZATION)
            .with_for_update(),
        )
        if root is None:
            return
        others = await AccessDecisionService(db).active_root_admin_ids(
            exclude_user_id=exclude_user_id,
        )
        if not others:
            self._audit(
                "user.operation_denied", target_user_id=exclude_user_id,
                scope_id=root.id, failure="last_root_admin",
            )
            await db.commit()
            raise UserManagementError(
                LAST_ROOT_ADMIN,
                "Operation would remove the last active root "
                "administrator.",
                409,
            )

    # ------------------------------------------------------------------
    # Listing (plan §5.2)
    # ------------------------------------------------------------------

    async def list_users(
        self,
        *,
        q: str | None = None,
        status: UserStatus | None = None,
        scope_id: str | None = None,
        role_key: str | None = None,
        include_descendants: bool = False,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[User], str | None]:
        db = self._db
        query = select(User)
        if status is not None:
            query = query.where(User.status == status)
        if q:
            # Escape LIKE wildcards so user input matches literally.
            escaped = (
                q.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            query = query.where(or_(
                User.normalized_email.like(
                    f"%{normalize_email(escaped)}%", escape="\\",
                ),
                User.display_name.ilike(f"%{escaped}%", escape="\\"),
            ))
        if scope_id is not None:
            selected_scopes = select(ScopeClosure.descendant_id).where(
                ScopeClosure.ancestor_id == scope_id,
            ) if include_descendants else [scope_id]
            query = query.where(User.id.in_(
                select(ScopeMembership.user_id).where(
                    ScopeMembership.scope_id.in_(selected_scopes),
                ),
            ))
        if role_key is not None:
            query = query.where(User.id.in_(
                select(RoleBinding.user_id).join(Role, Role.id == RoleBinding.role_id)
                .where(Role.key == role_key),
            ))
        if cursor is not None:
            created_at, user_id = _decode_cursor(cursor)
            query = query.where(or_(
                User.created_at < created_at,
                and_(User.created_at == created_at, User.id < user_id),
            ))
        query = query.order_by(
            User.created_at.desc(), User.id.desc(),
        ).limit(limit + 1)
        rows = list((await db.scalars(query)).all())
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id)
        return rows, next_cursor

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_user(self, user_id: str) -> User | None:
        return await self._db.get(User, user_id)

    async def user_access(
        self, user: User,
    ) -> tuple[list[str], list[dict], str | None]:
        """Memberships (scope ids), role bindings and the root scope id."""
        db = self._db
        memberships = list((await db.scalars(
            select(ScopeMembership.scope_id).where(
                ScopeMembership.user_id == user.id,
            ).order_by(ScopeMembership.scope_id),
        )).all())
        binding_rows = (await db.execute(
            select(
                RoleBinding.scope_id,
                Role.key,
                RoleBinding.include_descendants,
            )
            .join(Role, Role.id == RoleBinding.role_id)
            .where(RoleBinding.user_id == user.id)
            .order_by(Role.key, RoleBinding.scope_id),
        )).all()
        bindings = [
            {
                "scope_id": row.scope_id,
                "role_key": row.key,
                "include_descendants": row.include_descendants,
            }
            for row in binding_rows
        ]
        root = await get_root_scope(db)
        return memberships, bindings, root.id if root else None

    # ------------------------------------------------------------------
    # Create / update (plan §5.2)
    # ------------------------------------------------------------------

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        password: str,
        must_change_password: bool,
        membership_scope_ids: list[str] | None = None,
        role_bindings: list[dict] | None = None,
    ) -> User:
        db = self._db
        normalized = normalize_email(email)
        await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION).with_for_update())
        required = {"user.manage"}
        if membership_scope_ids:
            required.add("membership.manage")
        if role_bindings:
            required.add("role.bind")
        await self._require_actor(required)
        await self._ensure_email_available(normalized, email)
        first = await db.scalar(select(User.id).limit(1)) is None
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            normalized_email=normalized,
            password_hash=hash_password(password),
            display_name=display_name,
            must_change_password=must_change_password,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent create won the unique-index race that
            # _ensure_email_available cannot close (check-then-insert).
            await db.rollback()
            raise UserManagementError(
                EMAIL_ALREADY_REGISTERED, "Email already registered.", 409,
            ) from None
        # Personal scope + enterprise identity + optional first-admin grant + the
        # audit event land in one transaction.
        await _ensure_user_scopes(db, user, first_user=first)
        self._audit("user.create", target_user_id=user.id,
                    status=str(user.status))
        if membership_scope_ids or role_bindings:
            memberships, existing_bindings, _ = await self.user_access(user)
            # Personal membership is lifecycle owned and must survive.
            non_personal = list((await db.scalars(select(Scope.id).where(
                Scope.id.in_(memberships), Scope.kind != ScopeKind.PERSONAL,
            ))).all())
            requested_pairs = {(binding["scope_id"], binding["role_key"]) for binding in (role_bindings or [])}
            initial_bindings = [binding for binding in existing_bindings
                if (binding["scope_id"], binding["role_key"]) not in requested_pairs]
            try:
                return await self.update_access(
                    user, membership_scope_ids=list(set(non_personal + (membership_scope_ids or []))),
                    role_bindings=initial_bindings + (role_bindings or []),
                    onboarding=True,
                )
            except UserManagementError:
                await db.rollback()
                raise
        await db.commit()
        await db.refresh(user)
        return user

    async def update_user(
        self,
        user: User,
        *,
        email: str | None = None,
        display_name: str | None = None,
        expected_version: int | None = None,
    ) -> User:
        await self._db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION).with_for_update())
        await self._require_actor({"user.manage"})
        user = await self.lock_user_version(user.id, expected_version, "profile_version")
        before = {"email": user.email, "display_name": user.display_name}
        if email is not None:
            normalized = normalize_email(email)
            if normalized != user.normalized_email:
                await self._ensure_email_available(normalized, email)
            user.email = email
            user.normalized_email = normalized
        if display_name is not None:
            user.display_name = display_name
        self._audit("user.update", target_user_id=user.id, before=before,
                    after={"email": user.email, "display_name": user.display_name})
        user.profile_version += 1
        await self._db.commit()
        await self._db.refresh(user)
        return user

    async def _ensure_email_available(
        self, normalized: str, raw: str | None = None,
    ) -> None:
        conditions = [User.normalized_email == normalized]
        if raw is not None and raw != normalized:
            # Legacy rows kept their raw unique constraint on email.
            conditions.append(User.email == raw)
        existing = await self._db.scalar(
            select(User.id).where(or_(*conditions)).limit(1),
        )
        if existing is not None:
            raise UserManagementError(
                EMAIL_ALREADY_REGISTERED, "Email already registered.", 409,
            )

    # ------------------------------------------------------------------
    # Account lifecycle (plan §3.2/§3.7)
    # ------------------------------------------------------------------

    async def disable_user(self, *, actor: User, user: User) -> User:
        actor_id = actor.id
        if self._actor_id is not None and self._actor_id != actor_id:
            raise UserManagementError('permission_required', 'Actor identity changed.', 403)
        self._actor_id = actor_id
        user = await self._lock_account(user.id)
        if user.status != UserStatus.DISABLED:
            await self.ensure_root_admin_survives(exclude_user_id=user.id)
            user.status = UserStatus.DISABLED
            user.disabled_at = utcnow()
            user.disabled_by = actor_id
            user.token_version += 1
            user.profile_version += 1
            receipt = self._audit("user.disable", target_user_id=user.id,
                status="disabled", disabled_at=user.disabled_at.isoformat())
        else:
            # Idempotent retries finish the original authorized disable intent;
            # they do not impersonate the disabled owner or the retrying admin.
            receipt = await self._db.scalar(select(SecurityAuditEvent).where(
                SecurityAuditEvent.event_type == 'user.disable',
                SecurityAuditEvent.actor_user_id == user.disabled_by,
                SecurityAuditEvent.target_user_id == user.id,
            ).order_by(SecurityAuditEvent.created_at.desc()).limit(1))
        target_id = user.id
        receipt_id = receipt.id if receipt is not None else None
        disable_actor = receipt.actor_user_id if receipt is not None else actor_id
        # Revocation commits first. Stop admission must never resurrect access
        # on failure, and created-but-admitted Tasks still require Worker cleanup.
        await self._db.commit()
        await self._stop_disabled_tasks(target_id, disable_actor, receipt_id)
        await self._db.refresh(user)
        return user

    async def _stop_disabled_tasks(self, target_id, actor_id, receipt_id) -> None:
        if self._runtime is None:
            return
        try:
            if receipt_id is None:
                raise ValueError('Account-disable authorization receipt is missing')
            failures = await self._runtime.stop_disabled_user_tasks(self._db,
                actor_user_id=actor_id, target_user_id=target_id, disable_event_id=receipt_id)
        except Exception as exc:  # noqa: BLE001 - account revocation stays committed
            await self._db.rollback()
            failures = [(None, exc)]
        for task_id, error in failures:
            logger.warning('disable Stop admission failed for task %s: %s', task_id, error)
            self._audit('user.disable_interrupt_failed', target_user_id=target_id,
                        failure=type(error).__name__, task_id=task_id)
        if failures:
            await self._db.commit()

    async def enable_user(self, user: User) -> User:
        user = await self._lock_account(user.id)
        if user.status == UserStatus.DISABLED:
            user.status = UserStatus.ACTIVE
            user.disabled_at = None
            user.disabled_by = None
            user.profile_version += 1
            self._audit("user.enable", target_user_id=user.id,
                        status="active")
            await self._db.commit()
            await self._db.refresh(user)
        return user

    async def reset_password(
        self,
        user: User,
        *,
        new_password: str,
        must_change_password: bool = True,
    ) -> User:
        user = await self._lock_account(user.id)
        user.password_hash = hash_password(new_password)
        user.must_change_password = must_change_password
        user.token_version += 1
        user.profile_version += 1
        # Avoid sanitizer-sensitive vocabulary while retaining the
        # security-relevant state transition in the audit trail.
        self._audit("user.reset_password", target_user_id=user.id,
                    force_change_required=must_change_password)
        await self._db.commit()
        await self._db.refresh(user)
        return user

    # ------------------------------------------------------------------
    # Access desired-state convergence (plan §5.2)
    # ------------------------------------------------------------------

    async def update_access(
        self,
        user: User,
        *,
        membership_scope_ids: list[str],
        role_bindings: list[dict] | None = None,
        retain_departure_roles: bool = False,
        expected_version: int | None = None,
        onboarding: bool = False,
    ) -> User:
        """Converge memberships and role bindings to the desired state
        in one transaction: validate everything first, then mutate -
        an invalid input must never produce partial updates."""
        db = self._db
        # Root first is the shared lock order for access, role and lifecycle writes.
        root = await db.scalar(select(Scope).where(
            Scope.kind == ScopeKind.ORGANIZATION,
        ).with_for_update())
        user = await self.lock_user_version(user.id, expected_version)
        target_user_id = user.id
        scope_ids = set(membership_scope_ids)
        if root is not None:
            scope_ids.add(root.id)
        current_scope_ids, existing_roles, _ = await self.user_access(user)
        if not onboarding:
            current_departments = set((await db.scalars(select(Scope.id).where(
                Scope.id.in_(current_scope_ids), Scope.kind == ScopeKind.DEPARTMENT,
            ))).all())
            requested_departments = scope_ids - ({root.id} if root else set())
            required = set()
            if current_departments != requested_departments:
                required.add("membership.manage")
            if role_bindings is not None or retain_departure_roles:
                required.add("role.bind")
            # A no-op still requires management authority.
            if not required and not await actor_authorized(db, self._actor_id, {"membership.manage"}):
                required.add("role.bind")
            await self._require_actor(required)
        departed = set((await db.scalars(select(Scope.id).where(
            Scope.id.in_(set(current_scope_ids) - scope_ids),
            Scope.kind == ScopeKind.DEPARTMENT,
        ))).all())
        departure_roles = [binding for binding in existing_roles if binding["scope_id"] in departed]
        if departure_roles and not onboarding and not await actor_authorized(db, self._actor_id, {"role.bind"}):
            raise UserManagementError("department_roles_require_authorization",
                "调出部门涉及管理职责，请由有角色授权权限的管理员处理。", 403)
        if role_bindings is None:
            role_bindings = existing_roles
        if not retain_departure_roles:
            role_bindings = [binding for binding in role_bindings if binding["scope_id"] not in departed]
        binding_scopes = {binding["scope_id"] for binding in role_bindings}
        all_scope_ids = scope_ids | binding_scopes

        scopes = {}
        if all_scope_ids:
            rows = (await db.scalars(
                select(Scope).where(Scope.id.in_(all_scope_ids)),
            )).all()
            scopes = {scope.id: scope for scope in rows}
        for scope_id in all_scope_ids:
            scope = scopes.get(scope_id)
            if scope is None:
                raise UserManagementError(
                    SCOPE_NOT_FOUND,
                    f"Scope {scope_id} does not exist.", 422,
                )
            if scope.kind == ScopeKind.PERSONAL:
                raise UserManagementError(
                    PERSONAL_SCOPE_FORBIDDEN,
                    "Personal scopes are managed by the user lifecycle, "
                    "not the access API.", 422,
                )

        role_keys = {binding["role_key"] for binding in role_bindings}
        roles = {}
        if role_keys:
            rows = (await db.scalars(
                select(Role).where(Role.key.in_(role_keys)),
            )).all()
            roles = {role.key: role for role in rows}
        for role_key in role_keys:
            if role_key == "member" or role_key not in roles:
                raise UserManagementError(
                    ROLE_NOT_FOUND,
                    f"Role {role_key} does not exist.", 422,
                )

        # Duplicate (scope, role) bindings are ambiguous desired state.
        seen_pairs = set()
        for binding in role_bindings:
            pair = (binding["scope_id"], binding["role_key"])
            if pair in seen_pairs:
                raise UserManagementError(
                    "duplicate_role_binding",
                    "Duplicate role binding in desired state.", 422,
                )
            seen_pairs.add(pair)
            if scopes[binding["scope_id"]].kind != ScopeKind.ORGANIZATION:
                root_only = await db.scalar(select(Permission.key)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .where(RolePermission.role_id == roles[binding["role_key"]].id,
                           Permission.key.in_(ROOT_ONLY_PERMISSIONS)).limit(1))
                if root_only or binding["role_key"] == "admin":
                    raise UserManagementError("root_permission_scope", "Root-only roles cannot be assigned to a department.", 422)

        current_memberships = (await db.scalars(
            select(ScopeMembership).where(
                ScopeMembership.user_id == user.id,
            ),
        )).all()
        current_bindings = (await db.scalars(
            select(RoleBinding).where(RoleBinding.user_id == user.id),
        )).all()
        before_access = {
            "membership_scope_ids": sorted(item.scope_id for item in current_memberships),
            "role_bindings": [{"scope_id": item.scope_id, "role_id": item.role_id,
                "include_descendants": item.include_descendants} for item in current_bindings],
        }

        desired_binding_map = {
            (roles[binding["role_key"]].id, binding["scope_id"]): binding
            for binding in role_bindings
        }
        current_binding_map = {
            (binding.role_id, binding.scope_id): binding
            for binding in current_bindings
        }

        # Serialize concurrent access PUTs / disables / grant changes on
        # the root scope row before touching admin-relevant state.
        root = await db.scalar(
            select(Scope)
            .where(Scope.kind == ScopeKind.ORGANIZATION)
            .with_for_update(),
        )

        for key, binding in current_binding_map.items():
            if key not in desired_binding_map:
                await db.delete(binding)
        for key, desired in desired_binding_map.items():
            if key in current_binding_map:
                current_binding_map[key].include_descendants = (
                    desired["include_descendants"]
                )
            else:
                db.add(RoleBinding(
                    user_id=user.id,
                    role_id=key[0],
                    scope_id=key[1],
                    include_descendants=desired["include_descendants"],
                ))

        for membership in current_memberships:
            membership_scope = await db.get(Scope, membership.scope_id)
            if membership.scope_id not in scope_ids and membership_scope.kind != ScopeKind.PERSONAL:
                await db.delete(membership)
        for scope_id in scope_ids:
            if scope_id not in {
                membership.scope_id for membership in current_memberships
            }:
                db.add(ScopeMembership(scope_id=scope_id, user_id=user.id))

        if root is not None:
            root_id = root.id
            # Flush so the invariant check evaluates the post-write
            # state, then require at least one active root admin to
            # survive (plan §3.6).
            await db.flush()
            remaining = await AccessDecisionService(
                db,
            ).active_root_admin_ids()
            if not remaining:
                await db.rollback()
                self._audit(
                    "user.access_update_denied", target_user_id=target_user_id,
                    scope_id=root_id, failure="last_root_admin",
                )
                await db.commit()
                raise UserManagementError(
                    LAST_ROOT_ADMIN,
                    "Operation would remove the last active root "
                    "administrator.",
                    409,
                )

        self._audit(
            "user.access_update", target_user_id=target_user_id,
            membership_scope_ids=sorted(scope_ids),
            role_keys=sorted(role_keys),
            departed_scope_ids=sorted(departed),
            retain_departure_roles=retain_departure_roles,
            before=before_access,
            after={"membership_scope_ids": sorted(scope_ids), "role_bindings": role_bindings},
        )
        user.access_version += 1
        await db.commit()
        await db.refresh(user)
        return user

    async def lock_user_version(self, user_id, expected_version, field="access_version"):
        user = await self._db.scalar(select(User).where(User.id == user_id)
            .with_for_update().execution_options(populate_existing=True))
        if user is None:
            raise UserManagementError(USER_NOT_FOUND, "User not found.", 404)
        if expected_version is not None and getattr(user, field) != expected_version:
            raise UserManagementError("version_conflict", "This user changed. Reload before saving.")
        return user

    async def _require_actor(self, permissions):
        if not await actor_authorized(self._db, self._actor_id, permissions):
            raise UserManagementError("permission_required", "Your account or management permission changed.", 403)

    async def _lock_account(self, user_id):
        await self._db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION).with_for_update())
        await self._require_actor({"user.manage"})
        return await self.lock_user_version(user_id, None)

    async def access_summaries(self, user_ids: list[str]) -> dict[str, dict]:
        result = {user_id: {"departments": [], "roles": []} for user_id in user_ids}
        rows = (await self._db.execute(select(ScopeMembership.user_id, Scope.id, Scope.name)
            .join(Scope, Scope.id == ScopeMembership.scope_id)
            .where(ScopeMembership.user_id.in_(user_ids), Scope.kind == ScopeKind.DEPARTMENT)
            .order_by(Scope.name))).all()
        for user_id, scope_id, name in rows:
            result[user_id]["departments"].append({"id": scope_id, "name": name})
        rows = (await self._db.execute(select(RoleBinding, Role, Scope)
            .join(Role, Role.id == RoleBinding.role_id)
            .join(Scope, Scope.id == RoleBinding.scope_id)
            .where(RoleBinding.user_id.in_(user_ids)).order_by(Role.key))).all()
        for binding, role, scope in rows:
            result[binding.user_id]["roles"].append({
                "scope_id": scope.id, "scope_name": scope.name, "role_key": role.key,
                "role_name": role.display_name, "include_descendants": binding.include_descendants,
            })
        return result
