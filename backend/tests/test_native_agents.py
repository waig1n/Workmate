"""Native Agent HTTP governance uses durable Product facts, never App sync."""
import pytest
import pytest_asyncio
from fastapi import Depends
from httpx import ASGITransport,AsyncClient
from app.auth import get_current_user,get_access_decision
from app.db import get_db
from workmate_product.persistence.models import User, CapabilityInstallation, ResourceShare
from app.services.access_decision import AccessDecisionService
from workmate_product.persistence.models import ScopeClosure,ScopeMembership,Role,Permission,RolePermission,RoleBinding
from test_durable_projection import projection_database  # noqa: F401


@pytest_asyncio.fixture
async def native_agents(projection_database):
    from app.main import build_app
    sessions,admin=projection_database
    permissions=['agent.view','agent.use','agent.create','agent.update','agent.delete','agent.publish','skill.manage']
    async with sessions.begin() as db:
        db.add(ScopeClosure(ancestor_id='scope',descendant_id='scope',depth=0))
        db.add(ScopeMembership(scope_id='scope',user_id='u'))
        db.add(Role(id='manager',key='manager',display_name='Manager'))
        for key in permissions: db.add(Permission(id=key,key=key))
        await db.flush()
        db.add(RoleBinding(user_id='u',scope_id='scope',role_id='manager'))
        for key in permissions: db.add(RolePermission(id=key,role_id='manager',permission_id=key))
    application=build_app()
    class ForbiddenRuntime:
        def __getattr__(self,name): raise AssertionError('Native Agent management called legacy App: '+name)
    application.state.runtime_client=ForbiddenRuntime()
    async def database():
        async with sessions() as db: yield db
    async def actor():
        async with sessions() as db: return await db.get(User,'u')
    async def access(db=Depends(get_db)): return AccessDecisionService(db)
    application.dependency_overrides[get_db]=database
    application.dependency_overrides[get_current_user]=actor
    application.dependency_overrides[get_access_decision]=access
    async with AsyncClient(transport=ASGITransport(application),base_url='http://test') as client:
        yield client,sessions,admin


async def install(client):
    import io
    import zipfile
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as package:
        package.writestr('SKILL.md', '---\nname: native-fixture\ndescription: Native fixture\n---\nUse this fixture.')
    response = await client.post('/api/skills/upload', data={'scope_id': 'scope'},
        files={'archive': ('native-fixture.zip', archive.getvalue(), 'application/zip')})
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    url = '/api/sharing/skills/' + identifier
    preview = await client.post(url + '/preview', json={'target_scope_id': 'scope'})
    assert preview.status_code == 200, preview.text
    shared = await client.post(url, json={'target_scope_id': 'scope',
                                         'confirmation': preview.json()['confirmation']})
    assert shared.status_code == 200, shared.text
    return identifier


def agent_body(**overrides):
    return {'slug':'native-agent','display_name':'Native Agent','scope_id':'scope','visibility':'public',
        'system_prompt':'My original instructions',**overrides}


async def test_native_create_patch_unbind_and_delete_agent_with_ready_skill(native_agents):
    client,sessions,admin=native_agents
    skill=await install(client)
    response=await client.post('/api/agents',json=agent_body(skill_binding_ids=[skill]))
    assert response.status_code==201,response.text
    agent=response.json()
    assert agent['skill_binding_ids']==[skill]
    assert agent['unavailable_capability_ids']==[]
    assert await admin.fetchval('SELECT artifact_digest FROM capability_skill_artifacts WHERE capability_id=$1',skill)
    updated=await client.patch('/api/agents/'+agent['id'],json={'display_name':'Edited Agent','system_prompt':'Administrator edited','skill_binding_ids':[]})
    assert updated.status_code==200,updated.text
    assert updated.json()['display_name']=='Edited Agent'
    assert updated.json()['system_prompt']=='Administrator edited'
    assert updated.json()['skill_binding_ids']==[]
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',agent['id'])==0
    assert (await client.delete('/api/agents/'+agent['id'])).status_code==204
    assert (await client.get('/api/agents/'+agent['id'])).status_code==404
    assert await admin.fetchval('SELECT count(*) FROM capability_installations WHERE id=$1',skill)==1


async def test_native_create_rejects_unready_skill_without_partial_agent(native_agents):
    client,sessions,admin=native_agents
    async with sessions.begin() as db:
        db.add(CapabilityInstallation(id='unready',capability_type='skill',scope_id='scope',
            name='unready',display_name='Unready',enabled=True,metadata_={}))
        await db.flush()
        db.add(ResourceShare(id='unready-grant',capability_id='unready',target_scope_id='scope'))
    response=await client.post('/api/agents',json=agent_body(skill_binding_ids=['unready']))
    assert response.status_code==422,response.text
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE slug='native-agent'")==0


