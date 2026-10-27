# -*- coding: utf-8 -*-
"""Native Product API registration and explicitly shared Agent access flows."""


async def test_register_first_user_is_admin(client_and_app):
    http, _ = client_and_app
    resp = await http.post("/api/auth/register", json={
        "email": "a@t.dev", "password": "password123", "display_name": "A", "organization_name": "Test Organization",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    perms = await http.get(
        "/api/auth/me/permissions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert perms.status_code == 200
    body = perms.json()
    assert body["is_root_admin"] is True
    assert "agent.create" in body["permissions"]
    # The second registrant is a plain member: personal scope, no grants.
    await http.post("/api/auth/register", json={
        "email": "b@t.dev", "password": "password123", "display_name": "B",
    })
    login = await http.post("/api/auth/login", json={
        "email": "b@t.dev", "password": "password123",
    })
    member = await http.get(
        "/api/auth/me/permissions",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    member_body = member.json()
    assert member_body["is_root_admin"] is False
    assert member_body["permissions"] == []
    assert member_body["personal_scope_id"]


async def test_member_cannot_mutate_agents_but_can_use_them(
    client_and_app, auth_headers,
):
    """RBAC design §7: members manage their personal agents, but
    organization agents are only mutated with agent.update; an explicitly
    shared organization agent stays usable without management permission.
    """
    http, _ = client_and_app
    # auth_headers user is the first user => root admin.
    await http.post("/api/auth/register", json={
        "email": "m@t.dev", "password": "password123", "display_name": "M",
    })
    login = await http.post("/api/auth/login", json={
        "email": "m@t.dev", "password": "password123",
    })
    member_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Members may create agents - but only in their personal scope.
    resp = await http.post("/api/agents", json={
        "slug": "member-agent", "display_name": "X",
    }, headers=member_headers)
    assert resp.status_code == 201
    assert resp.json()["scope"]["kind"] == "personal"

    # A public organization agent: members see and use it, but cannot
    # mutate it (403 - visible yet forbidden, RBAC §7).
    scopes = await http.get("/api/scopes/accessible", headers=auth_headers)
    assert scopes.status_code == 200, scopes.text
    root_scope = next(
        s for s in scopes.json() if s["kind"] == "organization"
    )
    org_agent = await http.post("/api/agents", json={
        "slug": "org-agent", "display_name": "Org",
        "scope_id": root_scope["id"], "visibility": "public",
    }, headers=auth_headers)
    assert org_agent.status_code == 201, org_agent.text
    org_agent_id = org_agent.json()["id"]
    share_url = f"/api/sharing/agents/{org_agent_id}"
    preview = await http.post(share_url + '/preview', headers=auth_headers,
        json={'target_scope_id': root_scope['id']})
    assert preview.status_code == 200, preview.text
    shared = await http.post(share_url, headers=auth_headers,
        json={'target_scope_id': root_scope['id'],
              'confirmation': preview.json()['confirmation']})
    assert shared.status_code == 200, shared.text

    listed = await http.get(
        "/api/agents?scope=organization", headers=member_headers,
    )
    assert listed.status_code == 200
    assert any(a["id"] == org_agent_id for a in listed.json()['items'])

    resp = await http.patch(f"/api/agents/{org_agent_id}", json={
        "system_prompt": "hijacked",
    }, headers=member_headers)
    assert resp.status_code == 403

    conversation = await http.post("/api/conversations", json={
        "agent_id": org_agent_id, "title": "Org chat",
    }, headers=member_headers)
    assert conversation.status_code == 201, conversation.text
    assert conversation.json()["read_only"] is False

    # Another user's personal agent is invisible (404, not 403).
    resp = await http.patch("/api/agents/made-up-id", json={
        "display_name": "nope",
    }, headers=member_headers)
    assert resp.status_code == 404
