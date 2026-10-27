import uuid
from product_catalog_fakes import seed_skill_artifact

import pytest
from sqlalchemy import select

from workmate_product.persistence.models import (
    Agent,
    AgentCapabilityBinding,
    CapabilityInstallation,
    CapabilityType,
    ResourceShare,
    ScopeKind,
    ScopeMembership,
)
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import create_scope, get_root_scope


@pytest.fixture
async def graph(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    me = (await http.get('/api/auth/me', headers=auth_headers)).json()
    root = await get_root_scope(db_session)
    scopes = [await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name=name, parent=root) for name in ('A', 'B', 'C')]
    await db_session.commit()
    registered = (await http.post('/api/auth/register', json={'email': 'shared@example.com', 'password': 'password123', 'display_name': 'Recipient'})).json()
    headers = {'Authorization': f"Bearer {registered['access_token']}"}
    recipient = (await http.get('/api/auth/me', headers=headers)).json()
    db_session.add(ScopeMembership(scope_id=scopes[1].id, user_id=recipient['id']))
    agent = Agent(id=str(uuid.uuid4()), slug='shared-agent', display_name='Shared Agent', scope_id=scopes[0].id, created_by=me['id'], system_prompt='Private instructions', visibility='public')
    skill = CapabilityInstallation(id=str(uuid.uuid4()), capability_type=CapabilityType.SKILL, scope_id=scopes[0].id,   name='shared-skill', enabled=True)
    db_session.add_all([agent, skill])
    await db_session.flush()
    await seed_skill_artifact(db_session, skill)
    db_session.add(ResourceShare(id=str(uuid.uuid4()), capability_id=skill.id, target_scope_id=scopes[0].id))
    db_session.add(AgentCapabilityBinding(agent_id=agent.id, capability_installation_id=skill.id))
    await db_session.commit()
    return http, headers, recipient['id'], agent, skill, scopes


@pytest.mark.asyncio
async def test_share_agent_and_dependencies_atomically_and_revoke_independently(graph, auth_headers, db_session):
    http, recipient_headers, user_id, agent, skill, scopes = graph
    url = f'/api/sharing/agents/{agent.id}'
    preview = await http.post(url + '/preview', headers=auth_headers, json={'target_scope_id': scopes[1].id})
    assert preview.status_code == 200, preview.text
    assert [item['id'] for item in preview.json()['skills']] == [skill.id]
    denied = await http.post(url, headers=auth_headers, json={'target_scope_id': scopes[1].id})
    assert denied.status_code == 409
    applied = await http.post(url, headers=auth_headers, json={'target_scope_id': scopes[1].id, 'confirmation': preview.json()['confirmation']})
    assert applied.status_code == 200, applied.text
    access = AccessDecisionService(db_session)
    assert await access.can_use_agent(user_id, agent.id)
    assert await access.unavailable_capability_ids(user_id, agent.id) == []
    listed = await http.get('/api/skills', headers=recipient_headers)
    assert skill.id in [item['id'] for item in listed.json()['items']]
    assert not await access.can_manage_agent(user_id, agent.id)
    revoked = await http.delete(url + '/' + scopes[1].id, headers=auth_headers)
    assert revoked.status_code == 204, revoked.text
    assert not await access.can_use_agent(user_id, agent.id)
    assert skill.id in await access.accessible_capability_ids(user_id)


@pytest.mark.asyncio
async def test_skill_revoke_invalidates_bound_shared_agent(graph, auth_headers, db_session):
    http, _, user_id, agent, skill, scopes = graph
    db_session.add_all([
        ResourceShare(id=str(uuid.uuid4()), agent_id=agent.id, target_scope_id=scopes[1].id),
        ResourceShare(id=str(uuid.uuid4()), capability_id=skill.id, target_scope_id=scopes[1].id),
    ])
    await db_session.commit()
    response = await http.delete(f'/api/sharing/skills/{skill.id}/{scopes[1].id}', headers=auth_headers)
    assert response.status_code == 204, response.text
    assert await AccessDecisionService(db_session).unavailable_capability_ids(user_id, agent.id) == [skill.id]


