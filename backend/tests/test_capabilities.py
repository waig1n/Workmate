# -*- coding: utf-8 -*-
"""Organization capability registry tests."""

import uuid
from product_catalog_fakes import skill_archive

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapter.errors import RuntimeError as RuntimeHTTPError
from app.auth.permissions import ROLE_ADMIN
from workmate_product.persistence.models import (
    CapabilityInstallation,
    SecurityAuditEvent,
    CapabilityType,
    ResourceShare,
    Role,
    RoleBinding,
    Scope,
    ScopeKind,
    ScopeMembership,
)
from app.services import capabilities as caps
from app.services.bootstrap import create_scope, get_root_scope


SKILL_CARD = {
    "hub_id": "clawhub", "id": "c-1", "name": "deep-research",
    "display_name": "Deep Research", "description": "Research workflow",
    "tags": ["research"], "version": "1.0.0",
}


@pytest.mark.asyncio
async def test_skillhub_install_requires_confirmation_and_permission(
    client_and_app, auth_headers, fake_client, db_session,
):
    http, _ = client_and_app
    root, _ = await _organization_scopes(db_session)
    fake_client.skill_catalog = [{**SKILL_CARD, "hub_id": "skillhub-cn"}]
    payload = {"hub_id": "skillhub-cn", "card_id": "c-1", "scope_id": root.id}
    response = await http.post("/api/skills", json=payload, headers=auth_headers)
    assert response.status_code == 409
    member, _ = await _register(http, "skill-confirm-member@test.example")
    response = await http.post("/api/skills", json={**payload, "confirmed": True}, headers=member)
    assert response.status_code == 403
    response = await http.post("/api/skills", json={**payload, "confirmed": True}, headers=auth_headers)
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_skill_description_checks_visibility_and_returns_only_content(
    client_and_app, auth_headers, fake_client, db_session, monkeypatch,
):
    http, _ = client_and_app
    root, _ = await _organization_scopes(db_session)
    fake_client.skill_catalog = [SKILL_CARD]
    response = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "c-1", "scope_id": root.id,
    }, headers=auth_headers)
    assert response.status_code == 201
    installation = response.json()
    response = await http.get(f"/api/skills/{installation['id']}/description", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()['description'] == SKILL_CARD['description']
    assert 'Fixture instructions' in response.json()['markdown']
    assert set(response.json()) == {'description', 'markdown'}
    response = await http.get("/api/skills/missing/description", headers=auth_headers)
    assert response.status_code == 404

MCP_CARD = {
    "hub_id": "github", "id": "m-card", "name": "search",
    "display_name": "Search", "description": "Search MCP", "tags": [],
    "is_stateful": False,
    "inputs_schema": {"type": "object", "properties": {}},
}


async def _register(http, email: str) -> tuple[dict[str, str], str]:
    registered = await http.post("/api/auth/register", json={
        "email": email, "password": "password123", "display_name": email,
    })
    assert registered.status_code == 200, registered.text
    headers = {
        "Authorization": f"Bearer {registered.json()['access_token']}",
    }
    me = await http.get("/api/auth/me", headers=headers)
    return headers, me.json()["id"]


async def _organization_scopes(db_session):
    root = await get_root_scope(db_session)
    personal = (await db_session.scalars(
        select(Scope).where(Scope.kind == ScopeKind.PERSONAL),
    )).first()
    assert root is not None and personal is not None
    return root, personal


async def _grant_capability_use(db_session, capability_id, scope_id):
    """Fixture authorization is explicit; installation grants no use rights."""
    db_session.add(ResourceShare(id=str(uuid.uuid4()), capability_id=capability_id,
                                 target_scope_id=scope_id, include_descendants=False))
    await db_session.commit()


@pytest.mark.asyncio
async def test_skill_defaults_root_mcp_requires_scope_and_personal_is_rejected(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Removing organization-only validation would install into Personal."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD]
    fake_client.mcp_catalog = [MCP_CARD]
    _, personal = await _organization_scopes(db_session)

    for path, payload in [
        ("/api/skills", {"hub_id": "clawhub", "card_id": "c-1"}),
        ("/api/mcps", {
            "hub_id": "github", "card_id": "m-card", "values": {},
        }),
    ]:
        missing = await http.post(path, json=payload, headers=auth_headers)
        assert missing.status_code == (201 if path == "/api/skills" else 422), missing.text
        rejected = await http.post(
            path, json={**payload, "scope_id": personal.id},
            headers=auth_headers,
        )
        assert rejected.status_code == 422, rejected.text


@pytest.mark.asyncio
async def test_skill_upload_requires_manageable_organization_scope(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Upload must not retain the old implicit Personal installation path."""
    http, _ = client_and_app
    root, personal = await _organization_scopes(db_session)

    missing = await http.post(
        "/api/skills/upload",
        files={"archive": ("skill.zip", skill_archive(), "application/zip")},
        headers=auth_headers,
    )
    assert missing.status_code == 201, missing.text
    assert missing.json()["scope_id"] == root.id
    rejected = await http.post(
        "/api/skills/upload", data={"scope_id": personal.id},
        files={"archive": ("skill.zip", skill_archive(), "application/zip")},
        headers=auth_headers,
    )
    assert rejected.status_code == 422, rejected.text

    uploaded = missing
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["scope_id"] == root.id
    assert uploaded.json()["manageable"] is True
    assert uploaded.json()["usable"] is False
    assert uploaded.json()["metadata"]["hub_id"] == "uploaded"
    assert uploaded.json()["metadata"]["creation_channel"] == "upload"
    member, _ = await _register(http, "upload-private@test.example")
    assert (await http.get("/api/skills", headers=member)).json()['items'] == []
    assert list((await db_session.scalars(select(ResourceShare))).all()) == []


@pytest.mark.asyncio
async def test_member_catalog_is_read_only(
    client_and_app, auth_headers, fake_client, db_session,
):
    """An ordinary member sees sanitized metadata but cannot mutate it."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD]
    fake_client.mcp_catalog = [MCP_CARD]
    root, _ = await _organization_scopes(db_session)

    skill = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "c-1", "scope_id": root.id,
    }, headers=auth_headers)
    mcp = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card", "scope_id": root.id,
        "values": {"api_key": "super-secret"},
    }, headers=auth_headers)
    assert skill.status_code == mcp.status_code == 201

    member, _ = await _register(http, "catalog-member@t.dev")
    listed_skills = await http.get("/api/skills", headers=member)
    assert listed_skills.json()['items'] == []
    assert skill.json()["usable"] is False
    assert mcp.json()["usable"] is False
    assert (await http.get("/api/mcps", headers=member)).json()['items'] == []
    await _grant_capability_use(db_session, mcp.json()["id"], root.id)
    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), capability_id=skill.json()["id"],
        target_scope_id=root.id, include_descendants=True,
    ))
    await db_session.commit()
    listed_skills = await http.get("/api/skills", headers=member)
    listed_mcps = await http.get("/api/mcps", headers=member)
    assert listed_skills.json()['items'] == [{**skill.json(), "manageable": False, "usable": True}]
    assert listed_mcps.json()['items'] == [{**mcp.json(), "manageable": False, "usable": True}]
    assert "values" not in listed_mcps.json()['items'][0]

    install = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "c-1", "scope_id": root.id,
        "name": "member-copy",
    }, headers=member)
    upload = await http.post(
        "/api/skills/upload", data={"scope_id": root.id},
        files={"archive": ("skill.zip", skill_archive(), "application/zip")},
        headers=member,
    )
    update = await http.patch(
        f"/api/mcps/{mcp.json()['id']}",
        json={"name": "member-rename"}, headers=member,
    )
    delete = await http.delete(
        f"/api/skills/{skill.json()['id']}", headers=member,
    )
    assert [install.status_code, upload.status_code, update.status_code,
            delete.status_code] == [403, 403, 403, 403]


