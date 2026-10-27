# -*- coding: utf-8 -*-
"""Admin user directory and account lifecycle tests (plan §5.2/§3.7)."""

import uuid

import pytest
from sqlalchemy import select

from app.db import get_sessionmaker
from workmate_product.persistence.models import (
    Role,
    RoleBinding,
    Scope,
    ScopeKind,
    SecurityAuditEvent,
    User,
)


async def _security_events_for_user(user_id):
    async with get_sessionmaker()() as db:
        return list((await db.scalars(
            select(SecurityAuditEvent)
            .where(SecurityAuditEvent.target_user_id == user_id)
            .order_by(SecurityAuditEvent.created_at),
        )).all())


async def _register_second(http, email="b@test.dev"):
    resp = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": "Bee",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _make_user_token(http, email="b@test.dev"):
    """Register a second (member) user and return (id, headers)."""
    body = await _register_second(http, email)
    return body["user"]["id"], {
        "Authorization": f"Bearer {body['access_token']}",
    }


# ----------------------------------------------------------------------
# Permission matrix
# ----------------------------------------------------------------------

async def test_non_admin_cannot_list_users(client_and_app, auth_headers):
    http, _ = client_and_app
    _, member_headers = await _make_user_token(http)
    resp = await http.get("/api/admin/users", headers=member_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "permission_required"

    # Unauthenticated access is 401, not 403.
    assert (await http.get("/api/admin/users")).status_code == 401


async def test_admin_lists_users(client_and_app, auth_headers):
    http, _ = client_and_app
    await _register_second(http)
    resp = await http.get("/api/admin/users", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert {"id", "email", "display_name", "status", "must_change_password",
            "last_login_at", "created_at"} <= set(first)


# ----------------------------------------------------------------------
# Listing: search, filters, cursor pagination
# ----------------------------------------------------------------------

async def test_list_users_search_and_filters(client_and_app, auth_headers):
    http, _ = client_and_app
    await _register_second(http, "alice@corp.dev")
    await _register_second(http, "bob@corp.dev")

    # q matches normalized email and display name.
    resp = await http.get(
        "/api/admin/users?q=ALICE", headers=auth_headers,
    )
    assert [u["email"] for u in resp.json()["items"]] == ["alice@corp.dev"]
    resp = await http.get(
        "/api/admin/users?q=alice%40corp", headers=auth_headers,
    )
    assert {u["email"] for u in resp.json()["items"]} == {"alice@corp.dev"}
    resp = await http.get("/api/admin/users?q=Tester", headers=auth_headers)
    assert {u["email"] for u in resp.json()["items"]} == {"u@test.dev"}

    # Invalid status is rejected.
    resp = await http.get(
        "/api/admin/users?status=ghost", headers=auth_headers,
    )
    assert resp.status_code == 422

    # Unknown scope filter is accepted but yields nothing.
    resp = await http.get(
        "/api/admin/users?scope_id=00000000-0000-4000-8000-000000000001",
        headers=auth_headers,
    )
    assert resp.json()["items"] == []


async def test_list_users_search_treats_like_wildcards_literally(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    for email, name in (
        ("percent@test.dev", "100% Real"),
        ("underscore@test.dev", "under_score"),
        ("ordinary@test.dev", "100X Real underZscore"),
    ):
        response = await http.post("/api/admin/users", json={
            "email": email,
            "display_name": name,
            "initial_password": "password123",
        }, headers=auth_headers)
        assert response.status_code == 201, response.text

    response = await http.get(
        "/api/admin/users", params={"q": "100%"}, headers=auth_headers,
    )
    assert [item["email"] for item in response.json()["items"]] == [
        "percent@test.dev",
    ]
    response = await http.get(
        "/api/admin/users", params={"q": "under_"}, headers=auth_headers,
    )
    assert [item["email"] for item in response.json()["items"]] == [
        "underscore@test.dev",
    ]


async def test_list_users_cursor_pagination(client_and_app, auth_headers):
    http, _ = client_and_app
    for index in range(3):
        await _register_second(http, f"u{index}@test.dev")

    resp = await http.get(
        "/api/admin/users?limit=2", headers=auth_headers,
    )
    page1 = resp.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    resp = await http.get(
        f"/api/admin/users?limit=2&cursor={page1['next_cursor']}",
        headers=auth_headers,
    )
    page2 = resp.json()
    assert len(page2["items"]) == 2

    seen = {u["id"] for u in page1["items"] + page2["items"]}
    assert len(seen) == 4  # no duplicates across pages

    # Exhausted pagination: re-asking with the last cursor returns the
    # same trailing page and no further cursor.
    last_cursor = page2["next_cursor"] or page1["next_cursor"]
    resp = await http.get(
        f"/api/admin/users?limit=100&cursor={last_cursor}",
        headers=auth_headers,
    )
    replayed = resp.json()
    assert replayed["next_cursor"] is None
    assert {u["id"] for u in replayed["items"]} <= seen

    # limit is validated: 1..100.
    resp = await http.get(
        "/api/admin/users?limit=500", headers=auth_headers,
    )
    assert resp.status_code == 422


# ----------------------------------------------------------------------
# Create / detail / edit
# ----------------------------------------------------------------------

async def test_create_user_provisions_identity_without_management_role(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    resp = await http.post("/api/admin/users", json={
        "email": "New@Corp.dev", "display_name": "New",
        "initial_password": "initial-password-1",
    }, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["email"] == "New@corp.dev"
    assert created["status"] == "active"
    # Admin-created accounts must rotate the initial password.
    assert created["must_change_password"] is True
    user_id = created["id"]

    detail = await http.get(
        f"/api/admin/users/{user_id}", headers=auth_headers,
    )
    body = detail.json()
    assert {m for m in body["memberships"]} >= {body["root_scope_id"]}
    assert body["role_bindings"] == []

    # Personal scope exists and is owned by the new user.
    async with get_sessionmaker()() as db:
        personal = await db.scalar(
            select(Scope).where(
                Scope.kind == ScopeKind.PERSONAL,
                Scope.owner_user_id == user_id,
            ),
        )
        assert personal is not None

    # The initial password logs in and forces a password change.
    login = await http.post("/api/auth/login", json={
        "email": "new@corp.dev", "password": "initial-password-1",
    })
    assert login.status_code == 200
    assert login.json()["user"]["must_change_password"] is True
    gated = await http.get(
        "/api/tasks",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert gated.status_code == 403


def _select_scope_where(*, kind, owner):
    from sqlalchemy import select
    from workmate_product.persistence.models import Scope
    return select(Scope).where(
        Scope.kind == kind, Scope.owner_user_id == owner,
    )


async def test_create_user_duplicate_email_conflict(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    resp = await http.post("/api/admin/users", json={
        "email": "U@Test.dev", "display_name": "Dup",
        "initial_password": "initial-password-1",
    }, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "email_already_registered"


async def test_create_user_validates_password(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.post("/api/admin/users", json={
        "email": "weak@test.dev", "display_name": "W",
        "initial_password": "short",
    }, headers=auth_headers)
    assert resp.status_code == 422


async def test_update_user_email_and_name(client_and_app, auth_headers):
    http, _ = client_and_app
    user_id, _ = await _make_user_token(http, "rename@test.dev")

    resp = await http.patch(
        f"/api/admin/users/{user_id}",
        json={"display_name": "Renamed", "email": "Renamed@Corp.dev",
              "expected_version": (await http.get(
                  f"/api/admin/users/{user_id}", headers=auth_headers,
              )).json()["profile_version"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["email"] == "Renamed@corp.dev"

    # Login works with the new email (normalized).
    login = await http.post("/api/auth/login", json={
        "email": "renamed@corp.dev", "password": "password123",
    })
    assert login.status_code == 200

    # Moving to an email that collides with another user is a 409.
    await _register_second(http, "taken@test.dev")
    resp = await http.patch(
        f"/api/admin/users/{user_id}",
        json={"email": "TAKEN@test.dev", "expected_version": (await http.get(
            f"/api/admin/users/{user_id}", headers=auth_headers,
        )).json()["profile_version"]},
        headers=auth_headers,
    )
    assert resp.status_code == 409


async def test_user_routes_return_404_for_unknown_user(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    for method, path, body in (
        ("GET", "/api/admin/users/none", None),
        ("PATCH", "/api/admin/users/none", {"display_name": "X", "expected_version": 1}),
        ("POST", "/api/admin/users/none/disable", None),
        ("POST", "/api/admin/users/none/enable", None),
        ("POST", "/api/admin/users/none/reset-password",
         {"new_password": "valid-password-1"}),
    ):
        resp = await http.request(
            method, path, json=body, headers=auth_headers,
        )
        assert resp.status_code == 404, (method, path)
        assert resp.json()["detail"]["code"] == "user_not_found"


# ----------------------------------------------------------------------
# Disable / enable lifecycle
# ----------------------------------------------------------------------

async def test_disable_revokes_access_immediately(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    user_id, member_headers = await _make_user_token(http)

    resp = await http.post(
        f"/api/admin/users/{user_id}/disable", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"

    # The member's JWT is rejected right away; login is a uniform 401.
    assert (await http.get(
        "/api/auth/me", headers=member_headers,
    )).status_code == 401
    login = await http.post("/api/auth/login", json={
        "email": "b@test.dev", "password": "password123",
    })
    assert login.status_code == 401

    # Idempotent: a second disable returns the current state.
    resp = await http.post(
        f"/api/admin/users/{user_id}/disable", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


async def test_enable_restores_login(client_and_app, auth_headers):
    http, _ = client_and_app
    user_id, _ = await _make_user_token(http)
    await http.post(
        f"/api/admin/users/{user_id}/disable", headers=auth_headers,
    )

    resp = await http.post(
        f"/api/admin/users/{user_id}/enable", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    login = await http.post("/api/auth/login", json={
        "email": "b@test.dev", "password": "password123",
    })
    assert login.status_code == 200

    # Idempotent enable.
    resp = await http.post(
        f"/api/admin/users/{user_id}/enable", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_last_root_admin_cannot_be_disabled(
    client_and_app, auth_headers,
):
    http, app = client_and_app
    # auth_headers is the sole root admin in a fresh database.
    me = await http.get("/api/auth/me", headers=auth_headers)
    admin_id = me.json()["id"]

    resp = await http.post(
        f"/api/admin/users/{admin_id}/disable", headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "last_root_admin"

    # A second admin lifts the protection.
    second_id = (await _register_second(http))["user"]["id"]
    async with get_sessionmaker()() as db:
        root = await db.scalar(
            select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION),
        )
        admin_role = await db.scalar(
            select(Role).where(Role.key == "admin"),
        )
        db.add(RoleBinding(
            user_id=second_id, role_id=admin_role.id, scope_id=root.id,
        ))
        await db.commit()

    resp = await http.post(
        f"/api/admin/users/{admin_id}/disable", headers=auth_headers,
    )
    assert resp.status_code == 200


# ----------------------------------------------------------------------
# Reset password
# ----------------------------------------------------------------------

async def test_reset_password_revokes_and_forces_change(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    user_id, member_headers = await _make_user_token(http)

    resp = await http.post(
        f"/api/admin/users/{user_id}/reset-password",
        json={"new_password": "admin-set-password-1"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True

    # Old JWT is revoked.
    assert (await http.get(
        "/api/auth/me", headers=member_headers,
    )).status_code == 401

    login = await http.post("/api/auth/login", json={
        "email": "b@test.dev", "password": "admin-set-password-1",
    })
    assert login.status_code == 200
    assert login.json()["user"]["must_change_password"] is True

    # must_change_password can be turned off explicitly.
    resp = await http.post(
        f"/api/admin/users/{user_id}/reset-password",
        json={"new_password": "plain-password-2",
              "must_change_password": False},
        headers=auth_headers,
    )
    assert resp.json()["must_change_password"] is False

    events = await _security_events_for_user(user_id)
    assert events[-1].metadata_["force_change_required"] is False


# ----------------------------------------------------------------------
# Disable account and durable Stop admission (plan §3.7)
# ----------------------------------------------------------------------

async def test_disable_admits_durable_stop_after_account_commit(
    client_and_app, auth_headers, monkeypatch,
):
    from unittest.mock import AsyncMock
    http, app = client_and_app
    user_id, _ = await _make_user_token(http)
    admin_id = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']

    async def stop(db, *, actor_user_id, target_user_id, disable_event_id):
        # An independent connection sees the account denial before Stop admission.
        async with get_sessionmaker()() as reader:
            user = await reader.get(User, target_user_id)
            receipt = await reader.get(SecurityAuditEvent, disable_event_id)
            assert user.status == 'disabled'
            assert receipt.event_type == 'user.disable'
            assert receipt.actor_user_id == actor_user_id == admin_id
            assert receipt.target_user_id == user_id
        return []

    control = AsyncMock(side_effect=stop)
    monkeypatch.setattr(app.state.product_runtime, 'stop_disabled_user_tasks', control, raising=False)
    response = await http.post(f'/api/admin/users/{user_id}/disable', headers=auth_headers)
    assert response.status_code == 200, response.text
    control.assert_awaited_once()
    assert control.await_args.kwargs['target_user_id'] == user_id


async def test_disable_records_each_durable_stop_failure_without_rolling_back_account(
    client_and_app, auth_headers, monkeypatch, caplog,
):
    from unittest.mock import AsyncMock
    http, app = client_and_app
    user_id, _ = await _make_user_token(http)
    control = AsyncMock(return_value=[('task-running', RuntimeError('Stop admission unavailable'))])
    monkeypatch.setattr(app.state.product_runtime, 'stop_disabled_user_tasks', control, raising=False)
    caplog.set_level('WARNING', logger='app.services.user_management')
    response = await http.post(f'/api/admin/users/{user_id}/disable', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'disabled'
    control.assert_awaited_once()
    events = await _security_events_for_user(user_id)
    failure = next(event for event in events if event.event_type == 'user.disable_interrupt_failed')
    assert failure.metadata_['task_id'] == 'task-running'
    assert failure.metadata_['failure'] == 'RuntimeError'
    assert 'task-running' in caplog.text


async def test_disable_survives_durable_control_plane_failure(
    client_and_app, auth_headers, monkeypatch,
):
    from unittest.mock import AsyncMock
    http, app = client_and_app
    user_id, _ = await _make_user_token(http)
    control = AsyncMock(side_effect=RuntimeError('Control plane unavailable'))
    monkeypatch.setattr(app.state.product_runtime, 'stop_disabled_user_tasks', control, raising=False)
    response = await http.post(f'/api/admin/users/{user_id}/disable', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'disabled'
    control.assert_awaited_once()
    events = await _security_events_for_user(user_id)
    assert any(event.event_type == 'user.disable_interrupt_failed' for event in events)


async def test_concurrent_duplicate_email_returns_409_not_500(
    client_and_app, auth_headers, monkeypatch,
):
    """Simulate losing the unique-index race: the pre-check passes but
    the flush hits the constraint - must map to a stable 409 (plan
    §8.1 concurrent duplicate email)."""
    from sqlalchemy.exc import IntegrityError
    from app.services import user_management as service_module

    http, _ = client_and_app

    real_ensure = service_module.UserManagementService._ensure_email_available
    real_flush = service_module.AsyncSession.flush

    async def _silent_ensure(self, normalized, raw=None):
        return None  # pretend the email is free

    async def _failing_flush(self):
        if getattr(_failing_flush, "_armed", False):
            _failing_flush._armed = False
            raise IntegrityError(
                "INSERT users UNIQUE failed", None, Exception("dup"),
            )
        return await real_flush(self)

    monkeypatch.setattr(
        service_module.UserManagementService,
        "_ensure_email_available",
        _silent_ensure,
    )
    monkeypatch.setattr(service_module.AsyncSession, "flush", _failing_flush)
    _failing_flush._armed = True

    resp = await http.post("/api/admin/users", json={
        "email": "raced@test.dev", "display_name": "Raced",
        "initial_password": "initial-password-1",
    }, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "email_already_registered"


async def test_create_user_rolls_back_when_scope_creation_fails(
    client_and_app, monkeypatch,
):
    """User creation must not commit before its access records exist."""
    from app.services import user_management as service_module

    async def _fail_scopes(_db, _user, *, first_user=False):
        raise RuntimeError("scope creation failed")

    monkeypatch.setattr(
        service_module, "_ensure_user_scopes", _fail_scopes,
    )
    async with get_sessionmaker()() as db:
        service = service_module.UserManagementService(db)
        with pytest.raises(RuntimeError, match="scope creation failed"):
            await service.create_user(
                email="atomic@test.dev",
                display_name="Atomic",
                password="initial-password-1",
                must_change_password=True,
            )
        await db.rollback()

    async with get_sessionmaker()() as db:
        assert await db.scalar(
            select(User).where(User.normalized_email == "atomic@test.dev"),
        ) is None
