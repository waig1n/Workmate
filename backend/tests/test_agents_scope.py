# -*- coding: utf-8 -*-
"""Scoped agent API tests (RBAC design §6/§7/§11)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.selectable import Select

from app.api.routers import agents as agents_router
from app.auth.permissions import (
    AGENT_CREATE,
    AGENT_DELETE,
    AGENT_PUBLISH,
    AGENT_UPDATE,
)
from workmate_product.persistence.models import (
    Agent,
    AgentDefault,
    CapabilityInstallation,
    Permission,
    ResourceShare,
    Role,
    RoleBinding,
    RolePermission,
    Scope,
    ScopeKind,
    ScopeMembership,
)
from app.services import capabilities
from app.services.bootstrap import create_scope, get_root_scope


SKILL_CARD = {
    "hub_id": "clawhub", "id": "agent-skill", "name": "agent-skill",
    "display_name": "Agent Skill", "description": "", "tags": [],
    "version": "1.0.0",
}

MCP_CARD = {
    "hub_id": "github", "id": "agent-mcp", "name": "agent-mcp",
    "display_name": "Agent MCP", "description": "", "tags": [],
    "is_stateful": False,
}


async def _register(http, email: str) -> tuple[dict[str, str], str]:
    response = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": email,
    })
    assert response.status_code == 200, response.text
    headers = {
        "Authorization": f"Bearer {response.json()['access_token']}",
    }
    me = await http.get("/api/auth/me", headers=headers)
    return headers, me.json()["id"]


async def _role_with_permission(db, key: str, permission_key: str) -> Role:
    role = Role(id=str(uuid.uuid4()), key=key, display_name=key)
    permission = await db.scalar(
        select(Permission).where(Permission.key == permission_key),
    )
    db.add(role)
    await db.flush()
    db.add(RolePermission(
        id=str(uuid.uuid4()), role_id=role.id, permission_id=permission.id,
    ))
    await db.flush()
    return role


@pytest.mark.parametrize(
    ("scope", "agent_owner", "capability_owner"),
    [
        (
            Scope(
                id="personal-scope",
                kind=ScopeKind.PERSONAL,
                parent_id=None,
                owner_user_id="user-1",
                name="Personal",
            ),
            "user-1",
            "user-1",
        ),
        (
            Scope(
                id="org-scope",
                kind=ScopeKind.ORGANIZATION,
                parent_id=None,
                owner_user_id=None,
                name="Organization",
            ),
            "workmate-service",
            "workmate-scope:org-scope",
        ),
        (
            Scope(
                id="department-scope",
                kind=ScopeKind.DEPARTMENT,
                parent_id="org-scope",
                owner_user_id=None,
                name="Department",
            ),
            "workmate-service",
            "workmate-scope:department-scope",
        ),
    ],
)
def test_runtime_principals_distinguish_agents_from_capabilities(
    scope, agent_owner, capability_owner,
):
    """A scope's agent and capability resources use distinct principals."""
    assert capabilities.agent_runtime_principal(scope) == agent_owner


@pytest.mark.asyncio
async def test_agent_slug_unique_per_scope(client_and_app, auth_headers):
    """Same slug may exist in different scopes (RBAC §6)."""
    http, _ = client_and_app
    first = await http.post("/api/agents", json={
        "slug": "shared", "display_name": "Personal",
    }, headers=auth_headers)
    assert first.status_code == 201

    duplicate = await http.post("/api/agents", json={
        "slug": "shared", "display_name": "Dup",
    }, headers=auth_headers)
    assert duplicate.status_code == 409

    scopes = await http.get("/api/scopes/accessible", headers=auth_headers)
    root = next(s for s in scopes.json() if s["kind"] == "organization")
    org = await http.post("/api/agents", json={
        "slug": "shared", "display_name": "Org",
        "scope_id": root["id"], "visibility": "public",
    }, headers=auth_headers)
    assert org.status_code == 201


