"""Knowledge product boundary: defaults, authorization and lifecycle."""

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api.knowledge_schemas import KnowledgeConfig
from app.config_file import KnowledgeFileConfig


def test_knowledge_configuration_is_opt_in_and_strict():
    assert KnowledgeConfig().knowledge_base_ids == []
    assert KnowledgeConfig().parameters.mode == "agentic"
    assert KnowledgeFileConfig().enabled is False
    for value in (
        {"parameters": {"mode": "all"}},
        {"parameters": {"top_k": 0}},
        {"knowledge_base_ids": ["x", "x"]},
    ):
        with pytest.raises(ValidationError):
            KnowledgeConfig.model_validate(value)
    with pytest.raises(ValidationError):
        KnowledgeFileConfig(chunk_size=10, chunk_overlap=10)


async def test_knowledge_crud_binding_and_dependency_failure(
    client_and_app, auth_headers, monkeypatch
):
    http, app = client_and_app
    runtime = app.state.runtime_client
    runtime.knowledge_request = AsyncMock(return_value={"id": "runtime-kb"})
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    created = await http.post(
        "/api/knowledge-bases", headers=auth_headers, json={"name": "Policy library"}
    )
    assert created.status_code == 201, created.text
    kb = created.json()
    assert kb["actions"] == {"manage": True, "use": True, "download": True}
    agent = await http.post(
        "/api/agents",
        headers=auth_headers,
        json={
            "slug": "rag-helper",
            "display_name": "Knowledge assistant",
            "knowledge_config": {"knowledge_base_ids": [kb["id"]]},
        },
    )
    assert agent.status_code == 201, agent.text
    assert agent.json()["knowledge_config"]["parameters"]["mode"] == "agentic"
    blocked = await http.delete(
        "/api/knowledge-bases/" + kb["id"], headers=auth_headers
    )
    assert blocked.status_code == 409
    patched = await http.patch(
        "/api/agents/" + agent.json()["id"],
        headers=auth_headers,
        json={"knowledge_config": {"knowledge_base_ids": []}},
    )
    assert patched.status_code == 200
    assert patched.json()["knowledge_config"]["knowledge_base_ids"] == []
    assert (
        await http.delete("/api/knowledge-bases/" + kb["id"], headers=auth_headers)
    ).status_code == 204


async def test_other_users_personal_library_is_invisible(
    client_and_app, auth_headers, monkeypatch
):
    http, app = client_and_app
    app.state.runtime_client.knowledge_request = AsyncMock(
        return_value={"id": "private-runtime-kb"}
    )
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    kb = (
        await http.post(
            "/api/knowledge-bases", headers=auth_headers, json={"name": "Private documents"}
        )
    ).json()
    registration = await http.post(
        "/api/auth/register",
        json={
            "email": "rag-other@example.com",
            "password": "password123",
            "display_name": "Other",
        },
    )
    headers = {"Authorization": "Bearer " + registration.json()["access_token"]}
    assert (await http.get("/api/knowledge-bases", headers=headers)).json()['items'] == []
    assert (
        await http.get(f"/api/knowledge-bases/{kb['id']}/documents", headers=headers)
    ).status_code == 404
    response = await http.post(
        "/api/agents",
        headers=headers,
        json={
            "slug": "thief-agent",
            "display_name": "Other",
            "knowledge_config": {"knowledge_base_ids": [kb["id"]]},
        },
    )
    assert response.status_code == 422


async def test_delete_recovers_after_runtime_already_deleted(
    client_and_app, auth_headers, monkeypatch
):
    from app.adapter.errors import RuntimeError

    http, app = client_and_app
    remote = AsyncMock(return_value={"id": "gone-runtime"})
    app.state.runtime_client.knowledge_request = remote
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    kb = (
        await http.post(
            "/api/knowledge-bases", headers=auth_headers, json={"name": "Deletion recovery"}
        )
    ).json()
    remote.side_effect = RuntimeError(404, "already removed")
    assert (
        await http.delete("/api/knowledge-bases/" + kb["id"], headers=auth_headers)
    ).status_code == 204
    assert (await http.get("/api/knowledge-bases", headers=auth_headers)).json()['items'] == []


