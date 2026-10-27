# -*- coding: utf-8 -*-
"""Department scope tree management tests (plan §3.5/§5.4)."""

from sqlalchemy import select

from app.db import get_sessionmaker
from workmate_product.persistence.models import Scope, ScopeClosure, ScopeKind


async def _root_id():
    async with get_sessionmaker()() as db:
        return await db.scalar(select(Scope.id).where(
            Scope.kind == ScopeKind.ORGANIZATION,
        ))


async def _version(http, headers):
    return (await http.get('/api/admin/scopes/tree', headers=headers)).json()[0]['tree_version']


async def _create(http, headers, parent_id, name):
    response = await http.post("/api/admin/scopes", json={
        "parent_id": parent_id, "name": name,
    }, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_scope_tree_crud_and_normalized_sibling_name(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    root = await _root_id()
    engineering = await _create(http, auth_headers, root, "Engineering")
    platform = await _create(
        http, auth_headers, engineering["id"], "Platform",
    )

    duplicate = await http.post("/api/admin/scopes", json={
        "parent_id": root, "name": " engineering ",
    }, headers=auth_headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "scope_name_taken"

    renamed = await http.patch(
        f"/api/admin/scopes/{platform['id']}",
        json={"name": "Core Platform", "expected_tree_version": await _version(http, auth_headers)}, headers=auth_headers,
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Core Platform"

    tree = (await http.get(
        "/api/admin/scopes/tree", headers=auth_headers,
    )).json()
    assert tree[0]["id"] == root
    assert "scope.manage" in tree[0]["permissions"]
    assert tree[0]["children"][0]["children"][0]["name"] == "Core Platform"


async def test_move_rebuilds_closure_and_rejects_cycle(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    root = await _root_id()
    left = await _create(http, auth_headers, root, "Left")
    child = await _create(http, auth_headers, left["id"], "Child")
    grandchild = await _create(http, auth_headers, child["id"], "Grandchild")
    right = await _create(http, auth_headers, root, "Right")

    moved = await http.post(
        f"/api/admin/scopes/{child['id']}/move",
        json={"new_parent_id": right["id"], "expected_tree_version": await _version(http, auth_headers)}, headers=auth_headers,
    )
    assert moved.status_code == 200, moved.text
    async with get_sessionmaker()() as db:
        assert await db.get(ScopeClosure, (left["id"], child["id"])) is None
        assert (await db.get(
            ScopeClosure, (right["id"], grandchild["id"]),
        )).depth == 2

    cycle = await http.post(
        f"/api/admin/scopes/{right['id']}/move",
        json={"new_parent_id": grandchild["id"], "expected_tree_version": await _version(http, auth_headers)}, headers=auth_headers,
    )
    assert cycle.status_code == 409
    assert cycle.json()["detail"]["code"] == "scope_cycle"


async def test_delete_blockers_and_immutable_root(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    root = await _root_id()
    parent = await _create(http, auth_headers, root, "Parent")
    leaf = await _create(http, auth_headers, parent["id"], "Leaf")

    blocked = await http.delete(
        f"/api/admin/scopes/{parent['id']}?expected_tree_version={await _version(http, auth_headers)}", headers=auth_headers,
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "scope_not_empty"
    assert blocked.json()["detail"]["blockers"]["children"] == 1

    assert (await http.delete(
        f"/api/admin/scopes/{leaf['id']}?expected_tree_version={await _version(http, auth_headers)}", headers=auth_headers,
    )).status_code == 204
    assert (await http.delete(
        f"/api/admin/scopes/{parent['id']}?expected_tree_version={await _version(http, auth_headers)}", headers=auth_headers,
    )).status_code == 204

    immutable = await http.patch(
        f"/api/admin/scopes/{root}", json={"name": "No", "expected_tree_version": await _version(http, auth_headers)},
        headers=auth_headers,
    )
    assert immutable.status_code == 409
    assert immutable.json()["detail"]["code"] == "root_scope_immutable"


async def test_scope_management_requires_exact_permission(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    root = await _root_id()
    await http.post("/api/auth/register", json={
        "email": "member@test.dev", "password": "password123",
        "display_name": "Member",
    })
    login = await http.post("/api/auth/login", json={
        "email": "member@test.dev", "password": "password123",
    })
    member_headers = {
        "Authorization": f"Bearer {login.json()['access_token']}",
    }
    denied = await http.post("/api/admin/scopes", json={
        "parent_id": root, "name": "Denied",
    }, headers=member_headers)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "scope_manage_required"