@pytest.mark.asyncio
async def test_manage_role_can_see_and_mutate_without_target_membership(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Capability availability and manage permission are independent facts."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD]
    root = await get_root_scope(db_session)

    installed = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "c-1",
        "scope_id": root.id,
    }, headers=auth_headers)
    assert installed.status_code == 201, installed.text

    manager, manager_id = await _register(http, "cap-manager@t.dev")
    membership = await db_session.get(ScopeMembership, (root.id, manager_id))
    assert membership is not None
    await db_session.delete(membership)
    admin_role = await db_session.scalar(
        select(Role).where(Role.key == ROLE_ADMIN),
    )
    db_session.add(RoleBinding(
        user_id=manager_id, role_id=admin_role.id, scope_id=root.id,
        include_descendants=False,
    ))
    await db_session.commit()
    assert await db_session.get(
        ScopeMembership, (root.id, manager_id),
    ) is None

    listed = await http.get("/api/skills", headers=manager)
    assert [item["id"] for item in listed.json()['items']] == [installed.json()["id"]]
    assert listed.json()['items'][0]["manageable"] is True
    assert listed.json()['items'][0]["usable"] is False
    disabled = await http.patch(
        f"/api/skills/{installed.json()['id']}",
        json={"enabled": False}, headers=manager,
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False


@pytest.mark.asyncio
async def test_mcp_values_never_returned(
    client_and_app, auth_headers, fake_client, db_session,
):
    http, _ = client_and_app
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    installed = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card", "scope_id": root.id,
        "values": {"api_key": "super-secret"},
    }, headers=auth_headers)
    assert installed.status_code == 201
    assert "values" not in installed.json()
    listed = await http.get("/api/mcps", headers=auth_headers)
    assert "values" not in listed.json()['items'][0]


