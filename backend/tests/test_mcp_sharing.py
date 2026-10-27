"""MCP explicit use grants carry no connection configuration or credentials."""
import importlib
import uuid
import pytest
from sqlalchemy import select
from workmate_product.persistence.models import AgentCapabilityBinding, CapabilityInstallation, CapabilityType, ResourceShare, ScopeKind, ScopeMembership
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import create_scope
from test_resource_sharing import graph
from test_branch_sharing import share


@pytest.fixture
async def mcp_graph(graph, db_session):
    http, headers, uid, agent, skill, scopes = graph
    mcp = CapabilityInstallation(id=str(uuid.uuid4()), capability_type=CapabilityType.MCP,
        scope_id=scopes[0].id, name='private-mcp', display_name='Finance Connector', enabled=True,
        metadata_={'url': 'https://internal.invalid', 'headers': {'Authorization': 'secret-value'}})
    db_session.add(mcp)
    await db_session.flush()
    db_session.add(ResourceShare(id=str(uuid.uuid4()), capability_id=mcp.id, target_scope_id=scopes[0].id))
    db_session.add(AgentCapabilityBinding(agent_id=agent.id, capability_installation_id=mcp.id))
    await db_session.commit()
    return (*graph, mcp)


def test_mcp_is_a_supported_sharing_kind():
    from pydantic import TypeAdapter
    from app.api.routers.sharing import Kind
    assert TypeAdapter(Kind).validate_python('mcps') == 'mcps'


async def test_agent_sharing_confirms_mcp_dependencies_without_credentials(mcp_graph, auth_headers, db_session):
    http, headers, uid, agent, _, scopes, mcp = mcp_graph
    url = f'/api/sharing/agents/{agent.id}'
    body = {'target_scope_id': scopes[1].id, 'include_descendants': True}
    preview = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    assert preview['mcps'] == [{'id': mcp.id, 'name': 'Finance Connector', 'scope_id': scopes[0].id}]
    assert preview['blockers'] == []
    assert 'secret-value' not in str(preview) and 'internal.invalid' not in str(preview)
    assert (await http.post(url, headers=auth_headers, json=body)).status_code == 409
    assert not await db_session.scalar(select(ResourceShare.id).where(
        ResourceShare.capability_id == mcp.id, ResourceShare.target_scope_id == scopes[1].id))
    await share(http, auth_headers, 'agents', agent.id, scopes[1].id, include_descendants=True)
    origins = await http.get(f'/api/sharing/mcps/{mcp.id}/access', headers=headers)
    assert origins.status_code == 200, origins.text
    assert origins.json()['can_use'] is True
    assert 'secret-value' not in origins.text and 'internal.invalid' not in origins.text
    assert (await http.get(f'/api/sharing/skills/{mcp.id}', headers=auth_headers)).status_code == 404


async def test_direct_mcp_share_can_narrow_branch_and_checks_type(mcp_graph, auth_headers, db_session):
    http, headers, uid, _, skill, scopes, mcp = mcp_graph
    await share(http, auth_headers, 'mcps', mcp.id, scopes[1].id, include_descendants=True)
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Future', parent=scopes[1])
    await db_session.delete(await db_session.get(ScopeMembership, (scopes[1].id, uid)))
    db_session.add(ScopeMembership(scope_id=child.id, user_id=uid))
    await db_session.commit()
    access = AccessDecisionService(db_session)
    assert mcp.id in await access.accessible_capability_ids(uid)
    assert mcp.id in await access.bindable_capability_ids(child)
    await share(http, auth_headers, 'mcps', mcp.id, scopes[1].id, include_descendants=False)
    assert mcp.id not in await access.accessible_capability_ids(uid)
    assert (await http.get(f'/api/sharing/mcps/{skill.id}', headers=auth_headers)).status_code == 404
    assert (await http.get(f'/api/sharing/mcps/{mcp.id}/access', headers=headers)).status_code == 404