@pytest.mark.asyncio
async def test_agent_tool_policy_round_trips_and_validates(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    created = await http.post("/api/agents", json={
        "slug": "policy-agent",
        "display_name": "Policy Agent",
        "tool_policy": {
            "denied_groups": ["shell", "external_connectors"],
            "denied_tools": ["DeleteUser"],
        },
    }, headers=auth_headers)
    assert created.status_code == 201, created.text
    assert created.json()["tool_policy"] == {
        "denied_groups": ["shell", "external_connectors"],
        "denied_tools": ["DeleteUser"],
    }

    updated = await http.patch(
        f"/api/agents/{created.json()['id']}",
        json={"tool_policy": {"denied_groups": ["workspace_write"]}},
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tool_policy"] == {
        "denied_groups": ["workspace_write"],
        "denied_tools": [],
    }

    invalid = await http.patch(
        f"/api/agents/{created.json()['id']}",
        json={"tool_policy": {"denied_groups": ["not-a-group"]}},
        headers=auth_headers,
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_personal_agent_must_be_private(client_and_app, auth_headers):
    http, _ = client_and_app
    resp = await http.post("/api/agents", json={
        "slug": "no-publish", "display_name": "X", "visibility": "public",
    }, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_filters_by_scope_kind(client_and_app, auth_headers):
    http, _ = client_and_app
    scopes = await http.get("/api/scopes/accessible", headers=auth_headers)
    root = next(s for s in scopes.json() if s["kind"] == "organization")

    await http.post("/api/agents", json={
        "slug": "mine", "display_name": "Mine",
    }, headers=auth_headers)
    await http.post("/api/agents", json={
        "slug": "ours", "display_name": "Ours",
        "scope_id": root["id"], "visibility": "public",
    }, headers=auth_headers)

    personal = await http.get(
        "/api/agents?scope=personal", headers=auth_headers,
    )
    assert [a["slug"] for a in personal.json()['items']] == ["mine"]
    org = await http.get(
        "/api/agents?scope=organization", headers=auth_headers,
    )
    assert [a["slug"] for a in org.json()['items']] == ["ours"]
    everything = await http.get("/api/agents", headers=auth_headers)
    assert {a["slug"] for a in everything.json()['items']} == {"mine", "ours"}


@pytest.mark.asyncio
async def test_member_cannot_see_org_private_agent(client_and_app,
                                                   auth_headers, db_session):
    """Draft (private) org agents are visible to their creator and
    managers only (RBAC §7)."""
    http, _ = client_and_app
    scopes = await http.get("/api/scopes/accessible", headers=auth_headers)
    root = next(s for s in scopes.json() if s["kind"] == "organization")
    draft = await http.post("/api/agents", json={
        "slug": "draft", "display_name": "Draft",
        "scope_id": root["id"], "visibility": "private",
    }, headers=auth_headers)
    assert draft.status_code == 201
    draft_id = draft.json()["id"]

    await http.post("/api/auth/register", json={
        "email": "m2@t.dev", "password": "password123", "display_name": "M2",
    })
    login = await http.post("/api/auth/login", json={
        "email": "m2@t.dev", "password": "password123",
    })
    member = {"Authorization": f"Bearer {login.json()['access_token']}"}

    listed = await http.get("/api/agents", headers=member)
    assert all(a["id"] != draft_id for a in listed.json()['items'])
    resp = await http.get(f"/api/agents/{draft_id}", headers=member)
    assert resp.status_code == 404

    # Publishing alone does not grant use; an explicit grant is required.
    publish = await http.patch(f"/api/agents/{draft_id}", json={
        "visibility": "public",
    }, headers=auth_headers)
    assert publish.status_code == 200
    assert publish.json()["actions"]["use"] is False
    resp = await http.get(f"/api/agents/{draft_id}", headers=member)
    assert resp.status_code == 404
    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), agent_id=draft_id,
        target_scope_id=root["id"], include_descendants=True,
    ))
    await db_session.commit()
    resp = await http.get(f"/api/agents/{draft_id}", headers=member)
    assert resp.status_code == 200
    assert resp.json()["actions"]["use"] is True


