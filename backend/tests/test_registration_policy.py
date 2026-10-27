# -*- coding: utf-8 -*-
"""Registration policy (bootstrap_only / closed / open) tests."""

from types import SimpleNamespace

from app.api.routers import auth as auth_router


def _set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        auth_router,
        "get_settings",
        lambda: SimpleNamespace(registration_mode=mode),
    )


async def test_registration_status_available_on_empty_db(client_and_app):
    http, _ = client_and_app
    resp = await http.get("/api/auth/registration-status")
    assert resp.status_code == 200
    assert resp.json() == {"registration_available": True, "bootstrap_required": True}


async def test_registration_status_exposes_no_tenant_info(client_and_app):
    """The public status must not leak user counts, emails or mode."""
    http, _ = client_and_app
    await http.post("/api/auth/register", json={
        "email": "u@test.dev", "password": "password123", "display_name": "T", "organization_name": "Test Organization",
    })
    resp = await http.get("/api/auth/registration-status")
    assert resp.status_code == 200
    assert set(resp.json()) == {"registration_available", "bootstrap_required"}


async def test_bootstrap_only_closes_after_first_user(
    client_and_app, monkeypatch,
):
    _set_mode(monkeypatch, "bootstrap_only")
    http, _ = client_and_app
    first = await http.post("/api/auth/register", json={
        "email": "u@test.dev", "password": "password123", "display_name": "T", "organization_name": "Test Organization",
    })
    assert first.status_code == 200
    status = await http.get("/api/auth/registration-status")
    assert status.json()["registration_available"] is False

    second = await http.post("/api/auth/register", json={
        "email": "b@test.dev", "password": "password123", "display_name": "B",
    })
    assert second.status_code == 403


async def test_closed_mode_rejects_registration(client_and_app, monkeypatch):
    _set_mode(monkeypatch, "closed")
    http, _ = client_and_app
    status = await http.get("/api/auth/registration-status")
    assert status.json()["registration_available"] is False

    resp = await http.post("/api/auth/register", json={
        "email": "u@test.dev", "password": "password123", "display_name": "T", "organization_name": "Test Organization",
    })
    assert resp.status_code == 403


async def test_open_mode_keeps_self_registration(client_and_app, monkeypatch):
    _set_mode(monkeypatch, "open")
    http, _ = client_and_app
    first = await http.post("/api/auth/register", json={
        "email": "u@test.dev", "password": "password123", "display_name": "T", "organization_name": "Test Organization",
    })
    assert first.status_code == 200
    token = first.json()["access_token"]
    admin_perms = await http.get(
        "/api/auth/me/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert admin_perms.json()["is_root_admin"] is True

    status = await http.get("/api/auth/registration-status")
    assert status.json()["registration_available"] is True

    second = await http.post("/api/auth/register", json={
        "email": "b@test.dev", "password": "password123", "display_name": "B",
    })
    assert second.status_code == 200
    member_perms = await http.get(
        "/api/auth/me/permissions",
        headers={"Authorization": f"Bearer {second.json()['access_token']}"},
    )
    body = member_perms.json()
    assert body["is_root_admin"] is False
    assert "scope.manage" not in body["permissions"]
