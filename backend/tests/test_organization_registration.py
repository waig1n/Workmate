"""First account initializes the existing organization atomically and once."""
import asyncio
import pytest
from sqlalchemy import select, func
from app.api.routers import auth as auth_router
from app.services.bootstrap import get_root_scope
from workmate_product.persistence.models import Role, RoleBinding, Scope, ScopeKind, SecurityAuditEvent, User


def payload(email='first@example.com', name='  Example Company  '):
    return {'email': email, 'password': 'password123', 'display_name': 'First', 'organization_name': name}


async def test_first_registration_names_existing_root_and_creates_single_admin(client_and_app, db_session):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    root_id = root.id
    old_name, old_version = root.name, root.tree_version
    response = await http.post('/api/auth/register', json=payload())
    assert response.status_code == 200, response.text
    await db_session.refresh(root)
    assert root.id == root_id
    assert root.name == 'Example Company'
    assert root.normalized_name == 'example company'
    assert root.tree_version == old_version + 1
    audit = await db_session.scalar(select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == 'organization.initialize'))
    assert audit.metadata_ == {'old_name': old_name, 'new_name': 'Example Company'}
    assert audit.actor_user_id == response.json()['user']['id']
    assert await db_session.scalar(select(func.count()).select_from(Scope).where(Scope.kind == ScopeKind.ORGANIZATION)) == 1
    bindings = (await db_session.execute(select(RoleBinding, Role.key).join(Role, Role.id == RoleBinding.role_id))).all()
    assert [(binding.user_id, binding.scope_id, key) for binding, key in bindings] == [(response.json()['user']['id'], root_id, 'admin')]
    assert (await http.get('/api/auth/registration-status')).json() == {'registration_available': True, 'bootstrap_required': False}


async def test_bootstrap_status_uses_accounts_not_placeholder_name(client_and_app, db_session):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    root.name = 'Already customized without accounts'
    await db_session.commit()
    assert (await http.get('/api/auth/registration-status')).json()['bootstrap_required'] is True
    response = await http.post('/api/auth/register', json=payload(name='workmate'))
    assert response.status_code == 200
    assert (await http.get('/api/auth/registration-status')).json()['bootstrap_required'] is False


@pytest.mark.parametrize('name', [None, '', '   ', 'x' * 201, 'ß' * 101])
async def test_first_registration_requires_valid_organization_name(client_and_app, db_session, name):
    http, _ = client_and_app
    body = payload(name=name)
    if name is None:
        body.pop('organization_name')
    response = await http.post('/api/auth/register', json=body)
    assert response.status_code == 422, response.text
    assert await db_session.scalar(select(User.id)) is None


@pytest.mark.parametrize('name', ['Changed name', None])
async def test_later_registration_cannot_supply_organization_name(client_and_app, auth_headers, db_session, name):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    original_name = root.name
    response = await http.post('/api/auth/register', json=payload('later@example.com', name))
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'organization_already_initialized'
    await db_session.refresh(root)
    assert root.name == original_name
    assert await db_session.scalar(select(User).where(User.email == 'later@example.com')) is None


async def test_concurrent_open_bootstrap_accepts_one_and_rejects_second_intent(client_and_app, db_session):
    http, _ = client_and_app
    if db_session.bind.dialect.name != 'postgresql':
        pytest.skip('requires PostgreSQL row locks')
    bodies = [payload('one@example.com', 'Company One'), payload('two@example.com', 'Company Two')]
    responses = await asyncio.gather(*(http.post('/api/auth/register', json=body) for body in bodies))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(body for body, response in zip(bodies, responses) if response.status_code == 200)
    root = await get_root_scope(db_session)
    await db_session.refresh(root)
    assert root.name == winner['organization_name']
    assert await db_session.scalar(select(func.count()).select_from(User)) == 1
    assert await db_session.scalar(select(func.count()).select_from(RoleBinding)) == 1


async def test_bootstrap_failure_rolls_back_name_account_and_admin(client_and_app, db_session, monkeypatch):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    original_name, original_version = root.name, root.tree_version
    original = auth_router._ensure_user_scopes
    async def failing(db, user, *, first_user=False):
        await original(db, user, first_user=first_user)
        await db.flush()
        raise RuntimeError('injected provisioning failure')
    monkeypatch.setattr(auth_router, '_ensure_user_scopes', failing)
    with pytest.raises(RuntimeError, match='injected provisioning failure'):
        await http.post('/api/auth/register', json=payload())
    await db_session.refresh(root)
    assert (root.name, root.tree_version) == (original_name, original_version)
    assert await db_session.scalar(select(User.id)) is None
    assert await db_session.scalar(select(RoleBinding.user_id)) is None
    assert await db_session.scalar(select(Scope.id).where(Scope.kind == ScopeKind.PERSONAL)) is None
