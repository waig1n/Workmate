"""Full management fields and independent permissions survive native cutover."""
import pytest
from test_native_agents import native_agents, agent_body
from test_durable_projection import projection_database


async def test_management_fields_and_failed_knowledge_update_are_atomic(native_agents, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr('app.api.routers.agents.knowledge_settings', lambda: SimpleNamespace(enabled=False))
    client, _, admin = native_agents
    body = agent_body(platform_tool_profile='workspace', include_descendants=False,
        tool_policy={'denied_groups':[], 'denied_tools':['write_file']},
        knowledge_config={'knowledge_base_ids':[], 'parameters':{'mode':'static', 'top_k':8}})
    created = await client.post('/api/agents', json=body)
    assert created.status_code == 201, created.text
    value = created.json()
    assert value['platform_tool_profile'] == 'workspace'
    assert value['knowledge_config']['parameters']['top_k'] == 8
    assert value['tool_policy']['denied_tools'] == ['write_file']
    assert value['include_descendants'] is False
    identifier = value['id']
    changed = await client.patch('/api/agents/' + identifier, json={
        'platform_tool_profile':'none', 'description':'Changed', 'visibility':'private',
        'include_descendants':True, 'tool_policy':{'denied_groups':[], 'denied_tools':[]},
        'knowledge_config':{'knowledge_base_ids':[], 'parameters':{'mode':'agentic', 'top_k':3}}})
    assert changed.status_code == 200, changed.text
    assert changed.json()['knowledge_config']['parameters']['top_k'] == 3
    assert changed.json()['description'] == 'Changed'
    assert changed.json()['platform_tool_profile'] == 'none'
    failed = await client.patch('/api/agents/' + identifier, json={
        'display_name':'Should not persist', 'knowledge_config':{'knowledge_base_ids':['missing']}})
    assert failed.status_code == 503, failed.text
    assert await admin.fetchval('SELECT display_name FROM agents WHERE id=$1', identifier) == 'Native Agent'
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='agent.update'") == 1
    duplicate = await client.post('/api/agents', json=body)
    assert duplicate.status_code == 409
    assert (await client.post('/api/agents/' + identifier + '/restore-default-prompt')).status_code == 404


async def test_delete_permission_does_not_require_viewing_private_agent(native_agents):
    client, _, admin = native_agents
    created = await client.post('/api/agents', json=agent_body(visibility='private'))
    identifier = created.json()['id']
    await admin.execute('UPDATE agents SET created_by=NULL WHERE id=$1', identifier)
    await admin.execute("DELETE FROM role_permissions WHERE permission_id <> 'agent.delete'")
    assert (await client.get('/api/agents/' + identifier)).status_code == 404
    deleted = await client.delete('/api/agents/' + identifier)
    assert deleted.status_code == 204, deleted.text


async def test_personal_agent_constraints_and_missing_agent(native_agents):
    from workmate_product.persistence.models import Scope, ScopeClosure
    client, sessions, _ = native_agents
    async with sessions.begin() as db:
        db.add(Scope(id='personal',kind='personal',name='Personal',normalized_name='personal',owner_user_id='u'))
        await db.flush()
        db.add(ScopeClosure(ancestor_id='personal',descendant_id='personal',depth=0))
    body = agent_body(scope_id=None, visibility='private')
    created = await client.post('/api/agents', json=body)
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    assert (await client.patch('/api/agents/' + identifier, json={'visibility':'public'})).status_code == 422
    assert (await client.patch('/api/agents/' + identifier, json={'platform_tool_profile':'workspace'})).status_code == 422
    assert (await client.post('/api/agents', json=agent_body(scope_id=None))).status_code == 422
    assert (await client.patch('/api/agents/missing', json={'description':'x'})).status_code == 404
    assert (await client.delete('/api/agents/missing')).status_code == 404
