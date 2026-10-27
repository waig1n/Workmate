# -*- coding: utf-8 -*-
"""Native API edge paths: auth failures, ownership and missing resources."""


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

async def test_login_wrong_password(client_and_app):
    http, _ = client_and_app
    await http.post("/api/auth/register", json={
        "email": "u@test.dev", "password": "password123",
        "display_name": "T", "organization_name": "Test Organization",
    })
    resp = await http.post("/api/auth/login", json={
        "email": "u@test.dev", "password": "nope",
    })
    assert resp.status_code == 401


async def test_login_unknown_email(client_and_app):
    http, _ = client_and_app
    resp = await http.post("/api/auth/login", json={
        "email": "ghost@test.dev", "password": "x",
    })
    assert resp.status_code == 401


async def test_me_requires_token(client_and_app):
    http, _ = client_and_app
    assert (await http.get("/api/auth/me")).status_code == 401


async def test_register_duplicate_email(client_and_app):
    http, _ = client_and_app
    body = {"email": "dup@test.dev", "password": "password123",
            "display_name": "D"}
    first = await http.post("/api/auth/register", json={**body, "organization_name": "Test Organization"})
    assert first.status_code == 200, first.text
    resp = await http.post("/api/auth/register", json=body)
    assert resp.status_code == 409


# ----------------------------------------------------------------------
# Agents
# ----------------------------------------------------------------------

async def test_agent_duplicate_slug(client_and_app, auth_headers):
    http, _ = client_and_app
    await http.post("/api/agents", json={
        "slug": "dup", "display_name": "A",
    }, headers=auth_headers)
    resp = await http.post("/api/agents", json={
        "slug": "dup", "display_name": "B",
    }, headers=auth_headers)
    assert resp.status_code == 409


async def test_agent_get_and_404(client_and_app, auth_headers, seeded_ids):
    http, _ = client_and_app
    agent_id, _ = seeded_ids
    resp = await http.get(f"/api/agents/{agent_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert (await http.get(
        "/api/agents/does-not-exist", headers=auth_headers,
    )).status_code == 404


async def test_agent_update_and_delete(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.post("/api/agents", json={
        "slug": "temp", "display_name": "Temp",
    }, headers=auth_headers)
    agent_id = resp.json()["id"]

    resp = await http.patch(
        f"/api/agents/{agent_id}",
        json={"display_name": "Renamed"}, headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    persisted = await http.get(
        f"/api/agents/{agent_id}", headers=auth_headers,
    )
    assert persisted.json()["display_name"] == "Renamed"

    resp = await http.delete(f"/api/agents/{agent_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert (await http.get(
        f"/api/agents/{agent_id}", headers=auth_headers,
    )).status_code == 404


# ----------------------------------------------------------------------
# Conversations
# ----------------------------------------------------------------------

async def test_conversation_unknown_agent(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.post("/api/conversations", json={
        "agent_id": "nope", "title": "x",
    }, headers=auth_headers)
    assert resp.status_code == 404


async def test_conversation_get_delete_messages(
    client_and_app, auth_headers, seeded_ids,
):
    http, _ = client_and_app
    _, conv_id = seeded_ids

    resp = await http.get(f"/api/conversations/{conv_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["title"] == "T1"

    resp = await http.get(
        f"/api/conversations/{conv_id}/messages", headers=auth_headers,
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

    resp = await http.patch(
        f"/api/conversations/{conv_id}",
        json={"title": "Renamed conversation"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed conversation"

    resp = await http.delete(
        f"/api/conversations/{conv_id}", headers=auth_headers,
    )
    assert resp.status_code == 204
    assert (await http.get(
        f"/api/conversations/{conv_id}", headers=auth_headers,
    )).status_code == 404


# ----------------------------------------------------------------------
# Skills
# ----------------------------------------------------------------------

async def test_agent_rejects_uninstalled_skill(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    response = await http.post(
        "/api/agents",
        json={
            "slug": "invalid-skill",
            "display_name": "Invalid Skill",
            # Bindings reference installations by stable ID (RBAC §8);
            # a well-formed but nonexistent UUID is a 422.
            "skill_binding_ids": [
                "00000000-0000-4000-8000-000000000001",
            ],
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_conversation_other_user_404(
    client_and_app, auth_headers, seeded_ids,
):
    http, _ = client_and_app
    _, conv_id = seeded_ids
    # register a second user
    resp = await http.post("/api/auth/register", json={
        "email": "b@test.dev", "password": "password123", "display_name": "B",
    })
    other = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert (await http.get(
        f"/api/conversations/{conv_id}", headers=other,
    )).status_code == 404


# ----------------------------------------------------------------------
# Tasks: guard rails + timeline
# ----------------------------------------------------------------------

async def test_task_not_found(client_and_app, auth_headers):
    http, _ = client_and_app
    assert (await http.get(
        "/api/tasks/none", headers=auth_headers,
    )).status_code == 404


# ----------------------------------------------------------------------
# Artifacts
# ----------------------------------------------------------------------

async def test_artifact_download_404(client_and_app, auth_headers):
    http, _ = client_and_app
    assert (await http.get(
        "/api/artifacts/none/download", headers=auth_headers,
    )).status_code == 404
    assert (await http.get(
        "/api/artifacts", headers=auth_headers,
    )).json() == []