@pytest.mark.parametrize('module_name', ['app.services.resource_sharing', 'workmate_product.application.resource_sharing'])
async def test_binding_mcp_requires_explicit_dependency_confirmation(mcp_graph, db_session, module_name):
    module = importlib.import_module(module_name)
    _, _, _, agent, skill, scopes, mcp = mcp_graph
    await module.grant(db_session, 'agents', agent.id, scopes[1].id, agent.created_by, include_descendants=True)
    await module.grant(db_session, 'skills', skill.id, scopes[1].id, agent.created_by, include_descendants=True)
    await db_session.commit()
    with pytest.raises(module.HTTPException) as error:
        await module.check_binding_shares(db_session, AccessDecisionService(db_session), agent.created_by, agent, None)
    assert error.value.detail['code'] == 'sharing_confirmation_required'
    assert error.value.detail['plans'][0]['mcps'][0]['id'] == mcp.id
    await module.check_binding_shares(db_session, AccessDecisionService(db_session), agent.created_by,
                                     agent, error.value.detail['confirmation'])
    await db_session.commit()
    assert await db_session.scalar(select(ResourceShare.include_descendants).where(
        ResourceShare.capability_id == mcp.id, ResourceShare.target_scope_id == scopes[1].id))


async def test_agent_manager_without_source_mcp_manage_cannot_expand_mcp_use(mcp_graph, auth_headers, db_session):
    from test_scoped_administration import account, role
    http, _, _, agent, _, scopes, mcp = mcp_graph
    manager = await role(http, auth_headers, 'agent_only', ['agent.update', 'agent.publish', 'skill.manage'], False)
    _, headers = await account(http, auth_headers, 'agent-manager@example.com', [scopes[0].id], [
        {'scope_id': scopes[0].id, 'role_key': manager['key']}])
    preview = await http.post(f'/api/sharing/agents/{agent.id}/preview', headers=headers,
        json={'target_scope_id': scopes[1].id})
    assert preview.status_code == 200, preview.text
    assert any('MCP' in blocker and '管理权限' in blocker for blocker in preview.json()['blockers'])
    response = await http.post(f'/api/sharing/agents/{agent.id}', headers=headers,
        json={'target_scope_id': scopes[1].id, 'confirmation': preview.json()['confirmation']})
    assert response.status_code == 409
    assert (await http.get(f'/api/sharing/mcps/{mcp.id}', headers=headers)).status_code == 403
    assert not await db_session.scalar(select(ResourceShare.id).where(
        ResourceShare.capability_id == mcp.id, ResourceShare.target_scope_id == scopes[1].id))


async def test_mcp_ancestor_branch_covers_future_binding_without_duplicate_grants(mcp_graph, auth_headers, db_session):
    http, _, _, agent, _, scopes, mcp = mcp_graph
    await share(http, auth_headers, 'mcps', mcp.id, scopes[1].id, include_descendants=True)
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Future', parent=scopes[1])
    await db_session.commit()
    plan = await share(http, auth_headers, 'agents', agent.id, child.id, include_descendants=True)
    assert plan['mcps'] == []
    assert not await db_session.scalar(select(ResourceShare.id).where(
        ResourceShare.capability_id == mcp.id, ResourceShare.target_scope_id == child.id))


async def test_mcp_sharing_state_change_invalidates_agent_confirmation(mcp_graph, auth_headers, db_session):
    from app.services import resource_sharing
    http, _, _, agent, _, scopes, mcp = mcp_graph
    url = f'/api/sharing/agents/{agent.id}'
    body = {'target_scope_id': scopes[1].id}
    preview = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    await resource_sharing.grant(db_session, 'mcps', mcp.id, scopes[2].id, agent.created_by)
    await db_session.commit()
    result = await http.post(url, headers=auth_headers, json={**body, 'confirmation': preview['confirmation']})
    assert result.status_code == 409


async def test_personal_mcp_cannot_be_shared(mcp_graph, auth_headers, db_session):
    from workmate_product.persistence.models import Scope
    http, _, _, agent, _, scopes, mcp = mcp_graph
    personal = await db_session.scalar(select(Scope).where(Scope.kind == ScopeKind.PERSONAL,
                                                          Scope.owner_user_id == agent.created_by))
    mcp.scope_id = personal.id
    await db_session.commit()
    response = await http.post(f'/api/sharing/mcps/{mcp.id}/preview', headers=auth_headers,
        json={'target_scope_id': scopes[1].id})
    assert response.status_code == 403