@pytest.mark.asyncio
async def test_member_cannot_publish(client_and_app, auth_headers):
    """Publishing (private->public) needs agent.publish (RBAC §7)."""
    http, _ = client_and_app
    scopes = await http.get("/api/scopes/accessible", headers=auth_headers)
    root = next(s for s in scopes.json() if s["kind"] == "organization")
    draft = await http.post("/api/agents", json={
        "slug": "draft2", "display_name": "Draft",
        "scope_id": root["id"], "visibility": "private",
    }, headers=auth_headers)
    draft_id = draft.json()["id"]

    # A member cannot even create org agents (agent.create missing)...
    await http.post("/api/auth/register", json={
        "email": "m3@t.dev", "password": "password123", "display_name": "M3",
    })
    login = await http.post("/api/auth/login", json={
        "email": "m3@t.dev", "password": "password123",
    })
    member = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await http.post("/api/agents", json={
        "slug": "member-org", "display_name": "X",
        "scope_id": root["id"], "visibility": "public",
    }, headers=member)
    assert resp.status_code == 403

    # ...and cannot publish the admin's draft either (404: invisible).
    resp = await http.patch(f"/api/agents/{draft_id}", json={
        "visibility": "public",
    }, headers=member)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_agent_preserves_readonly_conversation(client_and_app,
                                                  auth_headers):
    http, _ = client_and_app
    agent = await http.post("/api/agents", json={
        "slug": "chatty", "display_name": "Chatty",
    }, headers=auth_headers)
    agent_id = agent.json()["id"]
    conversation = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "T",
    }, headers=auth_headers)
    assert conversation.status_code == 201

    resp = await http.delete(f"/api/agents/{agent_id}", headers=auth_headers)
    assert resp.status_code == 204
    history = await http.get(
        f"/api/conversations/{conversation.json()['id']}", headers=auth_headers,
    )
    assert history.status_code == 200
    assert history.json()["read_only"] is True
    assert "已删除" in history.json()["read_only_reason"]
    denied = await http.post(
        f"/api/tasks/conversations/{conversation.json()['id']}/tasks",
        json={"message": "continue"}, headers=auth_headers,
    )
    assert denied.status_code == 403
    assert (await http.get(f"/api/agents/{agent_id}", headers=auth_headers)).status_code == 404
    listed = await http.get("/api/agents", headers=auth_headers)
    assert agent_id not in {item["id"] for item in listed.json()['items']}
    assert (await http.post("/api/conversations", json={
        "agent_id": agent_id,
    }, headers=auth_headers)).status_code == 404


