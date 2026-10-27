# -*- coding: utf-8 -*-
"""Custom Role and Permission management tests (plan §3.4/§5.3)."""

from sqlalchemy import select

from app.db import get_sessionmaker
from workmate_product.persistence.models import (
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    Scope,
    ScopeKind,
)
from app.auth.permissions import ALL_PERMISSIONS, ROOT_ADMIN_PERMISSIONS


async def _register_second(http, email="b@test.dev"):
    resp = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": "Bee",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["user"]["id"]


async def _member_headers(http, email="b@test.dev"):
    login = await http.post("/api/auth/login", json={
        "email": email, "password": "password123",
    })
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ----------------------------------------------------------------------
# Permissions catalog
# ----------------------------------------------------------------------

async def test_list_permissions_marks_consumers(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.get("/api/admin/permissions", headers=auth_headers)
    assert resp.status_code == 200
    items = {p["key"]: p for p in resp.json()}
    # All seeded permissions exist and are marked system + consumed.
    for key in ALL_PERMISSIONS:
        assert key in items
        assert items[key]["is_system"] is True
        assert items[key]["has_policy_consumer"] is (key not in {"agent.view", "agent.use"})


async def test_create_patch_delete_custom_permission(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    resp = await http.post("/api/admin/permissions", json={
        "key": "report.export",
        "display_name": "Report Export",
        "description": "Export reports",
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["is_system"] is False
    # Custom keys have no policy consumer yet.
    assert created["has_policy_consumer"] is False

    perm_id = created["id"]
    resp = await http.patch(
        f"/api/admin/permissions/{perm_id}",
        json={"display_name": "Report export", "description": "Export reports"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Report export"

    resp = await http.delete(
        f"/api/admin/permissions/{perm_id}", headers=auth_headers,
    )
    assert resp.status_code == 204


async def test_permission_key_validation(client_and_app, auth_headers):
    http, _ = client_and_app
    for key in ("Bad Key", "1starts-with-digit", "", "x" * 65,
                "UPPER_CASE"):
        resp = await http.post("/api/admin/permissions", json={
            "key": key, "display_name": "X", "description": "",
        }, headers=auth_headers)
        assert resp.status_code == 422, key


async def test_permission_duplicate_key_409(client_and_app, auth_headers):
    http, _ = client_and_app
    await http.post("/api/admin/permissions", json={
        "key": "dup.key", "display_name": "Dup", "description": "",
    }, headers=auth_headers)
    resp = await http.post("/api/admin/permissions", json={
        "key": "dup.key", "display_name": "Dup2", "description": "",
    }, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "permission_key_taken"


async def test_system_permission_protected(client_and_app, auth_headers):
    http, _ = client_and_app
    perms = (await http.get(
        "/api/admin/permissions", headers=auth_headers,
    )).json()
    system = next(p for p in perms if p["is_system"])

    resp = await http.delete(
        f"/api/admin/permissions/{system['id']}", headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "system_permission"


async def test_permission_in_use_cannot_be_deleted(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    created = (await http.post("/api/admin/permissions", json={
        "key": "used.by.role", "display_name": "Used", "description": "",
    }, headers=auth_headers)).json()

    role = (await http.post("/api/admin/roles", json={
        "key": "user-of-custom", "display_name": "Custom",
        "permission_keys": ["used.by.role"],
    }, headers=auth_headers)).json()

    resp = await http.delete(
        f"/api/admin/permissions/{created['id']}", headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "permission_in_use"
    assert resp.json()["detail"]["roles_using"] == ["user-of-custom"]

    # Removing the grant frees the permission.
    resp = await http.put(
        f"/api/admin/roles/{role['id']}/permissions",
        json={"permission_keys": [], "expected_version": role["version"]}, headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    resp = await http.delete(
        f"/api/admin/permissions/{created['id']}", headers=auth_headers,
    )
    assert resp.status_code == 204


# ----------------------------------------------------------------------
# Roles
# ----------------------------------------------------------------------

async def test_list_roles(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.get("/api/admin/roles", headers=auth_headers)
    assert resp.status_code == 200
    items = {r["key"]: r for r in resp.json()}
    assert "admin" in items
    assert "member" not in items
    assert items["admin"]["is_system"] is True
    assert "scope.manage" in items["admin"]["permission_keys"]


async def test_create_custom_role_with_grants(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.post("/api/admin/roles", json={
        "key": "auditor",
        "display_name": "Auditor",
        "description": "Read-only audit",
        "permission_keys": ["agent.view", "agent.use"],
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    role = resp.json()
    assert role["is_system"] is False
    assert sorted(role["permission_keys"]) == ["agent.use", "agent.view"]

    # Unknown permission keys are 422 without partial grants.
    resp = await http.post("/api/admin/roles", json={
        "key": "broken", "display_name": "B",
        "permission_keys": ["agent.view", "nope.nope"],
    }, headers=auth_headers)
    assert resp.status_code == 422
    async with get_sessionmaker()() as db:
        count = await db.scalar(
            select(RolePermission).where(
                RolePermission.role_id.in_(
                    select(Role.id).where(Role.key == "broken"),
                ),
            ).limit(1),
        )
        assert count is None


async def test_role_duplicate_key_409(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.post("/api/admin/roles", json={
        "key": "admin", "display_name": "Fake Admin",
        "permission_keys": [],
    }, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "role_key_taken"


async def test_system_role_protections(client_and_app, auth_headers):
    http, _ = client_and_app
    roles = {
        r["key"]: r
        for r in (await http.get(
            "/api/admin/roles", headers=auth_headers,
        )).json()
    }
    admin = roles["admin"]

    # Delete is refused.
    resp = await http.delete(
        f"/api/admin/roles/{admin['id']}", headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "system_role"

    # Removing system permissions from the admin role is refused.
    resp = await http.put(
        f"/api/admin/roles/{admin['id']}/permissions",
        json={"permission_keys": ["agent.view"], "expected_version": admin["version"]}, headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "system_role"


async def test_update_custom_role_and_grants(client_and_app, auth_headers):
    http, _ = client_and_app
    role = (await http.post("/api/admin/roles", json={
        "key": "editor", "display_name": "Editor",
        "permission_keys": ["agent.view"],
    }, headers=auth_headers)).json()

    resp = await http.patch(
        f"/api/admin/roles/{role['id']}",
        json={"display_name": "Content editor", "description": "Editors",
              "expected_version": role["version"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Content editor"

    role = next(item for item in (await http.get(
        "/api/admin/roles", headers=auth_headers,
    )).json() if item["id"] == role["id"])

    # Desired-state grant update replaces atomically.
    resp = await http.put(
        f"/api/admin/roles/{role['id']}/permissions",
        json={"permission_keys": ["agent.view", "agent.create"],
              "expected_version": role["version"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert sorted(resp.json()["permission_keys"]) == [
        "agent.create", "agent.view",
    ]

    # Unknown key: 422 and no partial change.
    role = next(item for item in (await http.get(
        "/api/admin/roles", headers=auth_headers,
    )).json() if item["id"] == role["id"])
    resp = await http.put(
        f"/api/admin/roles/{role['id']}/permissions",
        json={"permission_keys": ["ghost.key"], "expected_version": role["version"]}, headers=auth_headers,
    )
    assert resp.status_code == 422
    detail = await http.get(
        f"/api/admin/roles?include_permissions=true", headers=auth_headers,
    )
    editor = next(
        r for r in detail.json() if r["key"] == "editor"
    )
    assert sorted(editor["permission_keys"]) == [
        "agent.create", "agent.view",
    ]


async def test_delete_role_with_bindings_409(client_and_app, auth_headers):
    http, _ = client_and_app
    role = (await http.post("/api/admin/roles", json={
        "key": "bound-role", "display_name": "Bound",
        "permission_keys": [],
    }, headers=auth_headers)).json()
    user_id = await _register_second(http)

    async with get_sessionmaker()() as db:
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        db.add(RoleBinding(
            user_id=user_id, role_id=role["id"], scope_id=root.id,
        ))
        await db.commit()

    resp = await http.delete(
        f"/api/admin/roles/{role['id']}", headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "role_in_use"
    assert resp.json()["detail"]["binding_count"] == 1


async def test_delete_unbound_custom_role(client_and_app, auth_headers):
    http, _ = client_and_app
    role = (await http.post("/api/admin/roles", json={
        "key": "disposable", "display_name": "D", "permission_keys": [],
    }, headers=auth_headers)).json()
    resp = await http.delete(
        f"/api/admin/roles/{role['id']}", headers=auth_headers,
    )
    assert resp.status_code == 204


# ----------------------------------------------------------------------
# Authorization and last-admin interaction
# ----------------------------------------------------------------------

async def test_role_management_requires_role_bind(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    await _register_second(http)
    member = await _member_headers(http)
    resp = await http.get("/api/admin/roles", headers=member)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "permission_required"


async def test_custom_role_full_management_permissions_participates_in_last_admin(
    client_and_app, auth_headers,
):
    """A custom role granting the full management set satisfies the last-admin
    invariant (plan §3.6): once bound to a second user, the original
    admin can be disabled."""
    http, _ = client_and_app
    me = await http.get("/api/auth/me", headers=auth_headers)
    admin_id = me.json()["id"]

    role = (await http.post("/api/admin/roles", json={
        "key": "deputy-admin", "display_name": "Deputy",
        "permission_keys": sorted(ROOT_ADMIN_PERMISSIONS),
    }, headers=auth_headers)).json()
    user_id = await _register_second(http)

    async with get_sessionmaker()() as db:
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        db.add(RoleBinding(
            user_id=user_id, role_id=role["id"], scope_id=root.id,
        ))
        await db.commit()

    resp = await http.post(
        f"/api/admin/users/{admin_id}/disable", headers=auth_headers,
    )
    assert resp.status_code == 200


async def test_removing_scope_manage_grant_keeps_last_admin(
    client_and_app, auth_headers,
):
    """Removing scope.manage from a custom role that underpins the only
    remaining admin binding is rejected (plan §3.4/§3.6)."""
    http, _ = client_and_app
    me = await http.get("/api/auth/me", headers=auth_headers)
    admin_id = me.json()["id"]
    user_id = await _register_second(http)

    role = (await http.post("/api/admin/roles", json={
        "key": "sole-manager", "display_name": "Sole",
        "permission_keys": sorted(ROOT_ADMIN_PERMISSIONS),
    }, headers=auth_headers)).json()

    async with get_sessionmaker()() as db:
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        # The member user's only path to scope.manage is this role...
        db.add(RoleBinding(
            user_id=user_id, role_id=role["id"], scope_id=root.id,
        ))
        # Keep the caller authorized through the pre-write state.  The
        # endpoint requires root role.bind; deleting its admin binding
        # without this binding would correctly fail authorization before
        # reaching the last-admin invariant.
        db.add(RoleBinding(
            user_id=admin_id, role_id=role["id"], scope_id=root.id,
        ))
        # ...and the sole admin's admin binding is removed, leaving the
        # member as the only prospective root admin.
        admin_role = await db.scalar(
            select(Role).where(Role.key == "admin"),
        )
        binding = await db.get(
            RoleBinding, (admin_id, admin_role.id, root.id),
        )
        await db.delete(binding)
        await db.commit()

    resp = await http.put(
        f"/api/admin/roles/{role['id']}/permissions",
        json={"permission_keys": ["agent.view"], "expected_version": role["version"]}, headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "last_root_admin"


async def test_permission_list_includes_role_counts(client_and_app, auth_headers):
    http, _ = client_and_app
    await http.post("/api/admin/permissions", json={
        "key": "counted.permission", "display_name": "Counted",
        "description": "",
    }, headers=auth_headers)
    await http.post("/api/admin/roles", json={
        "key": "counting-role", "display_name": "Counting",
        "permission_keys": ["counted.permission", "agent.view"],
    }, headers=auth_headers)

    items = {
        p["key"]: p
        for p in (await http.get(
            "/api/admin/permissions", headers=auth_headers,
        )).json()
    }
    assert items["counted.permission"]["role_count"] == 1
    assert items["agent.view"]["role_count"] == 2  # admin + counting-role
    assert items["agent.use"]["role_count"] == 1
    # A permission no role grants reports zero.
    zero = (await http.post("/api/admin/permissions", json={
        "key": "ungranted.permission", "display_name": "U",
        "description": "",
    }, headers=auth_headers)).json()
    await http.delete(
        f"/api/admin/permissions/{zero['id']}", headers=auth_headers,
    )
    ungranted = (await http.post("/api/admin/permissions", json={
        "key": "ungranted2.permission", "display_name": "U2",
        "description": "",
    }, headers=auth_headers)).json()
    items = {
        p["key"]: p
        for p in (await http.get(
            "/api/admin/permissions", headers=auth_headers,
        )).json()
    }
    assert items["ungranted2.permission"]["role_count"] == 0
