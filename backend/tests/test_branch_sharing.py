"""Department subtree grants are dynamic, explicit, and independently revocable."""
import uuid
import pytest
from sqlalchemy import select
from test_resource_sharing import graph
from workmate_product.persistence.models import KnowledgeBase, ResourceShare, ScopeKind, ScopeMembership, Scope
from workmate_product.application.knowledge_access import KnowledgeAccess
from workmate_product.application.resource_sharing import grant
from app.services.access_decision import AccessDecisionService
from app.services.bootstrap import create_scope, get_root_scope


async def share(http, headers, kind, rid, target, **options):
    url = f'/api/sharing/{kind}/{rid}'
    body = {'target_scope_id': target, **options}
    preview = await http.post(url + '/preview', headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    response = await http.post(url, headers=headers, json={**body, 'confirmation': preview.json()['confirmation']})
    assert response.status_code == 200, response.text
    return response.json()


async def test_department_branch_covers_future_children_and_can_narrow(graph, auth_headers, db_session):
    http, headers, uid, agent, skill, scopes = graph
    plan = await share(http, auth_headers, 'agents', agent.id, scopes[1].id, include_descendants=True)
    assert plan['include_descendants'] is True
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Future child', parent=scopes[1])
    await db_session.delete(await db_session.get(ScopeMembership, (scopes[1].id, uid)))
    db_session.add(ScopeMembership(scope_id=child.id, user_id=uid))
    await db_session.commit()
    access = AccessDecisionService(db_session)
    assert await access.can_use_agent(uid, agent.id)
    assert skill.id in await access.accessible_capability_ids(uid)
    assert skill.id in await access.bindable_capability_ids(child)
    origins = await http.get(f'/api/sharing/agents/{agent.id}/access', headers=headers)
    assert origins.status_code == 200, origins.text
    assert origins.json()['sources'][0]['inherited'] is True
    assert origins.json()['sources'][0]['member_scope_ids'] == [child.id]
    await share(http, auth_headers, 'agents', agent.id, scopes[1].id, include_descendants=False)
    assert not await access.can_use_agent(uid, agent.id)
    # Narrowing Agent use does not silently narrow previously authorized dependencies.
    assert skill.id in await access.accessible_capability_ids(uid)


async def test_knowledge_branch_download_and_explicit_narrowing(graph, auth_headers, db_session):
    http, _, uid, _, _, scopes = graph
    kb = KnowledgeBase(id=str(uuid.uuid4()), name='Library', scope_id=scopes[0].id, runtime_id='library', runtime_owner_id='owner')
    db_session.add(kb)
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Child', parent=scopes[1])
    await db_session.delete(await db_session.get(ScopeMembership, (scopes[1].id, uid)))
    db_session.add(ScopeMembership(scope_id=child.id, user_id=uid))
    await db_session.commit()
    await share(http, auth_headers, 'knowledge-bases', kb.id, scopes[1].id,
                include_descendants=True, allow_download=True)
    access = KnowledgeAccess(db_session)
    assert kb.id in await access.accessible_ids(uid, download=True)
    assert kb.id in await access.bindable_ids(child)
    await share(http, auth_headers, 'knowledge-bases', kb.id, scopes[1].id,
                include_descendants=False, allow_download=False)
    assert kb.id not in await access.accessible_ids(uid)
    assert kb.id not in await access.accessible_ids(uid, download=True)


async def test_ancestor_branch_dependency_needs_no_duplicate_grant(graph, auth_headers, db_session):
    http, _, _, agent, skill, scopes = graph
    await share(http, auth_headers, 'skills', skill.id, scopes[1].id, include_descendants=True)
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Child', parent=scopes[1])
    await db_session.commit()
    plan = await share(http, auth_headers, 'agents', agent.id, child.id, include_descendants=True)
    assert plan['skills'] == []
    assert not await db_session.scalar(select(ResourceShare.id).where(ResourceShare.capability_id == skill.id,
        ResourceShare.target_scope_id == child.id))


async def test_confirmation_detects_tree_change_and_grant_change(graph, auth_headers, db_session):
    http, _, _, agent, skill, scopes = graph
    url = f'/api/sharing/agents/{agent.id}'
    body = {'target_scope_id': scopes[1].id, 'include_descendants': True}
    plan = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    root = await get_root_scope(db_session)
    root.tree_version += 1
    await db_session.commit()
    result = await http.post(url, headers=auth_headers, json={**body, 'confirmation': plan['confirmation']})
    assert result.status_code == 409
    plan = (await http.post(url + '/preview', headers=auth_headers, json=body)).json()
    await grant(db_session, 'agents', agent.id, scopes[2].id, None, include_descendants=False)
    await db_session.commit()
    result = await http.post(url, headers=auth_headers, json={**body, 'confirmation': plan['confirmation']})
    assert result.status_code == 409


def test_share_request_accepts_explicit_department_inheritance():
    from app.api.routers.sharing import ShareRequest
    assert ShareRequest(target_scope_id='department', include_descendants=True).include_descendants is True


@pytest.mark.parametrize('module_name', ['app.services.resource_sharing', 'workmate_product.application.resource_sharing'])
async def test_dependency_upgrade_never_shrinks_existing_branch_or_download(graph, db_session, module_name):
    import importlib
    module = importlib.import_module(module_name)
    _, _, _, agent, skill, scopes = graph
    owner = agent.created_by
    kb = KnowledgeBase(id=str(uuid.uuid4()), name='Library', scope_id=scopes[0].id,
                       runtime_id='library', runtime_owner_id='owner')
    db_session.add(kb)
    agent.knowledge_config = {'knowledge_base_ids': [kb.id]}
    await db_session.flush()
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[0].id, owner)
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[1].id, owner,
                       allow_download=False, include_descendants=False)
    await module.grant(db_session, 'skills', skill.id, scopes[1].id, owner, include_descendants=False)
    await db_session.commit()
    await db_session.refresh(agent)
    plan = await module.build_plan(db_session, AccessDecisionService(db_session), owner, 'agents', agent,
                                   scopes[1].id, include_descendants=True)
    assert [item['id'] for item in plan['skills']] == [skill.id]
    assert [item['id'] for item in plan['knowledge_bases']] == [kb.id]
    await module.apply_plan(db_session, plan, 'agents', owner)
    await db_session.commit()
    kb_grant = await db_session.scalar(select(ResourceShare).where(ResourceShare.knowledge_base_id == kb.id,
                                                                   ResourceShare.target_scope_id == scopes[1].id))
    assert kb_grant.include_descendants and not kb_grant.allow_download
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[1].id, owner,
                       include_descendants=True, allow_download=True)
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[1].id, owner,
                       include_descendants=False, allow_download=False, preserve_existing=True)
    assert kb_grant.include_descendants and kb_grant.allow_download
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[1].id, owner,
                       include_descendants=False, allow_download=False)
    assert not kb_grant.include_descendants and not kb_grant.allow_download