async def test_native_agent_mutations_recheck_current_scope_permission(native_agents):
    client,sessions,admin=native_agents
    response=await client.post('/api/agents',json=agent_body())
    assert response.status_code==201,response.text
    identifier=response.json()['id']
    await admin.execute('DELETE FROM role_bindings')
    denied=await client.post('/api/agents',json=agent_body(slug='denied'))
    assert denied.status_code==403,denied.text
    changed=await client.patch('/api/agents/'+identifier,json={'system_prompt':'unauthorized'})
    assert changed.status_code==403,changed.text
    removed=await client.delete('/api/agents/'+identifier)
    assert removed.status_code==403,removed.text
    assert await admin.fetchval('SELECT system_prompt FROM agents WHERE id=$1',identifier)=='My original instructions'


async def test_native_workspace_assistant_prompt_restore_preserves_bindings_and_delete_protection(native_agents):
    from app.services.workspace_assistant import ensure_workspace_assistant
    from app.prompts import DEFAULT_WORKSPACE_ASSISTANT_PROMPT
    client,sessions,admin=native_agents
    async with sessions.begin() as db:
        assistant=await ensure_workspace_assistant(db)
        identifier=assistant.id
    changed=await client.patch('/api/agents/'+identifier,json={'system_prompt':'Administrator prompt','skill_binding_ids':[]})
    assert changed.status_code==200,changed.text
    restored=await client.post('/api/agents/'+identifier+'/restore-default-prompt')
    assert restored.status_code==200,restored.text
    assert restored.json()['system_prompt']==DEFAULT_WORKSPACE_ASSISTANT_PROMPT
    assert restored.json()['skill_binding_ids']==[]
    assert (await client.delete('/api/agents/'+identifier)).status_code==409


async def test_native_patch_unready_skill_preserves_existing_binding(native_agents):
    client,sessions,admin=native_agents
    skill=await install(client)
    created=await client.post('/api/agents',json=agent_body(skill_binding_ids=[skill]))
    assert created.status_code==201,created.text
    identifier=created.json()['id']
    async with sessions.begin() as db:
        db.add(CapabilityInstallation(id='unready',capability_type='skill',scope_id='scope',
            name='unready',display_name='Unready',enabled=True,metadata_={}))
        await db.flush()
        db.add(ResourceShare(id='unready-grant',capability_id='unready',target_scope_id='scope'))
    changed=await client.patch('/api/agents/'+identifier,json={'skill_binding_ids':['unready']})
    assert changed.status_code==422,changed.text
    assert await admin.fetchval('SELECT capability_installation_id FROM agent_capability_bindings WHERE agent_id=$1',identifier)==skill


async def test_concurrent_native_default_creation_preserves_single_scope_default(native_agents):
    import asyncio
    client,sessions,admin=native_agents
    results=await asyncio.wait_for(asyncio.gather(*(client.post('/api/agents',
        json=agent_body(slug='default-'+str(index),is_default=True)) for index in range(2))),10)
    assert [result.status_code for result in results]==[201,201],[result.text for result in results]
    identifiers={result.json()['id'] for result in results}
    assert await admin.fetchval('SELECT count(*) FROM agent_defaults')==1
    selected=await admin.fetchval('SELECT agent_id FROM agent_defaults')
    assert selected in identifiers
    assert (await client.delete('/api/agents/'+selected)).status_code==409
    cleared=await client.patch('/api/agents/'+selected,json={'is_default':False})
    assert cleared.status_code==200,cleared.text
    assert (await client.delete('/api/agents/'+selected)).status_code==204


async def test_native_delete_waits_for_both_gates_then_preserves_historical_agent(native_agents):
    client,sessions,admin=native_agents
    assert (await client.delete('/api/agents/a')).status_code==409
    await admin.execute("UPDATE tasks SET status='completed'")
    assert (await client.delete('/api/agents/a')).status_code==409
    await admin.execute('UPDATE runtime_sessions SET active_task_id=NULL')
    assert (await client.delete('/api/agents/a')).status_code==409
    await admin.execute('UPDATE conversations SET active_product_task_id=NULL')
    removed=await client.delete('/api/agents/a')
    assert removed.status_code==204,removed.text
    assert await admin.fetchval("SELECT deleted_at FROM agents WHERE id='a'") is not None
    assert await admin.fetchval("SELECT agent_id FROM conversations WHERE id='c'")=='a'
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'")=='completed'
    assert (await client.get('/api/agents/a')).status_code==404


