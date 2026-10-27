"""Department structure writes require a current tree version."""

from app.db import get_sessionmaker
from workmate_product.persistence.models import (
    Agent,
    AgentCapabilityBinding,
    CapabilityInstallation,
    Scope,
    ResourceShare,
    KnowledgeBase,
)


async def test_stale_tree_edit_is_rejected_without_overwriting(client_and_app, auth_headers):
    http, _ = client_and_app
    root = (await http.get('/api/admin/scopes/tree', headers=auth_headers)).json()[0]
    dept = (await http.post('/api/admin/scopes', headers=auth_headers,
                           json={'parent_id': root['id'], 'name': 'Finance'})).json()
    version = dept['tree_version']
    first = await http.patch(f"/api/admin/scopes/{dept['id']}", headers=auth_headers,
                             json={'name': 'Finance new', 'expected_tree_version': version})
    assert first.status_code == 200, first.text
    second = await http.patch(f"/api/admin/scopes/{dept['id']}", headers=auth_headers,
                              json={'name': 'Stale', 'expected_tree_version': version})
    assert second.status_code == 409
    assert second.json()['detail']['code'] == 'version_conflict'
    assert (await http.patch(f"/api/admin/scopes/{dept['id']}", headers=auth_headers,
                             json={'name': 'Missing version'})).status_code == 422


async def test_move_preview_is_read_only_and_counts_subtree(client_and_app, auth_headers):
    http, _ = client_and_app
    root = (await http.get('/api/admin/scopes/tree', headers=auth_headers)).json()[0]
    nodes = []
    for name in ('Left', 'Right'):
        nodes.append((await http.post('/api/admin/scopes', headers=auth_headers,
                                     json={'parent_id': root['id'], 'name': name})).json())
    preview = await http.post(f"/api/admin/scopes/{nodes[0]['id']}/move-preview", headers=auth_headers,
                              json={'new_parent_id': nodes[1]['id']})
    assert preview.status_code == 200, preview.text
    assert preview.json()['descendant_count'] == 1
    assert preview.json()['capability_blockers'] == 0
    tree = (await http.get('/api/admin/scopes/tree', headers=auth_headers)).json()[0]
    assert tree['tree_version'] == preview.json()['tree_version']
    assert len(tree['children']) == 2


async def test_move_rejects_mcp_that_would_lose_explicit_branch_grant(client_and_app, auth_headers):
    http, _ = client_and_app
    root = (await http.get('/api/admin/scopes/tree', headers=auth_headers)).json()[0]
    left = (await http.post('/api/admin/scopes', headers=auth_headers,
                           json={'parent_id': root['id'], 'name': 'Left'})).json()
    child = (await http.post('/api/admin/scopes', headers=auth_headers,
                            json={'parent_id': left['id'], 'name': 'Child'})).json()
    right = (await http.post('/api/admin/scopes', headers=auth_headers,
                            json={'parent_id': root['id'], 'name': 'Right'})).json()
    async with get_sessionmaker()() as db:
        db.add(Agent(id='move-agent', slug='move-agent', display_name='Move Agent', scope_id=child['id']))
        db.add(CapabilityInstallation(id='move-capability', capability_type='mcp', scope_id=left['id'],
              name='report'))
        await db.flush()
        db.add(AgentCapabilityBinding(agent_id='move-agent', capability_installation_id='move-capability'))
        db.add(ResourceShare(id='move-mcp-grant', capability_id='move-capability', target_scope_id=left['id'], include_descendants=True))
        await db.commit()
    preview = (await http.post(f"/api/admin/scopes/{child['id']}/move-preview", headers=auth_headers,
                              json={'new_parent_id': right['id']})).json()
    assert preview['capability_blockers'] == 1
    assert preview['shared_resource_changes']['mcps'] == {'gained': 0, 'lost': 1}
    response = await http.post(f"/api/admin/scopes/{child['id']}/move", headers=auth_headers,
                              json={'new_parent_id': right['id'], 'expected_tree_version': preview['tree_version']})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'scope_capability_conflict'
    async with get_sessionmaker()() as db:
        assert (await db.get(Scope, child['id'])).parent_id == left['id']
        assert (await db.get(Scope, root['id'])).tree_version == preview['tree_version']