async def test_own_sources_do_not_expose_other_departments_and_catalog_matches_branch(graph, auth_headers, db_session):
    from workmate_product.persistence.repositories.catalog_status import _skill_grant
    from workmate_product.persistence.models import CapabilityInstallation
    http, headers, uid, agent, skill, scopes = graph
    await share(http, auth_headers, 'agents', agent.id, scopes[1].id, include_descendants=True)
    await share(http, auth_headers, 'agents', agent.id, scopes[2].id, include_descendants=True)
    child = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Child', parent=scopes[1])
    await db_session.delete(await db_session.get(ScopeMembership, (scopes[1].id, uid)))
    db_session.add(ScopeMembership(scope_id=child.id, user_id=uid))
    await db_session.commit()
    result = (await http.get(f'/api/sharing/agents/{agent.id}/access', headers=headers)).json()
    assert [source['target_scope_id'] for source in result['sources']] == [scopes[1].id]
    assert await db_session.scalar(select(CapabilityInstallation.id).where(
        CapabilityInstallation.id == skill.id, _skill_grant(uid))) == skill.id
    assert (await http.get(f'/api/sharing/agents/{agent.id}', headers=headers)).status_code == 403
    outsider = await http.post('/api/auth/register', json={'email': 'outsider@example.com',
        'password': 'password123', 'display_name': 'Outsider'})
    outside_headers = {'Authorization': 'Bearer ' + outsider.json()['access_token']}
    assert (await http.get(f'/api/sharing/agents/{agent.id}/access', headers=outside_headers)).status_code == 404


