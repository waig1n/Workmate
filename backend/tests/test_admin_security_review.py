"""Review regressions for authorization at the serialized write boundary."""

from sqlalchemy import delete, select

from app.db import get_sessionmaker
from workmate_product.persistence.models import Role, RoleBinding, Scope, ScopeKind, User
from app.services.role_management import RoleManagementService


async def test_role_write_rechecks_actor_after_waiting_for_root_lock(
    client_and_app, auth_headers, monkeypatch,
):
    http, _ = client_and_app
    backup = (await http.post("/api/auth/register", json={
        "email": "backup-review@test.dev", "password": "password123",
        "display_name": "Backup",
    })).json()["user"]["id"]
    async with get_sessionmaker()() as db:
        admin = await db.scalar(select(Role).where(Role.key == "admin"))
        root = await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION))
        actor = await db.scalar(select(User).where(User.email == "u@test.dev"))
        actor_id = actor.id
        db.add(RoleBinding(user_id=backup, role_id=admin.id, scope_id=root.id))
        await db.commit()
    role = (await http.post("/api/admin/roles", headers=auth_headers, json={
        "key": "review_target", "display_name": "Original", "permission_keys": [],
    })).json()
    original_lock = RoleManagementService._lock_root
    revoked = False

    async def lock_after_revocation(service):
        # Model the state visible when a queued transaction finally gets
        # the lock: another administrator has committed actor revocation.
        nonlocal revoked
        if not revoked:
            revoked = True
            await service._db.execute(delete(RoleBinding).where(RoleBinding.user_id == actor_id))
            await service._db.commit()
        await original_lock(service)

    monkeypatch.setattr(RoleManagementService, "_lock_root", lock_after_revocation)
    result = await http.patch(f"/api/admin/roles/{role['id']}", headers=auth_headers, json={
        "display_name": "Should not persist", "permission_keys": ["agent.create"],
        "expected_version": role["version"],
    })
    assert result.status_code == 403, result.text
    async with get_sessionmaker()() as db:
        assert (await db.get(Role, role["id"])).display_name == "Original"


async def test_role_write_requires_a_concurrency_baseline(client_and_app, auth_headers):
    http, _ = client_and_app
    role = (await http.post("/api/admin/roles", headers=auth_headers, json={
        "key": "review_version", "display_name": "Original", "permission_keys": [],
    })).json()
    result = await http.patch(f"/api/admin/roles/{role['id']}", headers=auth_headers,
                              json={"permission_keys": ["agent.create"]})
    assert result.status_code == 422, result.text
