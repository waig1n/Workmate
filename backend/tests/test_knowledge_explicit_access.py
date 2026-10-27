"""Scope grants must govern both callers and Agent binding scopes."""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config_file import KnowledgeFileConfig
from workmate_product.persistence.models import (
    Agent,
    ResourceShare,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
    User,
)
from workmate_product.persistence.models import KnowledgeBase
from app.services.knowledge import KnowledgeService
from workmate_product.application.knowledge_access import KnowledgeAccess


async def test_explicit_knowledge_grants_are_dynamic_and_revocable(
    client_and_app, auth_headers, db_session, monkeypatch
):
    monkeypatch.setattr("app.services.knowledge.knowledge_settings", lambda: KnowledgeFileConfig(enabled=True))
    db = db_session
    root = await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION))
    user = await db.scalar(select(User))
    for sid, parent in (("knowledge-a", root.id), ("knowledge-b", root.id), ("knowledge-child", "knowledge-b")):
        db.add(Scope(id=sid, kind=ScopeKind.DEPARTMENT, parent_id=parent, name=sid))
        await db.flush()
        db.add(ScopeClosure(ancestor_id=sid, descendant_id=sid, depth=0))
        db.add(ScopeClosure(ancestor_id=root.id, descendant_id=sid, depth=1 if parent == root.id else 2))
    db.add(ScopeClosure(ancestor_id="knowledge-b", descendant_id="knowledge-child", depth=1))
    db.add(ScopeMembership(scope_id="knowledge-b", user_id=user.id))
    kb = KnowledgeBase(id=str(uuid.uuid4()), scope_id="knowledge-a", name="Library", runtime_id="runtime-explicit", runtime_owner_id="scope:knowledge-a", created_by=user.id)
    db.add(kb)
    await db.commit()
    access = KnowledgeAccess(db)
    service = KnowledgeService(db, user.id)
    assert kb.id not in await service.usable_ids()
    grant = ResourceShare(id=str(uuid.uuid4()), knowledge_base_id=kb.id, target_scope_id="knowledge-b")
    db.add(grant)
    await db.commit()
    assert kb.id in await service.usable_ids()
    assert kb.id not in await service.downloadable_ids()
    matrix = await access.ids_by_scope({"knowledge-a", "knowledge-b", "knowledge-child"})
    assert matrix == {"knowledge-a": set(), "knowledge-b": {kb.id}, "knowledge-child": set()}
    scope = await db.get(Scope, "knowledge-b")
    agent = Agent(scope_id=scope.id, knowledge_config={"knowledge_base_ids": [kb.id]})
    assert await service.validate_bindings(agent, agent.knowledge_config) == [kb]
    await db.delete(grant)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await service.validate_bindings(agent, agent.knowledge_config)
    assert exc.value.status_code == 422
    db.add(ResourceShare(id=str(uuid.uuid4()), knowledge_base_id=kb.id, target_scope_id=root.id, include_descendants=True, allow_download=True))
    await db.commit()
    assert await access.ids_for_scopes({"knowledge-child"}) == {kb.id}
    assert kb.id in await service.downloadable_ids()
    kb.enabled = False
    await db.commit()
    with pytest.raises(HTTPException):
        await service.get(kb.id, use=True)


async def test_shared_library_visible_without_source_membership_or_management(
    client_and_app, auth_headers, db_session
):
    http, app = client_and_app
    db = db_session
    root = await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION))
    for sid in ("kb-source", "kb-recipient"):
        db.add(Scope(id=sid, kind=ScopeKind.DEPARTMENT, parent_id=root.id, name=sid))
        await db.flush()
        db.add(ScopeClosure(ancestor_id=sid, descendant_id=sid, depth=0))
        db.add(ScopeClosure(ancestor_id=root.id, descendant_id=sid, depth=1))
    await db.commit()
    registered = await http.post("/api/auth/register", json={
        "email": "cross-kb@example.com", "password": "password123", "display_name": "Reader",
    })
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    reader = await db.scalar(select(User).where(User.email == "cross-kb@example.com"))
    db.add(ScopeMembership(scope_id="kb-recipient", user_id=reader.id))
    kb = KnowledgeBase(id=str(uuid.uuid4()), scope_id="kb-source", name="Shared", runtime_id="cross-runtime", runtime_owner_id="scope:kb-source")
    db.add(kb)
    await db.commit()
    assert (await http.get("/api/knowledge-bases", headers=headers)).json()['items'] == []
    db.add(ResourceShare(id=str(uuid.uuid4()), knowledge_base_id=kb.id, target_scope_id="kb-recipient"))
    await db.commit()
    listed = (await http.get("/api/knowledge-bases", headers=headers)).json()['items']
    assert len(listed) == 1 and listed[0]["id"] == kb.id
    assert listed[0]["actions"] == {"manage": False, "use": True, "download": False}
    candidates = await http.get("/api/knowledge-bases?agent_scope_id=kb-recipient", headers=headers)
    assert [item["id"] for item in candidates.json()['items']] == [kb.id]
    assert (await http.get(f"/api/knowledge-bases/{kb.id}/documents/doc/content", headers=headers)).status_code == 403


async def test_agent_manager_can_bind_scope_granted_library_without_personal_use(
    client_and_app, auth_headers, db_session, monkeypatch,
):
    """Agent configuration follows the target Scope grant, while execution
    continues to require the current user's own retrieval grant."""
    http, _ = client_and_app
    db = db_session
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    root = await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION))
    user = await db.scalar(select(User))
    for sid in ("kb-managed-source", "kb-agent-target"):
        db.add(Scope(id=sid, kind=ScopeKind.DEPARTMENT, parent_id=root.id, name=sid))
        await db.flush()
        db.add(ScopeClosure(ancestor_id=sid, descendant_id=sid, depth=0))
        db.add(ScopeClosure(ancestor_id=root.id, descendant_id=sid, depth=1))
    kb = KnowledgeBase(
        id=str(uuid.uuid4()), scope_id="kb-managed-source", name="Target authorized",
        runtime_id="target-authorized", runtime_owner_id="scope:kb-managed-source",
    )
    db.add(kb)
    await db.flush()
    db.add(ResourceShare(
        id=str(uuid.uuid4()), knowledge_base_id=kb.id,
        target_scope_id="kb-agent-target",
    ))
    await db.commit()

    service = KnowledgeService(db, user.id)
    assert kb.id not in await service.usable_ids()
    candidates = await http.get(
        "/api/knowledge-bases?agent_scope_id=kb-agent-target",
        headers=auth_headers,
    )
    assert candidates.status_code == 200, candidates.text
    assert [item["id"] for item in candidates.json()['items']] == [kb.id]
    assert candidates.json()['items'][0]["actions"]["use"] is False

    created = await http.post("/api/agents", headers=auth_headers, json={
        "slug": "target-scope-kb", "display_name": "Target Scope KB",
        "scope_id": "kb-agent-target",
        "knowledge_config": {"knowledge_base_ids": [kb.id]},
    })
    assert created.status_code == 201, created.text
    assert created.json()["knowledge_config"]["knowledge_base_ids"] == [kb.id]
    with pytest.raises(HTTPException):
        await service.validate_bindings(
            Agent(scope_id="kb-agent-target"),
            {"knowledge_base_ids": [kb.id]},
        )