async def test_org_default_and_legacy_false_remain_distinct(graph, auth_headers, db_session):
    http, _, _, agent, _, _ = graph
    root = await get_root_scope(db_session)
    preview = await http.post(f'/api/sharing/agents/{agent.id}/preview', headers=auth_headers,
                              json={'target_scope_id': root.id})
    assert preview.json()['include_descendants'] is True
    await share(http, auth_headers, 'agents', agent.id, root.id, include_descendants=False)
    response = (await http.get(f'/api/sharing/agents/{agent.id}', headers=auth_headers)).json()
    assert next(grant for grant in response['grants'] if grant['target_scope_id'] == root.id)['include_descendants'] is False
    assert all('parent_id' in target for target in response['targets'])


@pytest.mark.parametrize('module_name', ['app.services.resource_sharing', 'workmate_product.application.resource_sharing'])
async def test_agent_branch_cannot_implicitly_expand_existing_direct_download(graph, auth_headers, db_session, module_name):
    import importlib
    module = importlib.import_module(module_name)
    http, _, _, agent, _, scopes = graph
    kb = KnowledgeBase(id=str(uuid.uuid4()), name='Restricted downloads', scope_id=scopes[0].id,
                       runtime_id='library', runtime_owner_id='owner')
    db_session.add(kb)
    agent.knowledge_config = {'knowledge_base_ids': [kb.id]}
    await db_session.flush()
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[0].id, agent.created_by)
    await module.grant(db_session, 'knowledge-bases', kb.id, scopes[1].id, agent.created_by,
                       include_descendants=False, allow_download=True)
    await db_session.commit()
    await db_session.refresh(agent)
    plan = await module.build_plan(db_session, AccessDecisionService(db_session), agent.created_by,
                                   'agents', agent, scopes[1].id, include_descendants=True)
    assert any('下载权限' in blocker for blocker in plan['blockers'])
    response = await http.post(f'/api/sharing/agents/{agent.id}', headers=auth_headers,
        json={'target_scope_id': scopes[1].id, 'include_descendants': True, 'confirmation': plan['confirmation']})
    assert response.status_code == 409
    existing = await db_session.scalar(select(ResourceShare).where(ResourceShare.knowledge_base_id == kb.id,
                                                                  ResourceShare.target_scope_id == scopes[1].id))
    assert existing.allow_download and not existing.include_descendants
    with pytest.raises(module.HTTPException) as error:
        await module.grant(db_session, 'knowledge-bases', kb.id, scopes[1].id, agent.created_by,
                           include_descendants=True, preserve_existing=True)
    assert error.value.status_code == 409


async def test_skill_and_knowledge_access_sources_use_callers_memberships(graph, auth_headers, db_session):
    http, headers, uid, _, skill, scopes = graph
    await share(http, auth_headers, 'skills', skill.id, scopes[1].id, include_descendants=True)
    skill_sources = await http.get(f'/api/sharing/skills/{skill.id}/access', headers=headers)
    assert skill_sources.status_code == 200
    assert skill_sources.json()['can_use'] is True
    assert skill_sources.json()['sources'][0]['target_kind'] == 'department'
    assert skill_sources.json()['sources'][0]['inherited'] is False
    kb = KnowledgeBase(id=str(uuid.uuid4()), name='Library', scope_id=scopes[0].id,
                       runtime_id='library', runtime_owner_id='owner')
    db_session.add(kb)
    await db_session.commit()
    await share(http, auth_headers, 'knowledge-bases', kb.id, scopes[1].id, allow_download=True)
    knowledge_sources = await http.get(f'/api/sharing/knowledge-bases/{kb.id}/access', headers=headers)
    assert knowledge_sources.status_code == 200
    assert knowledge_sources.json()['can_use'] and knowledge_sources.json()['allow_download']
    assert knowledge_sources.json()['sources'][0]['member_scope_ids'] == [scopes[1].id]
