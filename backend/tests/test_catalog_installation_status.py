"""Catalog status must not depend on whichever registry page is displayed."""
import uuid

import pytest
from sqlalchemy import event

from workmate_product.persistence.models import CapabilityInstallation, ResourceShare, ScopeKind
from app.services.bootstrap import create_scope, get_root_scope

pytestmark = pytest.mark.asyncio


async def _seed(db, scope_id, kind, name, hub, card):
    item = CapabilityInstallation(id=str(uuid.uuid4()), scope_id=scope_id,
        capability_type=kind, name=name,
         metadata_={'hub_id': hub, 'card_id': card})
    db.add(item)
    await db.commit()
    return item


async def _member(http):
    response = await http.post('/api/auth/register', json={
        'email': 'catalog-status-member@test.dev', 'password': 'password123', 'display_name': 'Member'})
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


async def test_skill_catalog_reports_status_to_managers_and_rejects_use_only_members(
    client_and_app, auth_headers, db_session, fake_client,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    by_card = await _seed(db_session, root.id, 'skill', 'renamed', 'clawhub', 'card-a')
    await _seed(db_session, root.id, 'skill', 'same-name', 'uploaded', 'archive')
    fake_client.skill_catalog = [
        {'id': 'card-a', 'name': 'original'},
        {'id': 'card-b', 'name': 'same-name'},
        {'id': 'card-c', 'name': 'new'},
    ]
    response = await http.get('/api/skills/catalog?hub_id=clawhub', headers=auth_headers)
    assert [card['installed'] for card in response.json()['cards']] == [True, True, False]
    member = await _member(http)
    hidden = await http.get('/api/skills/catalog?hub_id=clawhub', headers=member)
    assert hidden.status_code == 403
    db_session.add(ResourceShare(id=str(uuid.uuid4()), capability_id=by_card.id,
        target_scope_id=root.id, include_descendants=True))
    await db_session.commit()
    shared = await http.get('/api/skills/catalog?hub_id=clawhub', headers=member)
    assert shared.status_code == 403


async def test_mcp_catalog_status_is_exact_scope_and_requires_management(
    client_and_app, auth_headers, db_session, fake_client,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    department = await create_scope(db_session, kind=ScopeKind.DEPARTMENT, name='Target', parent=root)
    await db_session.commit()
    await _seed(db_session, root.id, 'mcp', 'root-only', 'github', 'root-card')
    await _seed(db_session, department.id, 'mcp', 'renamed', 'github', 'dept-card')
    fake_client.mcp_catalog = [{'id': 'root-card', 'name': 'root-only'}, {'id': 'dept-card', 'name': 'original'}, {'id': 'new', 'name': 'new'}]
    base = '/api/mcps/catalog?hub_id=github'
    legacy = await http.get(base, headers=auth_headers)
    assert all('installed' not in card for card in legacy.json()['cards'])
    scoped = await http.get(base + f'&scope_id={department.id}', headers=auth_headers)
    assert [card['installed'] for card in scoped.json()['cards']] == [False, True, False]
    member = await _member(http)
    denied = await http.get(base + f'&scope_id={department.id}', headers=member)
    assert denied.status_code == 403


async def test_status_checks_only_catalog_page_with_exists_projection(
    client_and_app, auth_headers, db_session, fake_client, monkeypatch,
):
    http, _ = client_and_app
    root = await get_root_scope(db_session)
    await _seed(db_session, root.id, 'skill', 'hit', 'hub', 'hit-card')
    from workmate_product.skills.catalog import SkillCard, SkillHubPage
    from product_catalog_fakes import SkillCatalog
    page = SkillHubPage(cards=[SkillCard(hub_id='hub', id='hit-card', name='hit'),
                              SkillCard(hub_id='hub', id='new', name='new')],
                        next_cursor='next-page')
    async def catalog(*args, **kwargs):
        return page
    hub = SkillCatalog(fake_client, 'hub')
    monkeypatch.setattr(hub, 'list_skills', catalog)
    client_and_app[1].state.skill_catalogs['hub'] = hub
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if 'capability_installations' in statement:
            statements.append(statement)
    engine = db_session.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        response = await http.get('/api/skills/catalog?hub_id=hub', headers=auth_headers)
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert response.status_code == 200
    assert response.json()['next_cursor'] == 'next-page'
    assert [card['installed'] for card in response.json()['cards']] == [True, False]
    assert len(statements) == 1
    assert statements[0].startswith('SELECT EXISTS')
    assert statements[0].count('EXISTS (SELECT') == 2
    assert all('installed' not in card.model_dump() for card in page.cards)
    page.cards = []
    empty = await http.get('/api/skills/catalog?hub_id=hub', headers=auth_headers)
    assert empty.json() == page.model_dump()
