import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from workmate_product.persistence.models import (
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    ScopeKind,
    ScopeMembership,
)
from app.config_file import KnowledgeFileConfig
from app.services.bootstrap import create_scope, get_root_scope
from app.services.access_decision import AccessDecisionService

pytestmark = pytest.mark.asyncio

async def register(http, email):
    response = await http.post('/api/auth/register', json={'email': email, 'password': 'password123', 'display_name': email})
    assert response.status_code == 200, response.text
    headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
    user = (await http.get('/api/auth/me', headers=headers)).json()
    return headers, user['id']

async def scopes(db):
    root = await get_root_scope(db)
    a = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='A', parent=root)
    child = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='A child', parent=a)
    b = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='B', parent=root)
    await db.commit()
    return root, a, child, b

async def create_agent(http, headers, scope, **fields):
    response = await http.post('/api/agents', headers=headers, json={
        'slug': 'review-agent', 'display_name': 'Review', 'scope_id': scope.id,
        'visibility': 'public', 'include_descendants': False, **fields,
    })
    assert response.status_code == 201, response.text
    return response.json()

async def share(http, headers, agent, target):
    url = '/api/sharing/agents/' + agent['id']
    body = {'target_scope_id': target.id}
    response = await http.post(url + '/preview', headers=headers, json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert not plan['blockers'], plan
    response = await http.post(url, headers=headers, json={**body, 'confirmation': plan['confirmation']})
    assert response.status_code == 200, response.text
    return plan

async def test_update_only_cannot_expand_public_agent_audience(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    _, a, child, _ = await scopes(db_session)
    updater_headers, updater_id = await register(http, 'updater@example.com')
    _, child_id = await register(http, 'child@example.com')
    role = Role(id=str(uuid.uuid4()), key='update-only-review', display_name='Updater')
    db_session.add(role)
    await db_session.flush()
    permission = await db_session.scalar(select(Permission).where(Permission.key == 'agent.update'))
    db_session.add(RolePermission(id=str(uuid.uuid4()), role_id=role.id, permission_id=permission.id))
    db_session.add(RoleBinding(user_id=updater_id, role_id=role.id, scope_id=a.id, include_descendants=False))
    db_session.add(ScopeMembership(user_id=child_id, scope_id=child.id))
    await db_session.commit()
    agent = await create_agent(http, auth_headers, a)
    access = AccessDecisionService(db_session)
    assert not await access.has_permission(updater_id, a.id, 'agent.publish')
    assert not await access.can_use_agent(child_id, agent['id'])
    response = await http.patch('/api/agents/' + agent['id'], headers=updater_headers, json={'include_descendants': True})
    assert response.status_code == 403, response.text
    assert not await access.can_use_agent(child_id, agent['id'])

async def test_private_agent_revokes_existing_shared_department(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    _, a, _, b = await scopes(db_session)
    _, recipient_id = await register(http, 'recipient@example.com')
    db_session.add(ScopeMembership(user_id=recipient_id, scope_id=b.id))
    await db_session.commit()
    agent = await create_agent(http, auth_headers, a)
    await share(http, auth_headers, agent, b)
    response = await http.patch('/api/agents/' + agent['id'], headers=auth_headers, json={'visibility': 'private'})
    assert response.status_code == 200, response.text
    assert response.json()['visibility'] == 'private'
    assert not await AccessDecisionService(db_session).can_use_agent(recipient_id, agent['id'])


async def grant_resource(http, headers, kind, resource_id, target_id, *, allow_download=False):
    url = f'/api/sharing/{kind}/{resource_id}'
    body = {'target_scope_id': target_id, 'allow_download': allow_download}
    preview = await http.post(url + '/preview', headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    assert not preview.json()['blockers'], preview.text
    response = await http.post(url, headers=headers, json={**body, 'confirmation': preview.json()['confirmation']})
    assert response.status_code == 200, response.text
    return preview.json()

async def test_agent_knowledge_dependencies_require_atomic_confirmation_and_recheck_on_update(
    client_and_app, auth_headers, db_session, monkeypatch,
):
    http, app = client_and_app
    _, a, _, b = await scopes(db_session)
    recipient_headers, recipient_id = await register(http, 'kb-recipient@example.com')
    admin_id = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    db_session.add_all([ScopeMembership(user_id=recipient_id, scope_id=b.id), ScopeMembership(user_id=admin_id, scope_id=a.id)])
    await db_session.commit()
    monkeypatch.setattr('app.services.knowledge.knowledge_settings', lambda: KnowledgeFileConfig(enabled=True))
    app.state.runtime_client.knowledge_request = AsyncMock(side_effect=lambda *args, **kwargs: {'id': str(uuid.uuid4())})
    libraries = []
    for name in ('Original', 'Extra'):
        result = await http.post('/api/knowledge-bases', headers=auth_headers, json={'name': name, 'scope_id': a.id})
        assert result.status_code == 201, result.text
        kb_id = result.json()['id']
        await grant_resource(http, auth_headers, 'knowledge-bases', kb_id, a.id)
        libraries.append(kb_id)
    agent = await create_agent(http, auth_headers, a, knowledge_config={'knowledge_base_ids': libraries[:1]})
    url = '/api/sharing/agents/' + agent['id']
    body = {'target_scope_id': b.id}
    preview = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    assert [kb['id'] for kb in preview['knowledge_bases']] == libraries[:1]
    assert (await http.post(url, headers=auth_headers, json=body)).status_code == 409
    assert (await http.get('/api/knowledge-bases', headers=recipient_headers)).json()['items'] == []
    assert (await http.post(url, headers=auth_headers, json={**body, 'confirmation': preview['confirmation']})).status_code == 200
    access = AccessDecisionService(db_session)
    assert await access.can_use_agent(recipient_id, agent['id'])
    assert await access.unavailable_capability_ids(recipient_id, agent['id']) == []
    visible = (await http.get('/api/knowledge-bases', headers=recipient_headers)).json()['items']
    assert visible[0]['actions'] == {'use': True, 'manage': False, 'download': False}
    changes = {'knowledge_config': {'knowledge_base_ids': libraries}}
    result = await http.patch('/api/agents/' + agent['id'], headers=auth_headers, json=changes)
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'sharing_confirmation_required'
    assert (await http.patch('/api/agents/' + agent['id'], headers=auth_headers,
        json={**changes, 'sharing_confirmation': result.json()['detail']['confirmation']})).status_code == 200
    assert await access.unavailable_capability_ids(recipient_id, agent['id']) == []
    assert (await http.delete(f'/api/sharing/knowledge-bases/{libraries[1]}/{b.id}', headers=auth_headers)).status_code == 204
    assert await access.unavailable_capability_ids(recipient_id, agent['id']) == libraries

async def test_knowledge_download_permission_changes_do_not_revoke_retrieval(
    client_and_app, auth_headers, db_session, monkeypatch,
):
    http, app = client_and_app
    root, _, _, b = await scopes(db_session)
    headers, user_id = await register(http, 'download-reader@example.com')
    db_session.add(ScopeMembership(user_id=user_id, scope_id=b.id))
    await db_session.commit()
    monkeypatch.setattr('app.services.knowledge.knowledge_settings', lambda: KnowledgeFileConfig(enabled=True))
    app.state.runtime_client.knowledge_request = AsyncMock(return_value={'id': 'raw-kb'})
    app.state.runtime_client.knowledge_content = AsyncMock(return_value=(b'raw', 'text/plain', 'attachment'))
    kb = (await http.post('/api/knowledge-bases', headers=auth_headers, json={'name': 'Raw library', 'scope_id': root.id})).json()
    for download in (False, True, False):
        await grant_resource(http, auth_headers, 'knowledge-bases', kb['id'], b.id, allow_download=download)
        item = (await http.get('/api/knowledge-bases', headers=headers)).json()['items'][0]
        assert item['actions']['use'] is True
        assert item['actions']['download'] is download
        response = await http.get(f"/api/knowledge-bases/{kb['id']}/documents/doc/content", headers=headers)
        assert response.status_code == (200 if download else 403)

async def test_agent_share_and_draft_lifecycle_has_no_implicit_owner_scope_use(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    root, a, child, b = await scopes(db_session)
    headers, member_id = await register(http, 'audience@example.com')
    db_session.add(ScopeMembership(user_id=member_id, scope_id=a.id))
    await db_session.commit()
    agent = await create_agent(http, auth_headers, root, include_descendants=True)
    assert agent['actions']['use'] is False
    assert agent['id'] not in {row['id'] for row in (await http.get('/api/agents', headers=headers)).json()['items']}
    # Source ownership and the legacy descendant checkbox never grant use.
    await grant_resource(http, auth_headers, 'agents', agent['id'], b.id)
    assert not await AccessDecisionService(db_session).can_use_agent(member_id, agent['id'])
    await grant_resource(http, auth_headers, 'agents', agent['id'], root.id)
    assert await AccessDecisionService(db_session).can_use_agent(member_id, agent['id'])
    response = await http.patch('/api/agents/' + agent['id'], headers=auth_headers, json={'visibility': 'private'})
    assert response.status_code == 200
    assert response.json()['actions']['use'] is True  # creator trial only
    assert not await AccessDecisionService(db_session).can_use_agent(member_id, agent['id'])
    assert (await http.get('/api/sharing/agents/' + agent['id'], headers=auth_headers)).json()['grants'] == []
    # Explicitly sharing a draft publishes it, and requires the normal confirmation.
    plan = await grant_resource(http, auth_headers, 'agents', agent['id'], a.id)
    assert plan['publishes_agent'] is True
    assert (await http.get('/api/agents/' + agent['id'], headers=auth_headers)).json()['visibility'] == 'public'
    assert await AccessDecisionService(db_session).can_use_agent(member_id, agent['id'])
