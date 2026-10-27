"""Resource pagination applies authorization and filters before totals/limits."""
import uuid

import pytest

from workmate_product.persistence.models import (
    Agent,
    CapabilityInstallation,
    ResourceShare,
    ScopeKind,
    Visibility,
)
from workmate_product.persistence.models import KnowledgeBase
from app.services.bootstrap import create_scope, get_personal_scope, get_root_scope


@pytest.mark.parametrize('path', ['/agents', '/skills', '/knowledge-bases', '/mcps'])
async def test_resource_pages_have_one_shape_with_or_without_query(client_and_app, auth_headers, path):
    http, _ = client_and_app
    assert (await http.get('/api' + path, headers=auth_headers)).json() == {'items': [], 'total': 0, 'page': 1, 'page_size': 12}
    response = await http.get('/api' + path + '?page=999&page_size=2', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {'items': [], 'total': 0, 'page': 999, 'page_size': 2}
    for query in ('page=0', 'page_size=101', 'page_size=0', 'q=' + 'x' * 201):
        assert (await http.get('/api' + path + '?' + query, headers=auth_headers)).status_code == 422


@pytest.mark.parametrize('kind,path,filter_query', [
    ('agent', '/agents', 'category=shared'),
    ('skill', '/skills', 'source=uploaded'),
    ('kb', '/knowledge-bases', 'view=shared'),
    ('mcp', '/mcps', ''),
])
async def test_resource_total_is_visible_filtered_and_page_order_is_stable(
    client_and_app, auth_headers, db_session, kind, path, filter_query,
):
    http, _ = client_and_app
    db = db_session
    root = await get_root_scope(db)
    me = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    registered = await http.post('/api/auth/register', json={
        'email': f'{kind}-page@example.com', 'password': 'password123', 'display_name': 'Reader',
    })
    headers = {'Authorization': 'Bearer ' + registered.json()['access_token']}
    hidden_scope = await create_scope(db, kind=ScopeKind.DEPARTMENT, name="Hidden", parent=root)
    # Three readable matches, one hidden and one readable non-match.
    for index in range(5):
        rid = f'{kind}-page-{index}'
        name = 'needle ' + str(index) if index < 4 else 'other'
        if kind == 'agent':
            row = Agent(id=rid, slug=rid, display_name=name, scope_id=root.id,
                visibility=Visibility.PUBLIC, created_by=me)
        elif kind == 'kb':
            row = KnowledgeBase(id=rid, scope_id=root.id, name=name,
                runtime_owner_id='root', runtime_id=rid)
        else:
            row = CapabilityInstallation(id=rid, scope_id=hidden_scope.id if kind == "mcp" and index == 3 else root.id, name=name,
                capability_type=kind,
                metadata_={'hub_id': 'uploaded'})
        db.add(row)
        await db.flush()
        if index != 3:
            # Skill and MCP ownership both require an explicit use grant.
            column = {'agent': 'agent_id', 'kb': 'knowledge_base_id', 'skill': 'capability_id', 'mcp': 'capability_id'}[kind]
            db.add(ResourceShare(id=str(uuid.uuid4()), target_scope_id=root.id, **{column: rid}))
    await db.commit()
    url = '/api' + path + '?page=1&page_size=2&q=needle&' + filter_query
    response = await http.get(url, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    expected = 3
    assert result['total'] == expected
    assert len(result['items']) == 2
    second = (await http.get(url.replace('page=1', 'page=2'), headers=headers)).json()
    assert second['total'] == expected
    assert len(second['items']) == expected - 2
    assert not ({row['id'] for row in result['items']} & {row['id'] for row in second['items']})
    assert (await http.get(url.replace('page=1', 'page=99'), headers=headers)).json()['items'] == []


async def test_agent_categories_and_skill_sources_filter_before_count(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    db = db_session
    uid = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    root = await get_root_scope(db)
    personal = await get_personal_scope(db, uid)
    for category, scope, system in [('mine', personal, None), ('shared', root, None), ('builtin', root, 'system-test')]:
        db.add(Agent(id=category, slug=category, display_name=category, scope_id=scope.id, system_key=system, created_by=uid))
    for source, hub in [('uploaded', 'uploaded'), ('builtin', 'workmate-builtin'), ('third-party', 'skillhub-cn')]:
        db.add(CapabilityInstallation(id=source, name=source, scope_id=root.id, capability_type='skill',
            metadata_={'hub_id': hub}))
    await db.commit()
    for category in ['mine', 'shared', 'builtin']:
        result = (await http.get(f'/api/agents?page=1&category={category}', headers=auth_headers)).json()
        assert result['total'] == 1 and result['items'][0]['id'] == category
    for source in ['uploaded', 'builtin', 'third-party']:
        result = (await http.get(f'/api/skills?page=1&source={source}', headers=auth_headers)).json()
        assert result['total'] == 1 and result['items'][0]['id'] == source
    for path, query in [('/agents', 'category=invalid'), ('/skills', 'source=invalid'), ('/knowledge-bases', 'view=invalid')]:
        assert (await http.get('/api' + path + '?' + query, headers=auth_headers)).status_code == 422


async def test_knowledge_views_follow_user_relationships(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    db = db_session
    admin_id = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    root = await get_root_scope(db)
    department = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Research', parent=root)
    admin_personal = await get_personal_scope(db, admin_id)
    await db.commit()
    registered = await http.post('/api/auth/register', json={
        'email': 'knowledge-views@example.com', 'password': 'password123', 'display_name': 'Reader',
    })
    reader_headers = {'Authorization': 'Bearer ' + registered.json()['access_token']}
    reader_id = registered.json()['user']['id']
    reader_personal = await get_personal_scope(db, reader_id)
    rows = [
        KnowledgeBase(id='admin-personal-kb', scope_id=admin_personal.id, name='Admin personal', runtime_owner_id=admin_id, runtime_id='admin-personal'),
        KnowledgeBase(id='reader-personal-kb', scope_id=reader_personal.id, name='Reader personal', runtime_owner_id=reader_id, runtime_id='reader-personal'),
        KnowledgeBase(id='shared-kb', scope_id=root.id, name='Shared', runtime_owner_id='root', runtime_id='shared'),
        KnowledgeBase(id='company-managed-kb', scope_id=root.id, name='Company managed', runtime_owner_id='root', runtime_id='company-managed'),
        KnowledgeBase(id='department-managed-kb', scope_id=department.id, name='Department managed', runtime_owner_id='department', runtime_id='department-managed'),
    ]
    db.add_all(rows)
    await db.flush()
    db.add(ResourceShare(id=str(uuid.uuid4()), target_scope_id=root.id, knowledge_base_id='shared-kb'))
    await db.commit()

    async def ids(headers, view):
        result = (await http.get(f'/api/knowledge-bases?page=1&view={view}', headers=headers)).json()
        return {item['id'] for item in result['items']}

    assert await ids(reader_headers, 'personal') == {'reader-personal-kb'}
    assert await ids(reader_headers, 'shared') == {'shared-kb'}
    assert await ids(reader_headers, 'managed') == set()
    assert await ids(auth_headers, 'personal') == {'admin-personal-kb'}
    assert await ids(auth_headers, 'managed') == {
        'shared-kb', 'company-managed-kb', 'department-managed-kb',
    }
    hidden_scope_search = (await http.get(
        '/api/knowledge-bases?page=1&view=managed&q=Research', headers=auth_headers,
    )).json()
    assert hidden_scope_search['total'] == 0


async def test_knowledge_detail_refresh_checks_visibility(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    uid = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    scope = await get_personal_scope(db_session, uid)
    kb = KnowledgeBase(id='detail-library', scope_id=scope.id, name='Detail', runtime_owner_id=uid, runtime_id='detail')
    db_session.add(kb)
    await db_session.commit()
    response = await http.get('/api/knowledge-bases/detail-library', headers=auth_headers)
    assert response.status_code == 200 and response.json()['id'] == kb.id
    registered = await http.post('/api/auth/register', json={'email': 'detail-reader@example.com', 'password': 'password123', 'display_name': 'Reader'})
    headers = {'Authorization': 'Bearer ' + registered.json()['access_token']}
    assert (await http.get('/api/knowledge-bases/detail-library', headers=headers)).status_code == 404


@pytest.mark.parametrize('kind,path', [('agent', '/agents')])
async def test_scope_name_search_filters_before_total_and_page(
    client_and_app, auth_headers, db_session, kind, path,
):
    http, _ = client_and_app
    db = db_session
    root = await get_root_scope(db)
    department = await create_scope(db, kind=ScopeKind.DEPARTMENT, name='Finance needle', parent=root)
    for index, scope in enumerate([department, department, root]):
        rid = f'{kind}-scope-search-{index}'
        if kind == 'agent':
            row = Agent(id=rid, slug=rid, display_name='Resource', scope_id=scope.id)
        else:
            row = KnowledgeBase(id=rid, name='Resource', scope_id=scope.id,
                runtime_owner_id='owner', runtime_id=rid)
        db.add(row)
    await db.commit()
    url = '/api' + path + '?q=Finance&page_size=1&page='
    first = (await http.get(url + '1', headers=auth_headers)).json()
    second = (await http.get(url + '2', headers=auth_headers)).json()
    assert first['total'] == second['total'] == 2
    assert len(first['items']) == len(second['items']) == 1
    assert first['items'][0]['id'] != second['items'][0]['id']
    assert all(item['scope_id'] == department.id for item in first['items'] + second['items'])


async def test_confirmation_rejects_legacy_list_body(client_and_app, auth_headers):
    http, _ = client_and_app
    response = await http.post('/api/tasks/missing/confirm', headers=auth_headers,
        json=[{'tool_call_id': 'tool', 'approved': True}])
    assert response.status_code == 422


async def test_default_page_is_bounded_and_second_page_keeps_remaining_agents(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    user_id = (await http.get('/api/auth/me', headers=auth_headers)).json()['id']
    personal = await get_personal_scope(db_session, user_id)
    db_session.add_all([
        Agent(id=f'bounded-{index}', slug=f'bounded-{index}', display_name=f'Agent {index}',
              scope_id=personal.id, created_by=user_id)
        for index in range(13)
    ])
    await db_session.commit()
    first = (await http.get('/api/agents', headers=auth_headers)).json()
    second = (await http.get('/api/agents?page=2', headers=auth_headers)).json()
    assert first['total'] == second['total'] == 13
    assert len(first['items']) == 12 and len(second['items']) == 1
    assert {item['id'] for item in first['items']}.isdisjoint(item['id'] for item in second['items'])


async def test_mcp_sources_filter_authorized_rows_before_pagination(client_and_app, auth_headers, db_session):
    http, _ = client_and_app
    db = db_session
    root = await get_root_scope(db)
    registered = await http.post('/api/auth/register', json={
        'email': 'mcp-source-reader@example.com', 'password': 'password123', 'display_name': 'Reader',
    })
    reader = {'Authorization': 'Bearer ' + registered.json()['access_token']}
    expected = {'custom': set(), 'catalog': set()}
    for source, hub in [('custom', 'workmate-custom-http'), ('catalog', 'fixture-catalog')]:
        for index in range(5):
            identifier = f'mcp-{source}-{index}'
            # Null legacy provenance stays in the non-custom/catalog category.
            metadata = {} if source == 'catalog' and index == 0 else {'hub_id': hub}
            db.add(CapabilityInstallation(id=identifier, name='needle' if index < 4 else 'other',
                scope_id=root.id, capability_type='mcp', metadata_=metadata))
            await db.flush()
            if index != 3:
                db.add(ResourceShare(id=str(uuid.uuid4()), target_scope_id=root.id, capability_id=identifier))
                if index < 3:
                    expected[source].add(identifier)
    await db.commit()
    for source in ('custom', 'catalog'):
        collected = set()
        for page, size in [(1, 2), (2, 1), (3, 0)]:
            response = await http.get(f'/api/mcps?source={source}&q=needle&page_size=2&page={page}', headers=reader)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['total'] == 3
            assert len(result['items']) == size
            ids = {item['id'] for item in result['items']}
            assert not collected & ids
            collected |= ids
        assert collected == expected[source]
    assert (await http.get('/api/mcps?q=needle', headers=reader)).json()['total'] == 6
    for value in ('all', 'uploaded', 'invalid'):
        assert (await http.get('/api/mcps?source=' + value, headers=reader)).status_code == 422
