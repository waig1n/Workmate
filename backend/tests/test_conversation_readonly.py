# -*- coding: utf-8 -*-
"""Revocation semantics tests (RBAC design §10): history stays
readable, new tasks are forbidden."""

import uuid

import pytest
from sqlalchemy import delete, func, select

from workmate_product.persistence.models import (
    Conversation,
    ResourceShare,
    ScopeKind,
    ScopeMembership,
    Task,
    User,
)
from app.services.bootstrap import create_scope, get_root_scope


SKILL_CARD = {
    "hub_id": "clawhub",
    "id": "revocable-skill-card",
    "name": "revocable-skill",
    "display_name": "Revocable Skill",
    "description": "",
    "tags": [],
    "version": "1.0.0",
}


async def _personal_agent_with_revoked_department_skill(
    http, auth_headers, fake_client, db,
) -> tuple[str, str, str, str]:
    fake_client.skill_catalog = [SKILL_CARD]
    me = await http.get("/api/auth/me", headers=auth_headers)
    user_id = me.json()["id"]
    root = await get_root_scope(db)
    department = await create_scope(
        db, kind=ScopeKind.DEPARTMENT, name="Revocable department", parent=root,
    )
    db.add(ScopeMembership(user_id=user_id, scope_id=department.id))
    await db.commit()

    skill = await http.post("/api/skills", json={
        "hub_id": "clawhub",
        "card_id": SKILL_CARD["id"],
        "scope_id": root.id,
    }, headers=auth_headers)
    assert skill.status_code == 201, skill.text
    capability_id = skill.json()["id"]
    db.add(ResourceShare(
        id=str(uuid.uuid4()), capability_id=capability_id,
        target_scope_id=department.id, include_descendants=False,
    ))
    await db.commit()

    agent = await http.post("/api/agents", json={
        "slug": "revocable-personal-agent",
        "display_name": "Revocable Personal Agent",
        "skill_binding_ids": [capability_id],
    }, headers=auth_headers)
    assert agent.status_code == 201, agent.text
    assert agent.json()["unavailable_capability_ids"] == []
    agent_id = agent.json()["id"]

    conversation = await http.post("/api/conversations", json={
        "agent_id": agent_id,
        "title": "Revocable capability conversation",
    }, headers=auth_headers)
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]

    await db.execute(delete(ScopeMembership).where(
        ScopeMembership.user_id == user_id,
        ScopeMembership.scope_id == department.id,
    ))
    await db.commit()
    return user_id, agent_id, capability_id, conversation_id


@pytest.mark.asyncio
async def test_lost_access_makes_conversation_read_only(
    client_and_app, auth_headers, db_session,
):
    """A member who loses visibility of a public org agent keeps their
    conversation history but cannot start new tasks."""
    http, _ = client_and_app
    scopes = await http.get("/api/scopes/accessible", headers=auth_headers)
    root = next(s for s in scopes.json() if s["kind"] == "organization")
    agent = await http.post("/api/agents", json={
        "slug": "shared", "display_name": "Shared",
        "scope_id": root["id"], "visibility": "public",
    }, headers=auth_headers)
    agent_id = agent.json()["id"]
    db_session.add(ResourceShare(
        id=str(uuid.uuid4()), agent_id=agent_id,
        target_scope_id=root["id"], include_descendants=True,
    ))
    await db_session.commit()

    await http.post("/api/auth/register", json={
        "email": "ro@t.dev", "password": "password123", "display_name": "RO",
    })
    login = await http.post("/api/auth/login", json={
        "email": "ro@t.dev", "password": "password123",
    })
    member = {"Authorization": f"Bearer {login.json()['access_token']}"}

    conversation = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "Work chat",
    }, headers=member)
    assert conversation.status_code == 201
    assert conversation.json()["read_only"] is False
    conv_id = conversation.json()["id"]

    # Unpublish: the member loses visibility of the agent.
    unpublish = await http.patch(f"/api/agents/{agent_id}", json={
        "visibility": "private",
    }, headers=auth_headers)
    assert unpublish.status_code == 200

    # History remains readable, flagged read_only.
    fetched = await http.get(f"/api/conversations/{conv_id}", headers=member)
    assert fetched.status_code == 200
    assert fetched.json()["read_only"] is True
    assert fetched.json()["read_only_reason"]
    # History remains available without exposing the Agent's mutable private
    # prompt, bindings, scope or creator after visibility is revoked.
    assert fetched.json()["agent"] is None
    listed = await http.get("/api/conversations", headers=member)
    listed_conversation = next(
        item for item in listed.json() if item["id"] == conv_id
    )
    assert listed_conversation["agent"] is None

    # New conversations on an invisible agent are 404.
    resp = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "Again",
    }, headers=member)
    assert resp.status_code == 404

    # New tasks in the old conversation are 403.
    resp = await http.post(
        f"/api/tasks/conversations/{conv_id}/tasks",
        json={"message": "hello"}, headers=member,
    )
    assert resp.status_code == 403

    # The creator retains visibility and draft trial access after unpublishing.
    agent_view = await http.get(
        f"/api/agents/{agent_id}", headers=auth_headers,
    )
    assert agent_view.status_code == 200
    assert agent_view.json()["actions"]["use"] is True


