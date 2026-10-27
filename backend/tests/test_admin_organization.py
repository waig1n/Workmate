"""Organization naming is a versioned root-only setting, never a scope move."""
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import get_sessionmaker
from workmate_product.persistence.models import (
    Agent, ResourceShare, RoleBinding, Scope, ScopeClosure, ScopeMembership, SecurityAuditEvent,
)
from test_scoped_administration import account, role


@pytest_asyncio.fixture
async def organization_headers(client_and_app):
    http, _ = client_and_app
    response = await http.post('/api/auth/register', json={
        'email': 'org-admin@test.dev', 'password': 'password123', 'display_name': 'Admin',
        'organization_name': 'Initial enterprise',
    })
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


async def test_rename_organization_preserves_ids_relationships_and_audits(client_and_app, organization_headers):
    http, _ = client_and_app
    headers = organization_headers
    response = await http.get('/api/admin/organization', headers=headers)
    assert response.status_code == 200, response.text
    root = response.json()
    assert set(root) == {'id', 'name', 'tree_version'}
    department = await http.post('/api/admin/scopes', headers=headers,
        json={'parent_id': root['id'], 'name': 'Finance'})
    assert department.status_code == 201, department.text
    root = (await http.get('/api/admin/organization', headers=headers)).json()
    async with get_sessionmaker()() as db:
        db.add(Agent(id='rename-agent', slug='rename-agent', display_name='Agent', scope_id=root['id']))
        await db.flush()
        db.add(ResourceShare(id='rename-share', agent_id='rename-agent', target_scope_id=department.json()['id']))
        await db.commit()
        before = {}
        for model in (ScopeClosure, ScopeMembership, RoleBinding, ResourceShare):
            before[model] = (await db.execute(select(model.__table__))).all()
    renamed = await http.patch('/api/admin/organization', headers=headers,
        json={'name': '  Example Enterprise  ', 'expected_tree_version': root['tree_version']})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {'id': root['id'], 'name': 'Example Enterprise', 'tree_version': root['tree_version'] + 1}
    async with get_sessionmaker()() as db:
        for model, rows in before.items():
            assert (await db.execute(select(model.__table__))).all() == rows
        assert (await db.get(Scope, root['id'])).normalized_name == 'example enterprise'
        assert (await db.get(Agent, 'rename-agent')).scope_id == root['id']
        event = await db.scalar(select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == 'organization.rename'))
        assert event.scope_id == root['id']
        assert event.metadata_['old_name'] == root['name']
        assert event.metadata_['new_name'] == 'Example Enterprise'
    stale = await http.patch('/api/admin/organization', headers=headers,
        json={'name': 'Stale replacement', 'expected_tree_version': root['tree_version']})
    assert stale.status_code == 409, stale.text
    assert stale.json()['detail']['code'] == 'version_conflict'
    assert (await http.get('/api/admin/organization', headers=headers)).json() == renamed.json()


@pytest.mark.parametrize('body', [
    {'name': '   ', 'expected_tree_version': 1},
    {'name': 'x' * 201, 'expected_tree_version': 1},
    {'name': 'ß' * 200, 'expected_tree_version': 1},
    {'name': 'Missing version'},
    {'name': 'Name', 'expected_tree_version': 0},
])
async def test_organization_name_validation_is_atomic(client_and_app, organization_headers, body):
    http, _ = client_and_app
    before = (await http.get('/api/admin/organization', headers=organization_headers)).json()
    response = await http.patch('/api/admin/organization', headers=organization_headers, json=body)
    assert response.status_code == 422, response.text
    assert (await http.get('/api/admin/organization', headers=organization_headers)).json() == before


async def test_department_management_does_not_authorize_organization_settings(client_and_app, organization_headers):
    http, _ = client_and_app
    root = (await http.get('/api/admin/organization', headers=organization_headers)).json()
    department = (await http.post('/api/admin/scopes', headers=organization_headers,
        json={'parent_id': root['id'], 'name': 'Finance'})).json()
    manager = await role(http, organization_headers, 'department-manager', ['scope.manage'])
    _, member_headers = await account(http, organization_headers, 'ordinary@test.dev', [department['id']])
    _, department_headers = await account(http, organization_headers, 'department-admin@test.dev', [department['id']], [
        {'scope_id': department['id'], 'role_key': manager['key'], 'include_descendants': True},
    ])
    for headers in (member_headers, department_headers):
        assert (await http.get('/api/admin/organization', headers=headers)).status_code == 403
        response = await http.patch('/api/admin/organization', headers=headers,
            json={'name': 'Forbidden', 'expected_tree_version': root['tree_version']})
        assert response.status_code == 403, response.text
    _, root_headers = await account(http, organization_headers, 'root-manager@test.dev', [], [
        {'scope_id': root['id'], 'role_key': manager['key'], 'include_descendants': False},
    ])
    allowed = await http.get('/api/admin/organization', headers=root_headers)
    assert allowed.status_code == 200, allowed.text
    assert (await http.get('/api/admin/organization')).status_code == 401
    assert (await http.patch('/api/admin/organization', json={
        'name': 'Anonymous', 'expected_tree_version': 1})).status_code == 401


async def test_organization_rename_serializes_concurrent_updates(client_and_app, organization_headers):
    import asyncio
    http, _ = client_and_app
    before = (await http.get('/api/admin/organization', headers=organization_headers)).json()
    results = await asyncio.gather(*[
        http.patch('/api/admin/organization', headers=organization_headers,
                   json={'name': name, 'expected_tree_version': before['tree_version']})
        for name in ('First edit', 'Second edit')
    ])
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response.json() for response in results if response.status_code == 200)
    assert (await http.get('/api/admin/organization', headers=organization_headers)).json() == winner
    assert winner['tree_version'] == before['tree_version'] + 1


async def test_root_scope_manager_can_rename_and_revocation_takes_effect(client_and_app, organization_headers):
    http, _ = client_and_app
    root = (await http.get('/api/admin/organization', headers=organization_headers)).json()
    manager = await role(http, organization_headers, 'root-scope-manager', ['scope.manage'])
    person, headers = await account(http, organization_headers, 'limited-root@test.dev', [], [
        {'scope_id': root['id'], 'role_key': manager['key'], 'include_descendants': False},
    ])
    response = await http.patch('/api/admin/organization', headers=headers,
        json={'name': '  ' + 'x' * 200 + '  ', 'expected_tree_version': root['tree_version']})
    assert response.status_code == 200, response.text
    assert response.json()['name'] == 'x' * 200
    async with get_sessionmaker()() as db:
        binding = await db.get(RoleBinding, (person['id'], manager['id'], root['id']))
        await db.delete(binding)
        await db.commit()
    assert (await http.get('/api/admin/organization', headers=headers)).status_code == 403
    denied = await http.patch('/api/admin/organization', headers=headers,
        json={'name': 'Revoked', 'expected_tree_version': response.json()['tree_version']})
    assert denied.status_code == 403, denied.text
    assert (await http.get('/api/admin/organization', headers=organization_headers)).json() == response.json()