async def test_concurrent_patch_delete_finishes_without_agent_resurrection(native_agents):
    import asyncio
    client,sessions,admin=native_agents
    created=await client.post('/api/agents',json=agent_body())
    assert created.status_code==201,created.text
    identifier=created.json()['id']
    patched,removed=await asyncio.wait_for(asyncio.gather(
        client.patch('/api/agents/'+identifier,json={'display_name':'Racing edit'}),
        client.delete('/api/agents/'+identifier)),10)
    assert removed.status_code==204,removed.text
    assert patched.status_code in (200,404),patched.text
    assert await admin.fetchval('SELECT count(*) FROM agents WHERE id=$1',identifier)==0
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',identifier)==0
    assert (await client.get('/api/agents/'+identifier)).status_code==404


async def test_concurrent_skill_binding_uninstall_never_leaves_dangling_reference(native_agents):
    import asyncio
    client,sessions,admin=native_agents
    skill=await install(client)
    created=await client.post('/api/agents',json=agent_body())
    assert created.status_code==201,created.text
    identifier=created.json()['id']
    bound,removed=await asyncio.wait_for(asyncio.gather(
        client.patch('/api/agents/'+identifier,json={'skill_binding_ids':[skill]}),
        client.delete('/api/skills/'+skill)),10)
    assert (bound.status_code,removed.status_code) in {(200,409),(422,204)},(bound.text,removed.text)
    installations=await admin.fetchval('SELECT count(*) FROM capability_installations WHERE id=$1',skill)
    bindings=await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',identifier)
    if bound.status_code==200:
        assert installations==bindings==1
    else:
        assert installations==bindings==0
    assert await admin.fetchval('''SELECT count(*) FROM agent_capability_bindings b
        LEFT JOIN capability_installations c ON c.id=b.capability_installation_id WHERE c.id IS NULL''')==0


async def test_management_binding_diagnostics_do_not_use_personal_permissions(native_agents):
    client, sessions, admin = native_agents
    skill = await install(client)
    created = await client.post('/api/agents', json=agent_body(skill_binding_ids=[skill]))
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    await admin.execute('DELETE FROM scope_memberships')
    detail = (await client.get('/api/agents/' + identifier)).json()
    assert detail['actions']['update'] is True
    assert skill in detail['unavailable_capability_ids']
    assert detail['binding_issues'] == []
    saved = await client.patch('/api/agents/' + identifier, json={'skill_binding_ids': [skill]})
    assert saved.status_code == 200, saved.text
    await admin.execute('DELETE FROM resource_shares WHERE capability_id=$1', skill)
    detail = (await client.get('/api/agents/' + identifier)).json()
    assert detail['binding_issues'] == [{'id': skill, 'kind': 'skill', 'reason': 'unshared'}]
    await admin.execute("DELETE FROM role_permissions WHERE permission_id='agent.update'")
    detail = (await client.get('/api/agents/' + identifier)).json()
    assert detail['binding_issues'] is None


async def test_binding_diagnostics_report_individual_knowledge_and_mcp_issues(native_agents):
    from workmate_product.application.agent_binding_status import binding_issues
    from workmate_product.persistence.models import Agent, AgentCapabilityBinding, KnowledgeBase
    client, sessions, admin = native_agents
    async with sessions.begin() as db:
        db.add_all([
            CapabilityInstallation(id='connector', capability_type='mcp', scope_id='scope',
                name='connector', display_name='Connector', enabled=True, metadata_={}),
            KnowledgeBase(id='healthy', scope_id='scope', name='Healthy', runtime_id='healthy', runtime_owner_id='u'),
            KnowledgeBase(id='disabled', scope_id='scope', name='Disabled', runtime_id='disabled', runtime_owner_id='u', enabled=False),
        ])
        await db.flush()
        db.add_all([
            ResourceShare(id='connector-grant', capability_id='connector', target_scope_id='scope'),
            ResourceShare(id='kb-grant', knowledge_base_id='healthy', target_scope_id='scope'),
            AgentCapabilityBinding(agent_id='a', capability_installation_id='connector'),
        ])
        agent = await db.get(Agent, 'a')
        agent.knowledge_config = {'knowledge_base_ids': ['healthy', 'disabled', 'missing']}
        await db.flush()
        issues = await binding_issues(db, agent, AccessDecisionService(db), knowledge_enabled=True)
        assert issues == [
            {'id': 'connector', 'kind': 'mcp', 'reason': 'unready'},
            {'id': 'disabled', 'kind': 'knowledge', 'reason': 'disabled'},
            {'id': 'missing', 'kind': 'knowledge', 'reason': 'missing'},
        ]
        disabled = await binding_issues(db, agent, AccessDecisionService(db), knowledge_enabled=False)
        assert {'id': 'healthy', 'kind': 'knowledge', 'reason': 'service_disabled'} in disabled