async def _move_tree(http, headers):
    root = (await http.get('/api/admin/scopes/tree', headers=headers)).json()[0]
    nodes = {'root': root['id']}
    for name in ('left', 'right', 'child'):
        parent = nodes['left'] if name == 'child' else root['id']
        nodes[name] = (await http.post('/api/admin/scopes', headers=headers,
            json={'parent_id': parent, 'name': name})).json()['id']
    return nodes


async def test_move_counts_union_of_grants_and_downloads(client_and_app, auth_headers):
    http, _ = client_and_app
    ids = await _move_tree(http, auth_headers)
    async with get_sessionmaker()() as db:
        db.add(CapabilityInstallation(id='skill', capability_type='skill', scope_id=ids['root'], name='Skill'))
        db.add(CapabilityInstallation(id='mcp-gained', capability_type='mcp', scope_id=ids['root'], name='MCP'))
        db.add(KnowledgeBase(id='kb', scope_id=ids['root'], name='KB', runtime_id='kb', runtime_owner_id='owner'))
        db.add(Agent(id='shared', slug='shared', display_name='Shared', scope_id=ids['root'], visibility='public'))
        await db.flush()
        db.add_all([
            ResourceShare(id='s-left', capability_id='skill', target_scope_id=ids['left'], include_descendants=True),
            ResourceShare(id='s-direct', capability_id='skill', target_scope_id=ids['child']),
            ResourceShare(id='kb-root', knowledge_base_id='kb', target_scope_id=ids['root'], include_descendants=True),
            ResourceShare(id='kb-left', knowledge_base_id='kb', target_scope_id=ids['left'], include_descendants=True, allow_download=True),
            ResourceShare(id='mcp-right', capability_id='mcp-gained', target_scope_id=ids['right'], include_descendants=True),
            ResourceShare(id='a-right', agent_id='shared', target_scope_id=ids['right'], include_descendants=True),
        ])
        await db.commit()
    response = await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})
    assert response.status_code == 200, response.text
    assert response.json()['shared_resource_changes'] == {
        'agents': {'gained': 1, 'lost': 0}, 'mcps': {'gained': 1, 'lost': 0}, 'skills': {'gained': 0, 'lost': 0},
        'knowledge_bases': {'gained': 0, 'lost': 0}, 'knowledge_downloads': {'gained': 0, 'lost': 1},
    }


async def test_move_blocks_external_agent_shared_target_losing_dependency(client_and_app, auth_headers):
    http, _ = client_and_app
    ids = await _move_tree(http, auth_headers)
    async with get_sessionmaker()() as db:
        db.add(Agent(id='external', slug='external', display_name='External', scope_id=ids['root'], visibility='public',
                     knowledge_config={'knowledge_base_ids': ['kb']}))
        db.add(KnowledgeBase(id='kb', scope_id=ids['root'], name='KB', runtime_id='kb', runtime_owner_id='owner'))
        await db.flush()
        db.add_all([
            ResourceShare(id='kb-source', knowledge_base_id='kb', target_scope_id=ids['root']),
            ResourceShare(id='kb-left', knowledge_base_id='kb', target_scope_id=ids['left'], include_descendants=True),
            ResourceShare(id='a-target', agent_id='external', target_scope_id=ids['child'], include_descendants=True),
        ])
        await db.commit()
    preview = (await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})).json()
    assert preview['capability_blockers'] == 1
    response = await http.post(f"/api/admin/scopes/{ids['child']}/move", headers=auth_headers,
        json={'new_parent_id': ids['right'], 'expected_tree_version': preview['tree_version']})
    assert response.status_code == 409, response.text


async def test_move_preserves_direct_skill_grant_and_ignores_preexisting_damage(client_and_app, auth_headers):
    http, _ = client_and_app
    ids = await _move_tree(http, auth_headers)
    async with get_sessionmaker()() as db:
        db.add(Agent(id='local', slug='local', display_name='Local', scope_id=ids['child']))
        for name in ('valid', 'broken'):
            db.add(CapabilityInstallation(id=name, capability_type='skill', scope_id=ids['left'], name=name))
        await db.flush()
        db.add_all([AgentCapabilityBinding(agent_id='local', capability_installation_id=name) for name in ('valid', 'broken')])
        db.add(ResourceShare(id='valid-grant', capability_id='valid', target_scope_id=ids['child']))
        await db.commit()
    preview = (await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})).json()
    assert preview['capability_blockers'] == 0
    response = await http.post(f"/api/admin/scopes/{ids['child']}/move", headers=auth_headers,
        json={'new_parent_id': ids['right'], 'expected_tree_version': preview['tree_version']})
    assert response.status_code == 200, response.text