@pytest.mark.asyncio
async def test_default_agent_resolution(client_and_app, auth_headers):
    """is_default is resolved per requesting user (RBAC §6.1)."""
    http, _ = client_and_app
    resp = await http.post("/api/agents", json={
        "slug": "fallback", "display_name": "Fallback", "is_default": True,
    }, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["is_default"] is True

    listed = await http.get("/api/agents", headers=auth_headers)
    by_slug = {a["slug"]: a for a in listed.json()['items']}
    assert by_slug["fallback"]["is_default"] is True

    # Another agent shows is_default=False until the default moves.
    other = await http.post("/api/agents", json={
        "slug": "other", "display_name": "Other",
    }, headers=auth_headers)
    assert other.json()["is_default"] is False


@pytest.mark.asyncio
async def test_create_default_replaces_existing_scope_default(
    client_and_app, auth_headers, db_session,
):
    """Creating a second default upserts instead of inserting the scope PK."""
    http, _ = client_and_app
    first = await http.post("/api/agents", json={
        "slug": "first-default", "display_name": "First", "is_default": True,
    }, headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await http.post("/api/agents", json={
        "slug": "second-default", "display_name": "Second", "is_default": True,
    }, headers=auth_headers)
    assert second.status_code == 201, second.text
    assert second.json()["is_default"] is True

    default = await db_session.get(AgentDefault, second.json()["scope_id"])
    assert default.agent_id == second.json()["id"]


@pytest.mark.asyncio
async def test_accessible_scopes_follow_memberships_and_role_inheritance(
    client_and_app, auth_headers, db_session,
):
    """Membership ancestors and managed descendants are visible, not siblings."""
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    member_parent = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Member Parent", parent=root,
    )
    member_leaf = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Member Leaf", parent=member_parent,
    )
    sibling = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Unrelated Sibling", parent=root,
    )
    managed = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Managed", parent=root,
    )
    managed_child = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Managed Child", parent=managed,
    )
    managed_grandchild = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Managed Grandchild", parent=managed_child,
    )
    await db_session.commit()

    member_headers, member_id = await _register(http, "scope-member@t.dev")
    db_session.add(ScopeMembership(
        scope_id=member_leaf.id, user_id=member_id,
    ))
    await db_session.commit()

    accessible = await http.get(
        "/api/scopes/accessible", headers=member_headers,
    )
    assert accessible.status_code == 200, accessible.text
    ids = {scope["id"] for scope in accessible.json()}
    personal_ids = {
        scope["id"] for scope in accessible.json()
        if scope["kind"] == "personal"
    }
    assert len(personal_ids) == 1
    assert {root.id, member_parent.id, member_leaf.id} <= ids
    assert {sibling.id, managed.id, managed_child.id} & ids == set()
    assert all(scope["permissions"] == [] for scope in accessible.json())

    admin_role = await db_session.scalar(
        select(Role).where(Role.key == "admin"),
    )
    binding = RoleBinding(
        user_id=member_id,
        role_id=admin_role.id,
        scope_id=managed.id,
        include_descendants=False,
    )
    db_session.add(binding)
    await db_session.commit()
    managed_response = await http.get(
        "/api/scopes/accessible", headers=member_headers,
    )
    by_id = {scope["id"]: scope for scope in managed_response.json()}
    ids = set(by_id)
    assert managed.id in ids
    assert managed_child.id not in ids
    assert AGENT_CREATE in by_id[managed.id]["permissions"]
    assert by_id[member_leaf.id]["permissions"] == []
    assert by_id[root.id]["permissions"] == []

    binding.include_descendants = True
    await db_session.commit()
    ids = {
        scope["id"] for scope in (
            await http.get("/api/scopes/accessible", headers=member_headers)
        ).json()
    }
    assert {managed.id, managed_child.id, managed_grandchild.id} <= ids
    assert sibling.id not in ids


@pytest.mark.asyncio
async def test_personal_agent_binds_member_scope_and_rejects_other_department(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Personal Skill bindings require a grant to an owner membership."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD, {**SKILL_CARD, "id": "other-skill"}]
    root = await get_root_scope(db_session)
    department_a = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT, name="A", parent=root,
    )
    department_b = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT, name="B", parent=root,
    )
    me = await http.get("/api/auth/me", headers=auth_headers)
    db_session.add(ScopeMembership(
        scope_id=department_a.id, user_id=me.json()["id"],
    ))
    await db_session.commit()

    allowed = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "agent-skill",
        "scope_id": root.id, "name": "allowed-skill",
    }, headers=auth_headers)
    unrelated = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "other-skill",
        "scope_id": root.id, "name": "unrelated-skill",
    }, headers=auth_headers)
    assert allowed.status_code == unrelated.status_code == 201
    for installed, target in ((allowed, department_a), (unrelated, department_b)):
        db_session.add(ResourceShare(
            id=str(uuid.uuid4()), capability_id=installed.json()["id"],
            target_scope_id=target.id, include_descendants=False,
        ))
    await db_session.commit()

    legal_agent = await http.post("/api/agents", json={
        "slug": "legal-capability", "display_name": "Legal",
        "skill_binding_ids": [allowed.json()["id"]],
    }, headers=auth_headers)
    assert legal_agent.status_code == 201, legal_agent.text

    illegal_agent = await http.post("/api/agents", json={
        "slug": "cross-department", "display_name": "Cross",
        "skill_binding_ids": [unrelated.json()["id"]],
    }, headers=auth_headers)
    assert illegal_agent.status_code == 422, illegal_agent.text


