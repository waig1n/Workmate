# -*- coding: utf-8 -*-
"""User account status, normalized email and JWT token-version semantics
(user management plan §3.2)."""

import time

import jwt as pyjwt
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from workmate_product.persistence.models import User, UserStatus


async def _register(http, email="alice@example.com", name="Alice"):
    resp = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": name,
        "organization_name": "Test organization",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _db():
    async with get_sessionmaker()() as db:
        yield db


async def test_login_normalizes_email_case(client_and_app):
    http, _ = client_and_app
    body = await _register(http, email="Alice@example.com")

    # The display value keeps the original local-part casing (EmailStr
    # already lowercases the domain).
    assert body["user"]["email"] == "Alice@example.com"

    login = await http.post("/api/auth/login", json={
        "email": "  ALICE@EXAMPLE.COM ", "password": "password123",
    })
    assert login.status_code == 200


async def test_register_duplicate_email_case_insensitive(client_and_app):
    http, _ = client_and_app
    await _register(http, email="dup@test.dev")
    resp = await http.post("/api/auth/register", json={
        "email": "DUP@TEST.DEV", "password": "password123",
        "display_name": "Dup",
    })
    assert resp.status_code == 409


async def test_login_updates_last_login_at(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    await http.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "password123",
    })
    async with get_sessionmaker()() as db:
        user = await db.get(User, body["user"]["id"])
        assert user.last_login_at is not None


async def test_disabled_user_rejected_uniformly(client_and_app):
    """A disabled account gets the same 401 for login and old JWTs -
    no account-existence leak (plan §3.2)."""
    http, _ = client_and_app
    body = await _register(http)
    token = {"Authorization": f"Bearer {body['access_token']}"}

    async with get_sessionmaker()() as db:
        user = await db.get(User, body["user"]["id"])
        user.status = UserStatus.DISABLED
        await db.commit()

    login = await http.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "password123",
    })
    assert login.status_code == 401
    assert "disabled" not in login.json()["detail"].lower()

    me = await http.get("/api/auth/me", headers=token)
    assert me.status_code == 401


async def test_token_version_bump_invalidates_jwt(client_and_app):
    http, _ = client_and_app
    body = await _register(http)
    token = {"Authorization": f"Bearer {body['access_token']}"}
    assert (await http.get("/api/auth/me", headers=token)).status_code == 200

    async with get_sessionmaker()() as db:
        user = await db.get(User, body["user"]["id"])
        user.token_version += 1
        await db.commit()

    assert (await http.get("/api/auth/me", headers=token)).status_code == 401


async def test_legacy_token_without_ver_is_version_zero(client_and_app):
    """JWTs issued before the token-version upgrade stay valid (plan
    §3.2 - no forced logout at upgrade time)."""
    http, _ = client_and_app
    body = await _register(http)
    settings = get_settings()
    now = int(time.time())
    legacy = pyjwt.encode(
        {
            "sub": body["user"]["id"],
            "iat": now,
            "exp": now + 3600,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp = await http.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {legacy}"},
    )
    assert resp.status_code == 200


async def test_must_change_password_gates_non_whitelisted_paths(
    client_and_app,
):
    http, _ = client_and_app
    body = await _register(http)
    token = {"Authorization": f"Bearer {body['access_token']}"}

    async with get_sessionmaker()() as db:
        user = await db.get(User, body["user"]["id"])
        user.must_change_password = True
        await db.commit()

    # Whitelisted self-service paths keep working.
    assert (await http.get("/api/auth/me", headers=token)).status_code == 200
    assert (await http.get(
        "/api/auth/me/permissions", headers=token,
    )).status_code == 200

    # Everything else fails with the stable error code.
    resp = await http.get("/api/tasks", headers=token)
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "password_change_required"