async def test_move_blocks_source_skill_and_knowledge_losses(client_and_app, auth_headers):
    http, _ = client_and_app
    ids = await _move_tree(http, auth_headers)
    async with get_sessionmaker()() as db:
        db.add(Agent(id='local', slug='local', display_name='Local', scope_id=ids['child'],
                     knowledge_config={'knowledge_base_ids': ['kb']}))
        db.add(CapabilityInstallation(id='skill', capability_type='skill', scope_id=ids['root'], name='Skill'))
        db.add(KnowledgeBase(id='kb', scope_id=ids['root'], name='KB', runtime_id='kb', runtime_owner_id='owner'))
        await db.flush()
        db.add(AgentCapabilityBinding(agent_id='local', capability_installation_id='skill'))
        db.add_all([
            ResourceShare(id='skill-grant', capability_id='skill', target_scope_id=ids['left'], include_descendants=True),
            ResourceShare(id='kb-grant', knowledge_base_id='kb', target_scope_id=ids['left'], include_descendants=True),
        ])
        await db.commit()
    preview = (await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})).json()
    assert preview['capability_blockers'] == 2
    assert preview['shared_resource_changes']['skills'] == {'gained': 0, 'lost': 1}
    assert preview['shared_resource_changes']['knowledge_bases'] == {'gained': 0, 'lost': 1}


async def test_move_uses_continuous_branch_dependency_coverage(client_and_app, auth_headers):
    """A direct grant cannot replace branch coverage, even for a current leaf."""
    http, _ = client_and_app
    ids = await _move_tree(http, auth_headers)
    async with get_sessionmaker()() as db:
        db.add(Agent(id='external', slug='external', display_name='External', scope_id=ids['root'], visibility='public'))
        db.add(CapabilityInstallation(id='skill', capability_type='skill', scope_id=ids['root'], name='Skill'))
        await db.flush()
        db.add(AgentCapabilityBinding(agent_id='external', capability_installation_id='skill'))
        db.add_all([
            ResourceShare(id='skill-source', capability_id='skill', target_scope_id=ids['root']),
            ResourceShare(id='skill-branch', capability_id='skill', target_scope_id=ids['left'], include_descendants=True),
            ResourceShare(id='skill-direct', capability_id='skill', target_scope_id=ids['child']),
            ResourceShare(id='a-target', agent_id='external', target_scope_id=ids['child'], include_descendants=True),
        ])
        await db.commit()
    preview = (await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})).json()
    assert preview['shared_resource_changes']['skills'] == {'gained': 0, 'lost': 0}
    assert preview['capability_blockers'] == 1
    # An alternate whole-branch grant remains effective when its branch moves.
    async with get_sessionmaker()() as db:
        (await db.get(ResourceShare, 'skill-direct')).include_descendants = True
        await db.commit()
    refreshed = (await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})).json()
    assert refreshed['capability_blockers'] == 0


async def test_move_keeps_mcp_direct_grant_outside_source_ancestry(client_and_app, auth_headers):
    http, _ = client_and_app
    ids = await _move_tree(http, auth_headers)
    async with get_sessionmaker()() as db:
        db.add(Agent(id='mcp-local', slug='mcp-local', display_name='Local', scope_id=ids['child']))
        db.add(CapabilityInstallation(id='mcp-direct', capability_type='mcp', scope_id=ids['left'], name='MCP'))
        await db.flush()
        db.add(AgentCapabilityBinding(agent_id='mcp-local', capability_installation_id='mcp-direct'))
        db.add(ResourceShare(id='mcp-direct-share', capability_id='mcp-direct', target_scope_id=ids['child']))
        await db.commit()
    preview = (await http.post(f"/api/admin/scopes/{ids['child']}/move-preview", headers=auth_headers,
        json={'new_parent_id': ids['right']})).json()
    assert preview['capability_blockers'] == 0
    assert preview['shared_resource_changes']['mcps'] == {'gained': 0, 'lost': 0}
    response = await http.post(f"/api/admin/scopes/{ids['child']}/move", headers=auth_headers,
        json={'new_parent_id': ids['right'], 'expected_tree_version': preview['tree_version']})
    assert response.status_code == 200, response.text