@pytest.mark.asyncio
async def test_agent_manager_gets_contextual_capability_candidates(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Agent management rights expose bindable metadata without granting
    membership or Capability mutation rights."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD]
    root = await get_root_scope(db_session)
    department = await create_scope(
        db_session, kind=ScopeKind.DEPARTMENT,
        name="Agent-managed", parent=root,
    )
    await db_session.commit()
    installed = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "agent-skill",
        "scope_id": root.id,
    }, headers=auth_headers)
    agent = await http.post("/api/agents", json={
        "slug": "context-candidates", "display_name": "Context",
        "scope_id": department.id, "visibility": "private",
    }, headers=auth_headers)
    assert installed.status_code == agent.status_code == 201
    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), capability_id=installed.json()["id"],
        target_scope_id=department.id, include_descendants=False,
    ))
    await db_session.commit()

    manager, manager_id = await _register(http, "agent-manager@t.dev")
    update_role = await _role_with_permission(
        db_session, "agent-context-update", AGENT_UPDATE,
    )
    db_session.add(RoleBinding(
        user_id=manager_id,
        role_id=update_role.id,
        scope_id=department.id,
        include_descendants=False,
    ))
    await db_session.commit()

    generic = await http.get("/api/skills", headers=manager)
    assert generic.status_code == 200
    assert generic.json()['items'] == []
    candidates = await http.get(
        "/api/agents/capability-candidates",
        params={"agent_id": agent.json()["id"]},
        headers=manager,
    )
    assert candidates.status_code == 200, candidates.text
    assert candidates.json() == [{
        **installed.json(),
        "manageable": False,
        "usable": False,
    }]

    bound = await http.patch(
        f"/api/agents/{agent.json()['id']}",
        json={"skill_binding_ids": [installed.json()["id"]]},
        headers=manager,
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["skill_binding_ids"] == [installed.json()["id"]]


@pytest.mark.asyncio
async def test_agent_patch_preserves_omitted_binding_type(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Replacing Skill bindings must not erase omitted MCP bindings."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD]
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    skill = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "agent-skill", "scope_id": root.id,
    }, headers=auth_headers)
    mcp = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "agent-mcp", "scope_id": root.id,
        "values": {},
    }, headers=auth_headers)
    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), capability_id=skill.json()["id"],
        target_scope_id=root.id, include_descendants=True,
    ))
    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), capability_id=mcp.json()["id"],
        target_scope_id=root.id, include_descendants=False,
    ))
    await db_session.commit()
    agent = await http.post("/api/agents", json={
        "slug": "partial-bindings", "display_name": "Partial",
        "skill_binding_ids": [skill.json()["id"]],
        "mcp_binding_ids": [mcp.json()["id"]],
    }, headers=auth_headers)
    assert agent.status_code == 201, agent.text

    updated = await http.patch(
        f"/api/agents/{agent.json()['id']}",
        json={"skill_binding_ids": []}, headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["skill_binding_ids"] == []
    assert updated.json()["mcp_binding_ids"] == [mcp.json()["id"]]

    restored = await http.patch(
        f"/api/agents/{agent.json()['id']}",
        json={"skill_binding_ids": [skill.json()["id"]]},
        headers=auth_headers,
    )
    assert restored.status_code == 200, restored.text
    updated = await http.patch(
        f"/api/agents/{agent.json()['id']}",
        json={"mcp_binding_ids": []}, headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["skill_binding_ids"] == [skill.json()["id"]]
    assert updated.json()["mcp_binding_ids"] == []


@pytest.mark.asyncio
async def test_personal_agent_patch_rejects_public(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    created = await http.post("/api/agents", json={
        "slug": "private-forever", "display_name": "Private",
    }, headers=auth_headers)
    updated = await http.patch(
        f"/api/agents/{created.json()['id']}",
        json={"visibility": "public"}, headers=auth_headers,
    )
    assert updated.status_code == 422, updated.text


@pytest.mark.asyncio
async def test_public_organization_create_requires_publish_permission(
    client_and_app, auth_headers, db_session,
):
    """agent.create alone can create a draft, but cannot publish on create."""
    http, _ = client_and_app
    creator, creator_id = await _register(http, "creator-only@t.dev")
    root = await get_root_scope(db_session)
    role = await _role_with_permission(
        db_session, "creator-only", AGENT_CREATE,
    )
    db_session.add(RoleBinding(
        user_id=creator_id, role_id=role.id, scope_id=root.id,
        include_descendants=False,
    ))
    await db_session.commit()

    public = await http.post("/api/agents", json={
        "slug": "publish-on-create", "display_name": "Public",
        "scope_id": root.id, "visibility": "public",
    }, headers=creator)
    assert public.status_code == 403, public.text
    private = await http.post("/api/agents", json={
        "slug": "draft-on-create", "display_name": "Draft",
        "scope_id": root.id, "visibility": "private",
    }, headers=creator)
    assert private.status_code == 201, private.text


@pytest.mark.asyncio
async def test_organization_delete_requires_agent_delete(
    client_and_app, auth_headers, db_session,
):
    """agent.update must not accidentally authorize organization deletion."""
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    first = await http.post("/api/agents", json={
        "slug": "delete-protected", "display_name": "Protected",
        "scope_id": root.id, "visibility": "public",
    }, headers=auth_headers)
    second = await http.post("/api/agents", json={
        "slug": "delete-allowed", "display_name": "Allowed",
        "scope_id": root.id, "visibility": "private",
    }, headers=auth_headers)

    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), agent_id=first.json()["id"],
        target_scope_id=root.id, include_descendants=True,
    ))
    await db_session.commit()
    updater, updater_id = await _register(http, "updater-only@t.dev")
    update_role = await _role_with_permission(
        db_session, "updater-only", AGENT_UPDATE,
    )
    db_session.add(RoleBinding(
        user_id=updater_id, role_id=update_role.id, scope_id=root.id,
        include_descendants=False,
    ))
    await db_session.commit()
    deleter, deleter_id = await _register(http, "deleter-only@t.dev")
    hidden = await http.delete(
        f"/api/agents/{second.json()['id']}", headers=deleter,
    )
    assert hidden.status_code == 404, hidden.text
    delete_role = await _role_with_permission(
        db_session, "deleter-only", AGENT_DELETE,
    )
    db_session.add(RoleBinding(
        user_id=deleter_id, role_id=delete_role.id, scope_id=root.id,
        include_descendants=False,
    ))
    await db_session.commit()
    publisher, publisher_id = await _register(http, "publisher-only@t.dev")
    publish_role = await _role_with_permission(
        db_session, "publisher-only", AGENT_PUBLISH,
    )
    db_session.add(RoleBinding(
        user_id=publisher_id, role_id=publish_role.id, scope_id=root.id,
        include_descendants=False,
    ))
    await db_session.commit()

    updater_view = await http.get(
        f"/api/agents/{first.json()['id']}", headers=updater,
    )
    assert updater_view.status_code == 200
    assert updater_view.json()["actions"] == {
        "use": True, "update": True, "delete": False, "publish": False,
    }
    deleter_view = await http.get(
        f"/api/agents/{first.json()['id']}", headers=deleter,
    )
    assert deleter_view.status_code == 200
    assert deleter_view.json()["actions"] == {
        "use": True, "update": False, "delete": True, "publish": False,
    }
    publisher_view = await http.get(
        f"/api/agents/{first.json()['id']}", headers=publisher,
    )
    assert publisher_view.status_code == 200
    assert publisher_view.json()["actions"]["publish"] is False

    forbidden = await http.delete(
        f"/api/agents/{first.json()['id']}", headers=updater,
    )
    assert forbidden.status_code == 403, forbidden.text
    deleted = await http.delete(
        f"/api/agents/{second.json()['id']}", headers=deleter,
    )
    assert deleted.status_code == 204, deleted.text