@pytest.mark.asyncio
async def test_deleted_agent_leaves_history_readable(
    client_and_app, auth_headers,
):
    """Even after the agent is gone, the conversation stays listed with
    its messages readable."""
    http, _ = client_and_app
    agent = await http.post("/api/agents", json={
        "slug": "temp", "display_name": "Temp",
    }, headers=auth_headers)
    agent_id = agent.json()["id"]
    conversation = await http.post("/api/conversations", json={
        "agent_id": agent_id, "title": "Bye",
    }, headers=auth_headers)
    conv_id = conversation.json()["id"]

    # Soft deletion keeps the owner's history accessible but blocks new work.
    resp = await http.delete(f"/api/agents/{agent_id}", headers=auth_headers)
    assert resp.status_code == 204
    history = await http.get(f"/api/conversations/{conv_id}", headers=auth_headers)
    assert history.status_code == 200
    assert history.json()["read_only"] is True
    assert "已删除" in history.json()["read_only_reason"]
    listed = await http.get("/api/conversations", headers=auth_headers)
    assert conv_id in {item["id"] for item in listed.json()}
    messages = await http.get(f"/api/conversations/{conv_id}/messages", headers=auth_headers)
    assert messages.status_code == 200
    denied = await http.post(f"/api/tasks/conversations/{conv_id}/tasks",
                             json={"message": "Continue"}, headers=auth_headers)
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_revoked_capability_degrades_conversation_and_tasks_until_unbound(
    client_and_app, auth_headers, fake_client, db_session,
):
    """Stale personal bindings are visible, block work, and recover on unbind."""
    http, _ = client_and_app
    _, agent_id, capability_id, conversation_id = (
        await _personal_agent_with_revoked_department_skill(
            http, auth_headers, fake_client, db_session,
        )
    )

    agent = await http.get(f"/api/agents/{agent_id}", headers=auth_headers)
    assert agent.status_code == 200, agent.text
    assert agent.json()["unavailable_capability_ids"] == [capability_id]

    conversation = await http.get(
        f"/api/conversations/{conversation_id}", headers=auth_headers,
    )
    assert conversation.status_code == 200, conversation.text
    assert conversation.json()["read_only"] is True
    assert conversation.json()["agent"]["actions"]["use"] is True
    reason = conversation.json()["read_only_reason"]
    assert "能力" in reason
    assert reason != "你已失去该智能体的使用权限，历史内容仍可查看"

    blocked = await http.post(
        f"/api/tasks/conversations/{conversation_id}/tasks",
        json={"message": "must not run"}, headers=auth_headers,
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == {
        "code": "unavailable_capabilities",
        "capability_ids": [capability_id],
        "message": "智能体存在当前不可用的能力，请先解绑后再试",
    }
    assert await db_session.scalar(select(func.count(Task.id))) == 0

    unbound = await http.patch(
        f"/api/agents/{agent_id}",
        json={"skill_binding_ids": []}, headers=auth_headers,
    )
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["unavailable_capability_ids"] == []

    recovered = await http.get(
        f"/api/conversations/{conversation_id}", headers=auth_headers,
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["read_only"] is False
    assert recovered.json()["read_only_reason"] is None