@pytest.mark.asyncio
async def test_rename_keeps_stable_id(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Renaming an MCP must not disturb an Agent binding by stable ID."""
    http, _ = client_and_app
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    installed = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card",
        "scope_id": root.id, "values": {},
    }, headers=auth_headers)
    installation_id = installed.json()["id"]
    await _grant_capability_use(db_session, installation_id, root.id)
    agent = await http.post("/api/agents", json={
        "slug": "binder", "display_name": "Binder",
        "mcp_binding_ids": [installation_id],
    }, headers=auth_headers)
    assert agent.status_code == 201, agent.text

    renamed = await http.patch(f"/api/mcps/{installation_id}", json={
        "name": "search2",
    }, headers=auth_headers)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == installation_id
    fetched = await http.get(
        f"/api/agents/{agent.json()['id']}", headers=auth_headers,
    )
    assert fetched.json()["mcp_binding_ids"] == [installation_id]


@pytest.mark.asyncio
async def test_disable_bound_mcp_409(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Disabling a bound capability is blocked until its binding is removed."""
    http, _ = client_and_app
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    installed = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card",
        "scope_id": root.id, "values": {},
    }, headers=auth_headers)
    installation_id = installed.json()["id"]
    await _grant_capability_use(db_session, installation_id, root.id)
    agent = await http.post("/api/agents", json={
        "slug": "bound", "display_name": "Bound",
        "mcp_binding_ids": [installation_id],
    }, headers=auth_headers)
    assert agent.status_code == 201

    disabled = await http.patch(f"/api/mcps/{installation_id}", json={
        "enabled": False,
    }, headers=auth_headers)
    assert disabled.status_code == 409
    await http.patch(f"/api/agents/{agent.json()['id']}", json={
        "mcp_binding_ids": [],
    }, headers=auth_headers)
    disabled = await http.patch(f"/api/mcps/{installation_id}", json={
        "enabled": False,
    }, headers=auth_headers)
    assert disabled.status_code == 200


@pytest.mark.asyncio
async def test_capability_mutations_emit_sanitized_audit_events(
    client_and_app, auth_headers, fake_client, db_session, caplog,
):
    http, _ = client_and_app
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    caplog.set_level("INFO", logger="app.services.capabilities")

    installed = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card",
        "scope_id": root.id, "values": {"api_key": "never-log-me"},
    }, headers=auth_headers)
    assert installed.status_code == 201, installed.text
    installation_id = installed.json()["id"]
    updated = await http.patch(f"/api/mcps/{installation_id}", json={
        "name": "audited-search",
        "values": {"api_key": "rotated-never-log-me"},
        "enabled": False,
    }, headers=auth_headers)
    assert updated.status_code == 200, updated.text
    deleted = await http.delete(
        f"/api/mcps/{installation_id}", headers=auth_headers,
    )
    assert deleted.status_code == 204, deleted.text

    import json
    events = (await db_session.scalars(select(SecurityAuditEvent).where(
        SecurityAuditEvent.event_type.in_(['capability.install', 'capability.update', 'capability.delete'])))).all()
    assert {event.event_type for event in events} == {'capability.install', 'capability.update', 'capability.delete'}
    assert all(event.metadata_['capability_id'] == installation_id for event in events)
    actor = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    assert all(event.actor_user_id == actor for event in events)
    update = next(event for event in events if event.event_type == 'capability.update')
    assert update.metadata_['enabled'] is False
    audit = json.dumps([event.metadata_ for event in events])
    assert "never-log-me" not in audit
    assert "rotated-never-log-me" not in audit