@pytest.mark.asyncio
async def test_workspace_platform_profile_is_admin_controlled_and_org_only(
    client_and_app,
    auth_headers,
    db_session,
):
    http, _ = client_and_app
    personal = await http.post("/api/agents", headers=auth_headers, json={
        "slug": "personal-platform-tools",
        "display_name": "Personal",
        "platform_tool_profile": "workspace",
    })
    assert personal.status_code == 422

    root = await get_root_scope(db_session)
    organization = await http.post("/api/agents", headers=auth_headers, json={
        "slug": "organization-platform-tools",
        "display_name": "Organization",
        "scope_id": root.id,
        "platform_tool_profile": "workspace",
    })
    assert organization.status_code == 201, organization.text
    assert organization.json()["platform_tool_profile"] == "workspace"


@pytest.mark.asyncio
async def test_capability_candidates_search_and_paging(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    for index in range(5):
        skill_id = str(uuid.uuid4())
        db_session.add(CapabilityInstallation(
            id=skill_id, capability_type="skill", scope_id=root.id,

            name=f"skill-{index}", display_name=f"写作技能{index}", enabled=True,
        ))
        await db_session.flush()
        db_session.add(ResourceShare(
            id=str(uuid.uuid4()), capability_id=skill_id,
            target_scope_id=root.id, include_descendants=True,
        ))
    db_session.add(CapabilityInstallation(
        id=str(uuid.uuid4()), capability_type="mcp", scope_id=root.id,
          name="skill-mcp", enabled=True,
    ))
    await db_session.commit()
    params = {"scope_id": root.id, "capability_type": "skill", "limit": 2}
    seen = []
    for offset, count in [(0, 2), (2, 2), (4, 1)]:
        response = await http.get("/api/agents/capability-candidates", params={**params, "offset": offset}, headers=auth_headers)
        assert response.status_code == 200, response.text
        assert len(response.json()) == count
        seen.extend(item["id"] for item in response.json())
    assert len(set(seen)) == 5
    found = await http.get("/api/agents/capability-candidates", params={**params, "q": "写作技能3"}, headers=auth_headers)
    assert [item["name"] for item in found.json()] == ["skill-3"]
    literal = await http.get("/api/agents/capability-candidates", params={**params, "q": "%"}, headers=auth_headers)
    assert literal.json() == []
    invalid = await http.get("/api/agents/capability-candidates", params={**params, "limit": 101}, headers=auth_headers)
    assert invalid.status_code == 422
    outsider, _ = await _register(http, "candidate-outsider@t.dev")
    denied = await http.get("/api/agents/capability-candidates", params=params, headers=outsider)
    assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["created", "running", "awaiting_permission", "cancelling"])