@pytest.mark.asyncio
async def test_recipient_cannot_reshare(graph, auth_headers):
    http, headers, _, agent, _, scopes = graph
    url = f'/api/sharing/agents/{agent.id}'
    preview = await http.post(url + '/preview', headers=auth_headers, json={'target_scope_id': scopes[1].id})
    await http.post(url, headers=auth_headers, json={'target_scope_id': scopes[1].id, 'confirmation': preview.json()['confirmation']})
    response = await http.post(url + '/preview', headers=headers, json={'target_scope_id': scopes[2].id})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_binding_update_requires_new_confirmation_and_rejects_stale_preview(graph, auth_headers, db_session):
    http, _, _, agent, skill, scopes = graph
    url = f'/api/sharing/agents/{agent.id}'
    body = {'target_scope_id': scopes[1].id}
    original = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    extra = CapabilityInstallation(id=str(uuid.uuid4()), capability_type=CapabilityType.SKILL, scope_id=scopes[0].id,   name='extra', enabled=True)
    db_session.add(extra)
    await db_session.flush()
    await seed_skill_artifact(db_session, extra)
    db_session.add(ResourceShare(id=str(uuid.uuid4()), capability_id=extra.id, target_scope_id=scopes[0].id))
    db_session.add(AgentCapabilityBinding(agent_id=agent.id, capability_installation_id=extra.id))
    await db_session.commit()
    assert (await http.post(url, headers=auth_headers, json={**body, 'confirmation': original['confirmation']})).status_code == 409
    preview = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    assert (await http.post(url, headers=auth_headers, json={**body, 'confirmation': preview['confirmation']})).status_code == 200
    # Revoke the new dependency, then explicitly repair while saving bindings.
    assert (await http.delete(f'/api/sharing/skills/{extra.id}/{scopes[1].id}', headers=auth_headers)).status_code == 204
    changed = {'skill_binding_ids': [skill.id, extra.id]}
    result = await http.patch(f'/api/agents/{agent.id}', headers=auth_headers, json=changed)
    assert result.status_code == 409, result.text
    detail = result.json()['detail']
    assert detail['code'] == 'sharing_confirmation_required'
    confirmed = await http.patch(f'/api/agents/{agent.id}', headers=auth_headers, json={**changed, 'sharing_confirmation': detail['confirmation']})
    assert confirmed.status_code == 200, confirmed.text


@pytest.mark.asyncio
async def test_mcp_without_source_use_grant_blocks_agent_sharing_atomically(graph, auth_headers, db_session):
    http, _, _, agent, skill, scopes = graph
    mcp = CapabilityInstallation(id=str(uuid.uuid4()), capability_type=CapabilityType.MCP, scope_id=scopes[0].id,   name='private-mcp', enabled=True)
    db_session.add(mcp)
    await db_session.flush()
    db_session.add(AgentCapabilityBinding(agent_id=agent.id, capability_installation_id=mcp.id))
    await db_session.commit()
    url = f'/api/sharing/agents/{agent.id}'
    body = {'target_scope_id': scopes[1].id}
    plan = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    assert plan['blockers']
    result = await http.post(url, headers=auth_headers, json={**body, 'confirmation': plan['confirmation']})
    assert result.status_code == 409
    assert list((await db_session.scalars(select(ResourceShare).where(ResourceShare.target_scope_id != scopes[0].id))).all()) == []


@pytest.mark.asyncio
async def test_explicit_skill_sharing_allows_binding_but_not_descendant_or_resharing(graph, auth_headers, db_session):
    http, _, user_id, agent, skill, scopes = graph
    url = f'/api/sharing/skills/{skill.id}'
    body = {'target_scope_id': scopes[1].id}
    plan = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    result = await http.post(url, headers=auth_headers, json={**body, 'confirmation': plan['confirmation']})
    assert result.status_code == 200, result.text
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='B child', parent=scopes[1])
    await db_session.commit()
    access = AccessDecisionService(db_session)
    assert skill.id in await access.bindable_capability_ids(scopes[1])
    assert skill.id not in await access.bindable_capability_ids(child)
    assert skill.id not in await access.bindable_capability_ids(scopes[2])
    created = await http.post('/api/agents', headers=auth_headers, json={'slug': 'b-agent', 'display_name': 'B Agent', 'scope_id': scopes[1].id, 'skill_binding_ids': [skill.id]})
    assert created.status_code == 201, created.text


