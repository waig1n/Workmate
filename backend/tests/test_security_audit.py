# -*- coding: utf-8 -*-
"""Security administration audit history and sanitization tests."""

from sqlalchemy import select

from app.db import get_sessionmaker
from workmate_product.persistence.models import SecurityAuditEvent
from app.services.security_audit import SecurityAuditService


async def _events():
    async with get_sessionmaker()() as db:
        return list((await db.scalars(
            select(SecurityAuditEvent).order_by(SecurityAuditEvent.created_at),
        )).all())


async def test_user_role_permission_and_scope_mutations_are_audited(
    client_and_app, auth_headers,
):
    http, _ = client_and_app
    created_user = await http.post("/api/admin/users", json={
        "email": "audit-user@test.dev", "display_name": "Audit User",
        "initial_password": "password123",
    }, headers=auth_headers)
    assert created_user.status_code == 201, created_user.text

    permission = (await http.post("/api/admin/permissions", json={
        "key": "audit.custom", "display_name": "Audit", "description": "",
    }, headers=auth_headers)).json()
    role = (await http.post("/api/admin/roles", json={
        "key": "audit-role", "display_name": "Audit Role",
        "permission_keys": ["audit.custom"],
    }, headers=auth_headers)).json()
    roles = await http.put(
        f"/api/admin/roles/{role['id']}/permissions",
        json={"permission_keys": [], "expected_version": role["version"]}, headers=auth_headers,
    )
    assert roles.status_code == 200
    await http.delete(
        f"/api/admin/roles/{role['id']}", headers=auth_headers,
    )
    await http.delete(
        f"/api/admin/permissions/{permission['id']}", headers=auth_headers,
    )

    tree = (await http.get(
        "/api/admin/scopes/tree", headers=auth_headers,
    )).json()
    root_id = tree[0]["id"]
    scope = (await http.post("/api/admin/scopes", json={
        "parent_id": root_id, "name": "Audit Department",
    }, headers=auth_headers)).json()
    renamed = await http.patch(
        f"/api/admin/scopes/{scope['id']}", json={"name": "Audited", "expected_tree_version": scope["tree_version"]},
        headers=auth_headers,
    )
    assert renamed.status_code == 200, renamed.text
    deleted = await http.delete(
        f"/api/admin/scopes/{scope['id']}?expected_tree_version={renamed.json()['tree_version']}", headers=auth_headers,
    )
    assert deleted.status_code == 204, deleted.text

    events = await _events()
    event_types = {event.event_type for event in events}
    assert {
        "user.create", "permission.create", "permission.delete",
        "role.create", "role.permissions_update", "role.delete",
        "scope.create", "scope.rename", "scope.delete",
    } <= event_types
    deleted_event = next(
        event for event in events if event.event_type == "scope.delete"
    )
    assert deleted_event.scope_id is None
    assert deleted_event.metadata_["deleted_scope_id"] == scope["id"]


async def test_audit_metadata_drops_secret_fields(db_session):
    async with get_sessionmaker()() as db:
        SecurityAuditService(db).record("sanitize.test", metadata={
            "status": "ok", "password": "never-log-me",
            "token_value": "never-log-token", "credential": "never-log-cred",
            "force_change_required": True,
        })
        await db.commit()
    event = next(
        event for event in await _events() if event.event_type == "sanitize.test"
    )
    assert event.metadata_ == {
        "status": "ok", "force_change_required": True,
    }
    rendered = str(event.metadata_).casefold()
    for forbidden in ("password", "token", "secret", "credential", "never-log"):
        assert forbidden not in rendered