async def test_delete_agent_blocks_active_tasks(client_and_app, auth_headers, db_session, status):
    from workmate_product.persistence.models import Task
    http, _ = client_and_app
    agent = (await http.post("/api/agents", json={
        "slug": "busy-delete", "display_name": "Busy",
    }, headers=auth_headers)).json()
    conv = (await http.post("/api/conversations", json={
        "agent_id": agent["id"],
    }, headers=auth_headers)).json()
    task = Task(id=str(uuid.uuid4()), conversation_id=conv["id"], title="Busy", status=status)
    db_session.add(task)
    await db_session.commit()
    response = await http.delete(f"/api/agents/{agent['id']}", headers=auth_headers)
    assert response.status_code == 409
    task.status = "completed"
    await db_session.commit()
    assert (await http.delete(f"/api/agents/{agent['id']}", headers=auth_headers)).status_code == 204


@pytest.mark.asyncio
async def test_agent_manager_can_edit_without_use_and_cannot_expand_public_scope(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    created = await http.post('/api/agents', headers=auth_headers, json={
        'slug': 'management-without-use', 'display_name': 'Managed',
        'scope_id': root.id, 'visibility': 'public', 'include_descendants': False,
    })
    assert created.status_code == 201
    assert created.json()['actions']['use'] is False
    manager, manager_id = await _register(http, 'view-only-manager@test.dev')
    role = await _role_with_permission(db_session, 'separate-agent-use-update', AGENT_UPDATE)
    db_session.add(RoleBinding(user_id=manager_id, role_id=role.id,
                               scope_id=root.id, include_descendants=False))
    await db_session.commit()
    path = f"/api/agents/{created.json()['id']}"
    viewed = await http.get(path, headers=manager)
    assert viewed.status_code == 200
    assert viewed.json()['actions']['use'] is False
    assert viewed.json()['actions']['update'] is True
    listed = await http.get('/api/agents', headers=manager)
    assert next(item for item in listed.json()['items'] if item['id'] == created.json()['id'])['actions']['use'] is False
    renamed = await http.patch(path, headers=manager, json={'display_name': 'Updated'})
    assert renamed.status_code == 200
    assert renamed.json()['actions']['use'] is False
    expanded = await http.patch(path, headers=manager, json={'include_descendants': True})
    assert expanded.status_code == 403
    assert 'agent.publish' in expanded.json()['detail']