@pytest.mark.asyncio
async def test_missing_dependency_management_blocks_entire_share(graph, auth_headers, db_session):
    from workmate_product.persistence.models import Role, RoleBinding, RolePermission, Permission
    http, headers, user_id, agent, skill, scopes = graph
    role = Role(id=str(uuid.uuid4()), key='sharing-agent-manager', display_name='Agent manager')
    db_session.add(role)
    await db_session.flush()
    for key in ('agent.update', 'agent.publish'):
        permission = await db_session.scalar(select(Permission).where(Permission.key == key))
        db_session.add(RolePermission(id=str(uuid.uuid4()), role_id=role.id, permission_id=permission.id))
    db_session.add(RoleBinding(user_id=user_id, role_id=role.id, scope_id=scopes[0].id, include_descendants=False))
    await db_session.commit()
    url = f'/api/sharing/agents/{agent.id}'
    body = {'target_scope_id': scopes[2].id}
    preview = await http.post(url + '/preview', headers=headers, json=body)
    assert preview.status_code == 200
    assert preview.json()['blockers']
    result = await http.post(url, headers=headers, json={**body, 'confirmation': preview.json()['confirmation']})
    assert result.status_code == 409
    assert list((await db_session.scalars(select(ResourceShare).where(ResourceShare.target_scope_id != scopes[0].id))).all()) == []


@pytest.mark.asyncio
async def test_personal_source_and_target_rejected(graph, auth_headers, db_session):
    http, _, user_id, agent, _, scopes = graph
    from workmate_product.persistence.models import Scope
    personal = await db_session.scalar(select(Scope).where(Scope.owner_user_id == user_id, Scope.kind == ScopeKind.PERSONAL))
    response = await http.post(f'/api/sharing/agents/{agent.id}/preview', headers=auth_headers, json={'target_scope_id': personal.id})
    assert response.status_code == 422
    agent.scope_id = personal.id
    await db_session.commit()
    response = await http.post(f'/api/sharing/agents/{agent.id}/preview', headers=auth_headers, json={'target_scope_id': scopes[1].id})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_revoke_skill_blocks_new_task_and_runtime_preflight(graph, auth_headers, db_session):
    from workmate_product.persistence.models import Conversation
    http, headers, user_id, agent, skill, scopes = graph
    db_session.add(ResourceShare(id=str(uuid.uuid4()), agent_id=agent.id, target_scope_id=scopes[1].id))
    conv = Conversation(id=str(uuid.uuid4()), user_id=user_id, agent_id=agent.id, title='Shared conversation')
    db_session.add(conv)
    await db_session.commit()
    result = await http.post(f'/api/tasks/conversations/{conv.id}/tasks', headers=headers, json={'message': 'Run'})
    assert result.status_code == 409, result.text
    factory_access = AccessDecisionService(db_session)
    from app.api.routers._agent_views import AgentOutFactory
    factory = AgentOutFactory(db_session, factory_access, user_id)
    await factory.prepare([agent])
    assert (await factory.build(agent)).unavailable_capability_ids == [skill.id]


@pytest.mark.asyncio
async def test_batch_share_revalidates_all_targets_before_writing(graph, auth_headers, db_session):
    http, _, _, agent, _, scopes = graph
    url = f'/api/sharing/agents/{agent.id}'
    targets = []
    for scope in scopes[1:]:
        plan = (await http.post(url + '/preview', headers=auth_headers, json={'target_scope_id': scope.id})).json()
        targets.append({'target_scope_id': scope.id, 'confirmation': plan['confirmation']})
    invalid = [targets[0], {**targets[1], 'confirmation': 'stale'}]
    response = await http.post(url + '/batch', headers=auth_headers, json={'targets': invalid})
    assert response.status_code == 409
    assert not (await db_session.execute(select(ResourceShare).where(ResourceShare.target_scope_id != scopes[0].id))).scalars().all()
    response = await http.post(url + '/batch', headers=auth_headers, json={'targets': targets})
    assert response.status_code == 200, response.text
    grants = (await db_session.execute(select(ResourceShare))).scalars().all()
    assert len(grants) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('targets', [[], [{'target_scope_id': 'b'}, {'target_scope_id': 'b'}], [{'target_scope_id': 'b'}] * 101])
async def test_batch_rejects_invalid_selection(graph, auth_headers, targets):
    http, _, _, agent, _, _ = graph
    response = await http.post(f'/api/sharing/agents/{agent.id}/batch', headers=auth_headers, json={'targets': targets})
    assert response.status_code == 422