async def test_upload_limits_and_search_failure_are_explicit(
    client_and_app, auth_headers, monkeypatch
):
    from app.adapter.errors import RuntimeError

    http, app = client_and_app
    remote = AsyncMock(return_value={"id": "docs-runtime"})
    app.state.runtime_client.knowledge_request = remote
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    monkeypatch.setattr(
        "app.api.routers.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True, max_upload_bytes=10),
    )
    kb = (
        await http.post(
            "/api/knowledge-bases", headers=auth_headers, json={"name": "Documents"}
        )
    ).json()
    url = "/api/knowledge-bases/" + kb["id"]
    for name, content, status in [
        ("bad.html", b"abc", 422),
        ("empty.txt", b"", 422),
        ("large.txt", b"12345678901", 413),
    ]:
        assert (
            await http.post(
                url + "/documents",
                headers=auth_headers,
                files={"file": (name, content)},
            )
        ).status_code == status
    remote.side_effect = RuntimeError(500, "secret backend address")
    response = await http.post(
        url + "/search", headers=auth_headers, json={"query": "test"}
    )
    assert response.status_code == 502
    assert "secret" not in response.text


async def test_document_routes_scope_candidates_and_disabled_binding(
    client_and_app, auth_headers, monkeypatch
):
    from app.db import get_sessionmaker
    from workmate_product.persistence.models import Agent
    from app.services.knowledge import KnowledgeService

    http, app = client_and_app
    runtime = app.state.runtime_client
    runtime.knowledge_request = AsyncMock(return_value={"id": "document-runtime"})
    runtime.knowledge_upload = AsyncMock(
        side_effect=lambda owner,kb,name,mime,content,doc: {"id": doc, "status": "uploaded"}
    )
    runtime.knowledge_content = AsyncMock(
        return_value=(b"hello", "text/plain", "attachment")
    )
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    kb = (
        await http.post(
            "/api/knowledge-bases", headers=auth_headers, json={"name": "File operations"}
        )
    ).json()
    url = "/api/knowledge-bases/" + kb["id"]
    uploaded = await http.post(
        url + "/documents",
        headers=auth_headers,
        files={"file": ("a.md", b"# hello", "application/octet-stream")},
    )
    assert uploaded.status_code == 202
    assert runtime.knowledge_upload.call_args.args[3] == "text/markdown"
    doc_id = uploaded.json()["id"]
    runtime.knowledge_request.return_value = {"items": [{"id": doc_id}], "total": 1}
    assert (await http.get(url + "/documents", headers=auth_headers)).json()[
        "total"
    ] == 1
    assert (
        await http.post(url + f"/documents/{doc_id}/retry", headers=auth_headers)
    ).status_code == 202
    downloaded = await http.get(url + f"/documents/{doc_id}/content", headers=auth_headers)
    assert downloaded.content == b"hello"
    assert downloaded.headers["cache-control"] == "no-store"
    assert (
        await http.delete(url + f"/documents/{doc_id}", headers=auth_headers)
    ).status_code == 204
    assert (
        await http.patch(
            url, headers=auth_headers, json={"name": "Renamed", "description": "Test"}
        )
    ).json()["name"] == "Renamed"
    candidates = await http.get(
        "/api/knowledge-bases?agent_scope_id=" + kb["scope_id"], headers=auth_headers
    )
    assert candidates.json()['items'][0]["id"] == kb["id"]
    runtime.knowledge_request.return_value = {}
    agent = (
        await http.post(
            "/api/agents",
            headers=auth_headers,
            json={
                "slug": "kb-static",
                "display_name": "Questions and answers",
                "knowledge_config": {
                    "knowledge_base_ids": [kb["id"]],
                    "parameters": {"mode": "static"},
                },
            },
        )
    ).json()
    async with get_sessionmaker()() as db:
        row = await db.get(Agent, agent["id"])
        service = KnowledgeService(db, row.created_by)
        bindings = await service.validate_bindings(row, row.knowledge_config)
        assert row.knowledge_config["parameters"]["mode"] == "static"
        assert bindings[0].runtime_id == "document-runtime"
    assert (
        await http.patch(url, headers=auth_headers, json={"enabled": False})
    ).status_code == 200
    state = (await http.get("/api/agents/" + agent["id"], headers=auth_headers)).json()
    assert state["unavailable_knowledge_base_ids"] == [kb["id"]]
    assert (
        await http.get(url + f"/documents/{doc_id}/content", headers=auth_headers)
    ).status_code == 403


