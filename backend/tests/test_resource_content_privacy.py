"""Use grants expose public descriptions, never managed implementation text."""
import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select

from workmate_product.persistence.models import (
    Conversation,
    Permission,
    ResourceShare,
    Role,
    RoleBinding,
    RolePermission,
)
from app.services.bootstrap import get_root_scope
from test_capabilities import SKILL_CARD, _register
from workmate_product.persistence.models import Agent


async def _context(http, headers, db):
    scopes = (await http.get('/api/scopes/accessible', headers=headers)).json()
    root = next(scope for scope in scopes if scope['kind'] == 'organization')
    created = await http.post('/api/agents', headers=headers, json={
        'slug': 'privacy-agent', 'display_name': 'Privacy Agent',
        'scope_id': root['id'], 'visibility': 'public'})
    assert created.status_code == 201, created.text
    db.add(ResourceShare(id=str(uuid.uuid4()), agent_id=created.json()['id'],
        target_scope_id=root['id'], include_descendants=True))
    await db.commit()
    conversation = await http.post('/api/conversations', headers=headers,
        json={'agent_id': created.json()['id'], 'title': 'Privacy'})
    assert conversation.status_code == 201, conversation.text
    return root['id'], await db.get(Agent, created.json()['id']), conversation.json()['id']


async def grant_permission(db, user_id, scope_id, key):
    role = Role(id=str(uuid.uuid4()), key='privacy-' + str(uuid.uuid4()), display_name='Scoped editor')
    db.add(role)
    await db.flush()
    permission = await db.scalar(select(Permission).where(Permission.key == key))
    db.add(RolePermission(id=str(uuid.uuid4()), role_id=role.id, permission_id=permission.id))
    db.add(RoleBinding(user_id=user_id, role_id=role.id, scope_id=scope_id, include_descendants=False))
    await db.commit()


async def test_skill_content_requires_scoped_manage_and_catalog_cannot_bypass(
    client_and_app, auth_headers, fake_client, db_session,
):
    http, app = client_and_app
    root = await get_root_scope(db_session)
    fake_client.skill_catalog = [{**SKILL_CARD, "markdown": "PRIVATE SKILL"}]
    skill = (await http.post('/api/skills', headers=auth_headers,
        json={'hub_id': 'clawhub', 'card_id': 'c-1'})).json()
    reader, uid = await _register(http, 'skill-privacy@example.com')
    assert (await http.get('/api/skills/' + skill['id'] + '/description', headers=reader)).status_code == 404
    db_session.add(ResourceShare(id=str(uuid.uuid4()), capability_id=skill['id'], target_scope_id=root.id))
    await db_session.commit()
    catalog = app.state.skill_catalogs['clawhub']
    catalog.list_skills = AsyncMock(wraps=catalog.list_skills)
    visible = (await http.get('/api/skills', headers=reader)).json()['items'][0]
    assert visible['usable'] is True and visible['manageable'] is False
    assert (await http.get('/api/skills/' + skill['id'] + '/description', headers=reader)).status_code == 403
    assert (await http.get('/api/skills/missing/description', headers=reader)).status_code == 404
    assert (await http.get('/api/skills/catalog?hub_id=clawhub', headers=reader)).status_code == 403
    catalog.list_skills.assert_not_awaited()
    # A scope permission suffices; no global administrator role is required.
    await grant_permission(db_session, uid, root.id, 'skill.manage')
    response = await http.get('/api/skills/' + skill['id'] + '/description', headers=reader)
    assert response.status_code == 200 and 'PRIVATE SKILL' in response.json()['markdown']
    assert (await http.get('/api/skills/catalog?hub_id=clawhub', headers=reader)).status_code == 200


async def test_agent_prompt_redacted_across_lists_detail_and_conversations(
    client_and_app, auth_headers, db_session,
):
    http, _ = client_and_app
    root_id, agent, conversation_id = await _context(http, auth_headers, db_session)
    reader, uid = await _register(http, 'agent-privacy@example.com')
    agent.system_prompt = 'PRIVATE AGENT INSTRUCTIONS'
    agent.description = 'Public summary'
    conversation = await db_session.get(Conversation, conversation_id)
    conversation.user_id = uid
    await db_session.commit()
    detail = (await http.get('/api/agents/' + agent.id, headers=reader)).json()
    default_page = (await http.get('/api/agents', headers=reader)).json()['items'][0]
    paged = (await http.get('/api/agents?page=1', headers=reader)).json()['items'][0]
    conv = (await http.get('/api/conversations/' + conversation.id, headers=reader)).json()['agent']
    conversations = (await http.get('/api/conversations', headers=reader)).json()[0]['agent']
    for view in [detail, default_page, paged, conv, conversations]:
        assert view['system_prompt'] == ''
        assert view['description'] == 'Public summary'
        assert view['actions']['use'] is True
        assert view['actions']['update'] is False
    await grant_permission(db_session, uid, root_id, 'agent.update')
    editable = (await http.get('/api/agents/' + agent.id, headers=reader)).json()
    assert editable['system_prompt'] == 'PRIVATE AGENT INSTRUCTIONS'
    assert editable['actions']['update'] is True


async def test_personal_owner_keeps_prompt_without_management_role(client_and_app, auth_headers):
    http, _ = client_and_app
    reader, _ = await _register(http, 'personal-privacy@example.com')
    created = await http.post('/api/agents', headers=reader, json={
        'slug': 'personal-private', 'display_name': 'Personal', 'system_prompt': 'OWNER INSTRUCTIONS',
    })
    assert created.status_code == 201, created.text
    assert created.json()['system_prompt'] == 'OWNER INSTRUCTIONS'
    detail = await http.get('/api/agents/' + created.json()['id'], headers=reader)
    assert detail.json()['system_prompt'] == 'OWNER INSTRUCTIONS'
