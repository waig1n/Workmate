# -*- coding: utf-8 -*-
"""Access (membership + role binding) desired-state management tests
(plan §5.2 access PUT / §3.6)."""

from sqlalchemy import select

from app.db import get_sessionmaker
from workmate_product.persistence.models import Role, RoleBinding, Scope, ScopeKind, ScopeMembership


async def _register_second(http, email="b@test.dev"):
    resp = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": "Bee",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["user"]["id"]


async def _access_version(http, headers, user_id):
    response = await http.get(f"/api/admin/users/{user_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["access_version"]


async def _root_scope_id():
    async with get_sessionmaker()() as db:
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        return root.id


async def _department(name, parent_id):
    async with get_sessionmaker()() as db:
        from app.services.bootstrap import create_scope
        scope = await create_scope(
            db, kind=ScopeKind.DEPARTMENT, name=name,
            parent=await db.get(Scope, parent_id),
        )
        await db.commit()
        return scope.id


async def _bindings(user_id):
    async with get_sessionmaker()() as db:
        rows = (await db.execute(
            select(RoleBinding.scope_id, Role.key,
                   RoleBinding.include_descendants)
            .join(Role, Role.id == RoleBinding.role_id)
            .where(RoleBinding.user_id == user_id)
        )).all()
        return sorted(
            (row.scope_id, row.key, row.include_descendants) for row in rows
        )


async def _memberships(user_id):
    async with get_sessionmaker()() as db:
        rows = (await db.scalars(
            select(ScopeMembership.scope_id).where(
                ScopeMembership.user_id == user_id,
            ),
        )).all()
        return set(rows)


async def test_access_put_replaces_state_atomically(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    user_id = await _register_second(http)
    root_id = await _root_scope_id()
    dept_id = await _department("engineering", root_id)

    # Department placement plus an explicit management responsibility;
    # enterprise identity is lifecycle owned and remains present.
    await _editor_role(http, auth_headers)
    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={
            "membership_scope_ids": [dept_id],
            "expected_version": await _access_version(http, auth_headers, user_id),
            "role_bindings": [
                {"scope_id": dept_id, "role_key": "editor",
                 "include_descendants": False},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["memberships"]) == {root_id, dept_id}
    assert body["role_bindings"] == [{
        "scope_id": dept_id, "role_key": "editor",
        "include_descendants": False,
    }]

    assert await _memberships(user_id) == {root_id, dept_id}
    assert await _bindings(user_id) == [(dept_id, "editor", False)]


async def test_access_put_rejects_personal_scope(client_and_app, auth_headers):
    http, _ = client_and_app
    user_id = await _register_second(http)
    root_id = await _root_scope_id()

    async with get_sessionmaker()() as db:
        personal = await db.scalar(
            select(Scope).where(
                Scope.kind == ScopeKind.PERSONAL,
                Scope.owner_user_id == user_id,
            ),
        )

    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={
            "membership_scope_ids": [personal.id],
            "expected_version": await _access_version(http, auth_headers, user_id),
            "role_bindings": [],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "personal_scope_forbidden"


async def test_access_put_rejects_unknown_scope_and_role(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    user_id = await _register_second(http)
    root_id = await _root_scope_id()

    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={
            "membership_scope_ids": ["missing-scope"],
            "expected_version": await _access_version(http, auth_headers, user_id),
            "role_bindings": [],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "scope_not_found"

    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={
            "membership_scope_ids": [],
            "expected_version": await _access_version(http, auth_headers, user_id),
            "role_bindings": [
                {"scope_id": root_id, "role_key": "ghost",
                 "include_descendants": True},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "role_not_found"


async def test_access_put_invalid_leaves_state_untouched(
    client_and_app, auth_headers,
):
    """A failing access PUT must not partially apply (plan §5.2)."""
    http, _ = client_and_app
    user_id = await _register_second(http)
    root_id = await _root_scope_id()

    before_memberships = await _memberships(user_id)
    before_bindings = await _bindings(user_id)

    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={
            "membership_scope_ids": ["missing-scope"],
            "expected_version": await _access_version(http, auth_headers, user_id),
            "role_bindings": [],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert await _memberships(user_id) == before_memberships
    assert await _bindings(user_id) == before_bindings


async def test_access_put_last_admin_protection(client_and_app, auth_headers):
    """Stripping the sole admin's root binding is rejected (§3.6)."""
    http, _ = client_and_app
    me = await http.get("/api/auth/me", headers=auth_headers)
    admin_id = me.json()["id"]
    root_id = await _root_scope_id()

    resp = await http.put(
        f"/api/admin/users/{admin_id}/access",
        json={
            "membership_scope_ids": [root_id],
            "expected_version": await _access_version(http, auth_headers, admin_id),
            "role_bindings": [],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "last_root_admin"

    # The admin binding is untouched.
    bindings = await _bindings(admin_id)
    assert (root_id, "admin", True) in bindings


async def test_access_put_membership_and_bindings_are_independent(
    client_and_app, auth_headers,
):
    """Explicit cross-department responsibility need not imply membership."""
    http, _ = client_and_app
    user_id = await _register_second(http)
    root_id = await _root_scope_id()
    dept_id = await _department("research", root_id)

    await _editor_role(http, auth_headers)
    # Binding without department membership.
    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={
            "membership_scope_ids": [],
            "expected_version": await _access_version(http, auth_headers, user_id),
            "role_bindings": [
                {"scope_id": dept_id, "role_key": "editor",
                 "include_descendants": True},
            ],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert await _memberships(user_id) == {root_id}
    assert await _bindings(user_id) == [(dept_id, "editor", True)]


async def test_access_put_non_admin_forbidden(client_and_app, auth_headers):
    http, _ = client_and_app
    user_id = await _register_second(http)
    login = await http.post("/api/auth/login", json={
        "email": "b@test.dev", "password": "password123",
    })
    member_headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
    }
    root_id = await _root_scope_id()
    resp = await http.put(
        f"/api/admin/users/{user_id}/access",
        json={"membership_scope_ids": [], "role_bindings": [],
              "expected_version": await _access_version(http, auth_headers, user_id)},
        headers=member_headers,
    )
    assert resp.status_code == 403


async def test_access_put_unknown_user_404(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.put(
        "/api/admin/users/none/access",
        json={"membership_scope_ids": [], "role_bindings": [], "expected_version": 1},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "user_not_found"


async def _editor_role(http, headers):
    response = await http.post('/api/admin/roles', headers=headers, json={
        'key': 'editor', 'display_name': 'Agent editor', 'permission_keys': ['agent.update'],
    })
    assert response.status_code == 201, response.text