async def test_department_isolation_and_management_does_not_grant_use(
    client_and_app, auth_headers, db_session, monkeypatch
):
    from sqlalchemy import select
    from workmate_product.persistence.models import (
        Scope,
        ScopeKind,
        ScopeMembership,
        Role,
        RoleBinding,
        User,
    )
    from workmate_product.persistence.models import ScopeClosure

    http, app = client_and_app
    remote = AsyncMock(return_value={"id": "department-runtime"})
    app.state.runtime_client.knowledge_request = remote
    monkeypatch.setattr(
        "app.services.knowledge.knowledge_settings",
        lambda: KnowledgeFileConfig(enabled=True),
    )
    root = await db_session.scalar(
        select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION)
    )
    for identifier in ("dept-rag-a", "dept-rag-b"):
        db_session.add(
            Scope(
                id=identifier,
                kind=ScopeKind.DEPARTMENT,
                parent_id=root.id,
                name=identifier,
            )
        )
    await db_session.flush()
    for identifier in ("dept-rag-a", "dept-rag-b"):
        db_session.add(
            ScopeClosure(ancestor_id=identifier, descendant_id=identifier, depth=0)
        )
        db_session.add(
            ScopeClosure(ancestor_id=root.id, descendant_id=identifier, depth=1)
        )
    await db_session.commit()
    kb = (
        await http.post(
            "/api/knowledge-bases",
            headers=auth_headers,
            json={"name": "Department documents", "scope_id": "dept-rag-a"},
        )
    ).json()
    registered = await http.post(
        "/api/auth/register",
        json={
            "email": "dept-reader@example.com",
            "password": "password123",
            "display_name": "Reader",
        },
    )
    headers = {"Authorization": "Bearer " + registered.json()["access_token"]}
    reader = await db_session.scalar(
        select(User).where(User.email == "dept-reader@example.com")
    )
    db_session.add(ScopeMembership(scope_id="dept-rag-b", user_id=reader.id))
    await db_session.commit()
    assert (await http.get("/api/knowledge-bases", headers=headers)).json()['items'] == []
    assert (
        await http.get(f"/api/knowledge-bases/{kb['id']}/documents", headers=headers)
    ).status_code == 404
    admin_role = await db_session.scalar(select(Role).where(Role.key == "admin"))
    db_session.add(
        RoleBinding(
            user_id=reader.id,
            role_id=admin_role.id,
            scope_id="dept-rag-a",
            include_descendants=False,
        )
    )
    await db_session.commit()
    visible = (await http.get("/api/knowledge-bases", headers=headers)).json()['items'][0]
    assert visible["actions"] == {"manage": True, "use": False, "download": False}
    assert (
        await http.post(
            f"/api/knowledge-bases/{kb['id']}/search",
            headers=headers,
            json={"query": "secret"},
        )
    ).status_code == 403
    db_session.add(ScopeMembership(scope_id="dept-rag-a", user_id=reader.id))
    await db_session.commit()
    assert (await http.get("/api/knowledge-bases", headers=headers)).json()['items'][0][
        "actions"
    ]["use"] is False

    # Cross-department grant is visible even without source membership or roles.
    from workmate_product.persistence.models import ResourceShare
    grant = ResourceShare(
        id="kb-cross-department", knowledge_base_id=kb["id"],
        target_scope_id="dept-rag-b", allow_download=False,
    )
    db_session.add(grant)
    await db_session.commit()
    visible = (await http.get("/api/knowledge-bases", headers=headers)).json()['items'][0]
    assert visible["actions"]["use"] is True
    assert visible["actions"]["download"] is False
    content_url = f"/api/knowledge-bases/{kb['id']}/documents/source/content"
    assert (await http.get(content_url, headers=headers)).status_code == 403
    runtime_content = AsyncMock(return_value=(b"source", "text/plain", "attachment"))
    app.state.runtime_client.knowledge_content = runtime_content
    grant.allow_download = True
    await db_session.commit()
    assert (await http.get(content_url, headers=headers)).content == b"source"
    runtime_content.assert_awaited_once()
    await db_session.delete(grant)
    await db_session.commit()
    assert (await http.get(content_url, headers=headers)).status_code == 403


@pytest.mark.parametrize(
    "fields",
    [
        {"chunk_size": 10},
        {"chunk_overlap": -1},
        {"max_upload_bytes": 0},
        {"search_timeout_secs": 121},
        {"enabled": True, "unknown": 1},
    ],
)
def test_invalid_knowledge_yaml_fails(fields):
    from app.config_file import WorkmateFileConfig

    with pytest.raises(ValidationError):
        WorkmateFileConfig.model_validate({"product": {"knowledge": fields}})


def test_knowledge_yaml_is_policy_source(monkeypatch, tmp_path):
    from app.config_file import load_file_config

    config = tmp_path / "knowledge.yaml"
    config.write_text(
        "version: 1\nproduct:\n  knowledge:\n    enabled: true\n    chunk_size: 256\n    chunk_overlap: 32\n    max_upload_bytes: 1024\n    search_timeout_secs: 12\n"
    )
    monkeypatch.setenv("WORKMATE_CONFIG_FILE", str(config))
    monkeypatch.setenv("KNOWLEDGE_CHUNK_SIZE", "999")
    policy = load_file_config().product.knowledge
    assert policy.enabled and policy.chunk_size == 256 and policy.chunk_overlap == 32
    assert policy.max_upload_bytes == 1024 and policy.search_timeout_secs == 12
