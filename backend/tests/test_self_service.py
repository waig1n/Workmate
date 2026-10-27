# -*- coding: utf-8 -*-
"""Self-service profile and password management tests (plan §5.1)."""

import pytest

from app.db import get_sessionmaker
from workmate_product.persistence.models import User


async def _register(http, email="alice@example.com", name="Alice"):
    resp = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": name,
        "organization_name": "Test organization",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_me_returns_status_fields(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    resp = await http.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    data = resp.json()
    assert data["status"] == "active"
    assert data["must_change_password"] is False
    assert "token_version" not in data
    assert "disabled_by" not in data


async def test_patch_me_updates_display_name(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    resp = await http.patch(
        "/api/auth/me", json={"display_name": "Alice Cooper"}, headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alice Cooper"

    persisted = await http.get("/api/auth/me", headers=headers)
    assert persisted.json()["display_name"] == "Alice Cooper"


async def test_patch_me_rejects_empty_display_name(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    resp = await http.patch(
        "/api/auth/me", json={"display_name": ""},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert resp.status_code == 422


async def test_change_password_rejects_wrong_current(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    resp = await http.post(
        "/api/auth/me/change-password",
        json={"current_password": "wrong-password",
              "new_password": "new-password-456"},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_current_password"


async def test_change_password_rejects_weak_new_password(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    resp = await http.post(
        "/api/auth/me/change-password",
        json={"current_password": "password123", "new_password": "short"},
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert resp.status_code == 422


async def test_change_password_revokes_old_token(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    old_headers = {"Authorization": f"Bearer {body['access_token']}"}

    resp = await http.post(
        "/api/auth/me/change-password",
        json={"current_password": "password123",
              "new_password": "new-password-456"},
        headers=old_headers,
    )
    assert resp.status_code == 200
    fresh = resp.json()
    assert fresh["access_token"]
    assert fresh["user"]["must_change_password"] is False

    # The pre-change token is revoked; the returned one works.
    assert (await http.get(
        "/api/auth/me", headers=old_headers,
    )).status_code == 401
    new_headers = {"Authorization": f"Bearer {fresh['access_token']}"}
    assert (await http.get(
        "/api/auth/me", headers=new_headers,
    )).status_code == 200

    # Login now requires the new password.
    login = await http.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "new-password-456",
    })
    assert login.status_code == 200


async def test_change_password_clears_must_change_flag(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    headers = {"Authorization": f"Bearer {body['access_token']}"}

    async with get_sessionmaker()() as db:
        user = await db.get(User, body["user"]["id"])
        user.must_change_password = True
        await db.commit()

    gated = await http.get("/api/tasks", headers=headers)
    assert gated.status_code == 403

    resp = await http.post(
        "/api/auth/me/change-password",
        json={"current_password": "password123",
              "new_password": "new-password-456"},
        headers=headers,
    )
    assert resp.status_code == 200
    fresh_headers = {
        "Authorization": f"Bearer {resp.json()['access_token']}",
    }
    assert (await http.get("/api/tasks", headers=fresh_headers)).status_code == 200