@pytest.mark.asyncio
async def test_agent_unbind_persists_agent_update_audit(
    client_and_app, auth_headers, fake_client, db_session, caplog,
):
    http, _ = client_and_app
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    installed = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card",
        "scope_id": root.id, "values": {},
    }, headers=auth_headers)
    capability_id = installed.json()["id"]
    await _grant_capability_use(db_session, capability_id, root.id)
    agent = await http.post("/api/agents", json={
        "slug": "audit-unbind", "display_name": "Audit unbind",
        "mcp_binding_ids": [capability_id],
    }, headers=auth_headers)
    caplog.clear()
    caplog.set_level("INFO", logger="app.api.routers.agents")

    unbound = await http.patch(f"/api/agents/{agent.json()['id']}", json={
        "mcp_binding_ids": [],
    }, headers=auth_headers)
    assert unbound.status_code == 200, unbound.text
    events = (await db_session.scalars(select(SecurityAuditEvent).where(
        SecurityAuditEvent.event_type == 'agent.update'))).all()
    assert len(events) == 1
    assert events[0].metadata_['agent_id'] == agent.json()['id']
    assert events[0].actor_user_id == (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    assert unbound.json()['mcp_binding_ids'] == []


@pytest.mark.asyncio
async def test_agent_rejects_same_type_same_name_capability_bindings(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Distinct same-name installations must not enter one Agent workspace."""
    http, _ = client_and_app
    fake_client.skill_catalog = [SKILL_CARD]
    root = await get_root_scope(db_session)
    installed = await http.post("/api/skills", json={
        "hub_id": "clawhub", "card_id": "c-1", "scope_id": root.id,
    }, headers=auth_headers)
    assert installed.status_code == 201, installed.text
    first = installed.json()
    # Organization centralization preserves historical same-name records.
    # New API installs reject duplicates, so seed that supported stored state.
    duplicate = CapabilityInstallation(
        id=str(uuid.uuid4()), capability_type=CapabilityType.SKILL,
        scope_id=root.id, name=first["name"], enabled=True,
    )
    db_session.add(duplicate)
    await db_session.commit()
    assert first["id"] != duplicate.id
    await _grant_capability_use(db_session, first["id"], root.id)
    await _grant_capability_use(db_session, duplicate.id, root.id)

    response = await http.post("/api/agents", json={
        "slug": "ambiguous-name", "display_name": "Ambiguous name",
        "skill_binding_ids": [first["id"], duplicate.id],
    }, headers=auth_headers)

    assert response.status_code == 422
    assert "Multiple skill capabilities with the same name" in response.json()["detail"]


@pytest.mark.asyncio
async def test_public_agent_create_and_delete_persist_audit_history(
    client_and_app, auth_headers, fake_client, db_session, caplog,
):
    http, _ = client_and_app
    fake_client.mcp_catalog = [MCP_CARD]
    root = await get_root_scope(db_session)
    installed = await http.post("/api/mcps", json={
        "hub_id": "github", "card_id": "m-card",
        "scope_id": root.id, "values": {},
    }, headers=auth_headers)
    capability_id = installed.json()["id"]
    await _grant_capability_use(db_session, capability_id, root.id)
    caplog.clear()
    caplog.set_level("INFO", logger="app.api.routers.agents")

    agent = await http.post("/api/agents", json={
        "slug": "published-audit", "display_name": "Published audit",
        "scope_id": root.id, "visibility": "public",
        "mcp_binding_ids": [capability_id],
    }, headers=auth_headers)
    assert agent.status_code == 201, agent.text
    deleted = await http.delete(
        f"/api/agents/{agent.json()['id']}", headers=auth_headers,
    )
    assert deleted.status_code == 204, deleted.text

    events = (await db_session.scalars(select(SecurityAuditEvent).where(
        SecurityAuditEvent.event_type.in_(['agent.create', 'agent.delete'])))).all()
    assert {event.event_type for event in events} == {'agent.create', 'agent.delete'}
    assert all(event.metadata_['agent_id'] == agent.json()['id'] for event in events)
    actor = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    assert all(event.actor_user_id == actor for event in events)
