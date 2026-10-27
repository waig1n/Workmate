"""Management duties and scope boundaries must survive navigation and restart."""

import uuid

from sqlalchemy import delete, select

from app.db import get_sessionmaker
from workmate_product.persistence.models import (
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    ScopeKind,
    SecurityAuditEvent,
    User,
)
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import create_scope, ensure_bootstrap, get_root_scope


async def grant(http, keys, *, department=False):
    result = await http.post('/api/auth/register', json={
        'email': f'{uuid.uuid4()}@test.dev', 'display_name': 'Scoped manager',
        'password': 'password123',
    })
    payload = result.json()
    user_id = payload['user']['id']
    async with get_sessionmaker()() as db:
        root = await get_root_scope(db)
        scope = await create_scope(db, kind=ScopeKind.DEPARTMENT,
                                   name=str(uuid.uuid4()), parent=root) if department else root
        role = Role(id=str(uuid.uuid4()), key=str(uuid.uuid4()), display_name='Manager')
        db.add(role)
        await db.flush()
        for key in keys:
            permission = await db.scalar(select(Permission).where(Permission.key == key))
            assert permission is not None, key
            db.add(RolePermission(id=str(uuid.uuid4()), role_id=role.id,
                                  permission_id=permission.id))
        db.add(RoleBinding(user_id=user_id, role_id=role.id, scope_id=scope.id,
                           include_descendants=True))
        await db.commit()
        return {'Authorization': f"Bearer {payload['access_token']}"}, user_id, scope.id


async def test_department_permission_does_not_become_root_authority(client_and_app, auth_headers):
    http, _ = client_and_app
    headers, _, scope_id = await grant(http, ['scope.manage'], department=True)
    result = (await http.get('/api/auth/me/permissions', headers=headers)).json()
    assert 'scope.manage' in result['permissions']
    assert 'scope.manage' not in result['root_permissions']
    assert scope_id in result['managed_scope_ids']
    assert not result['is_root_admin']
    assert (await http.get('/api/admin/users', headers=headers)).status_code == 403


async def test_account_reader_cannot_write_users_or_roles(client_and_app, auth_headers):
    http, _ = client_and_app
    headers, _, _ = await grant(http, ['user.read'])
    assert (await http.get('/api/admin/users', headers=headers)).status_code == 200
    response = await http.post('/api/admin/users', headers=headers, json={
        'email': 'forbidden@test.dev', 'display_name': 'Forbidden',
        'initial_password': 'password123',
    })
    assert response.status_code == 403
    assert (await http.get('/api/admin/roles', headers=headers)).status_code == 403


async def test_root_only_keys_do_not_inherit_into_department(client_and_app, auth_headers):
    http, _ = client_and_app
    headers, user_id, root_id = await grant(http, ['user.manage'])
    async with get_sessionmaker()() as db:
        root = await get_root_scope(db)
        dept = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Finance', parent=root)
        access = AccessDecisionService(db)
        assert await access.has_permission(user_id, root_id, 'user.manage')
        assert not await access.has_permission(user_id, dept.id, 'user.manage')
        assert 'user.manage' not in await access.permissions_for_scope(user_id, dept.id)


async def test_partial_root_manager_is_not_last_recovery_admin(client_and_app, auth_headers):
    http, _ = client_and_app
    _, user_id, _ = await grant(http, ['scope.manage'])
    async with get_sessionmaker()() as db:
        assert user_id not in await AccessDecisionService(db).active_root_admin_ids()


async def test_bootstrap_does_not_restore_revoked_admin_binding(client_and_app, auth_headers):
    http, _ = client_and_app
    first_id = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    async with get_sessionmaker()() as db:
        await db.execute(delete(RoleBinding).where(RoleBinding.user_id == first_id))
        await db.commit()
        await ensure_bootstrap(db)
        assert not await db.scalar(select(RoleBinding).where(RoleBinding.user_id == first_id))


async def test_audit_reader_sees_only_permitted_scope(client_and_app, auth_headers):
    http, _ = client_and_app
    headers, user_id, scope_id = await grant(http, ['audit.read'], department=True)
    async with get_sessionmaker()() as db:
        root = await get_root_scope(db)
        for scope, event in [(scope_id, 'scope.rename'), (root.id, 'user.disable'), (None, 'role.create')]:
            db.add(SecurityAuditEvent(id=str(uuid.uuid4()), event_type=event,
                                      actor_user_id=user_id, scope_id=scope,
                                      metadata_={'name': 'Safe', 'password': 'do-not-return'}))
        await db.commit()
    response = await http.get('/api/admin/security-audit-events', headers=headers)
    assert response.status_code == 200, response.text
    assert [item['event_type'] for item in response.json()['items']] == ['scope.rename']
    assert 'do-not-return' not in response.text
    assert (await http.get('/api/admin/security-audit-events?scope_id=other', headers=headers)).status_code == 403
