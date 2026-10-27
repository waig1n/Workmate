"""Native MCP HTTP owns durable private revisions and exposes public metadata only."""
import json
import pytest
import pytest_asyncio
from workmate_product.mcp.definitions import MCPCard,MCPHubPage,HttpMCPConfig
from workmate_product.persistence.models import Permission,RolePermission
from test_native_agents import native_agents,projection_database,agent_body  # noqa: F401

TOKEN='fixture-private-token-do-not-expose'
NEW_TOKEN='fixture-updated-token-do-not-expose'


class Catalog:
    hub_id='fixture-mcp'
    display_name='Fixture MCP catalog'
    description='Server-owned MCP templates'
    icon_url=None
    def __init__(self): self.calls=[]
    def card(self):
        return MCPCard(hub_id=self.hub_id,id='remote',name='remote',display_name='Remote tools',
            description='Fixture connection',is_stateful=True,auth='inputs',
            inputs_schema={'type':'object','properties':{'token':{'type':'string'},'tenant':{'type':'string'}},
                'required':['token','tenant'],'additionalProperties':False},
            config_template=HttpMCPConfig(url='https://example.invalid/${tenant}',headers={'Authorization':'Bearer ${token}'}))
    async def list_mcps(self,user_id,q=None,cursor=None,limit=40):
        self.calls.append(('list',user_id))
        return MCPHubPage(cards=[self.card()])
    async def get_mcp(self,user_id,card_id):
        self.calls.append(('get',user_id,card_id))
        assert card_id=='remote'
        return self.card()


@pytest_asyncio.fixture
async def native_mcps(native_agents):
    client,sessions,admin=native_agents
    async with sessions.begin() as db:
        db.add(Permission(id='mcp.manage',key='mcp.manage'))
        await db.flush()
        db.add(RolePermission(id='mcp.manage',role_id='manager',permission_id='mcp.manage'))
    catalog=Catalog()
    # Use the actual native application supplied by the shared ASGI fixture.
    client._transport.app.state.mcp_catalogs={catalog.hub_id:catalog}
    yield client,sessions,admin,catalog


async def install(client):
    response=await client.post('/api/mcps',json={'scope_id':'scope','hub_id':'fixture-mcp',
        'card_id':'remote','values':{'token':TOKEN,'tenant':'initial-tenant'}})
    assert response.status_code==201,response.text
    assert TOKEN not in response.text
    assert response.json()['usable'] is False
    return response.json()['id']


async def share_mcp(client, identifier):
    url = '/api/sharing/mcps/' + identifier
    body = {'target_scope_id': 'scope', 'include_descendants': False}
    preview = await client.post(url + '/preview', json=body)
    assert preview.status_code == 200, preview.text
    assert TOKEN not in preview.text
    response = await client.post(url, json={**body, 'confirmation': preview.json()['confirmation']})
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text


async def test_native_catalog_and_install_update_keep_private_immutable_revisions(native_mcps):
    import asyncpg
    client,sessions,admin,catalog=native_mcps
    hubs=await client.get('/api/mcps/hubs')
    assert hubs.status_code==200,hubs.text
    assert {item['hub_id'] for item in hubs.json()}=={'fixture-mcp'}
    page=await client.get('/api/mcps/catalog',params={'hub_id':'fixture-mcp'})
    assert page.status_code==200,page.text
    assert page.json()['cards'][0]['id']=='remote'
    identifier=await install(client)
    old=await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions WHERE capability_id=$1',identifier)
    old_payload=await admin.fetchval('SELECT payload FROM mcp_secret_revisions WHERE id=$1',old)
    updated=await client.patch('/api/mcps/'+identifier,json={'values':{'token':NEW_TOKEN}})
    assert updated.status_code==200,updated.text
    assert TOKEN not in updated.text and NEW_TOKEN not in updated.text
    current=await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions WHERE capability_id=$1',identifier)
    assert current!=old
    assert await admin.fetchval('SELECT payload FROM mcp_secret_revisions WHERE id=$1',old)==old_payload
    payload=json.loads(await admin.fetchval('SELECT payload FROM mcp_secret_revisions WHERE id=$1',current))
    assert payload['values']=={'token':NEW_TOKEN,'tenant':'initial-tenant'}
    assert payload['client']['mcp_config']['headers']['Authorization']=='Bearer '+NEW_TOKEN
    assert payload['client']['mcp_config']['url']=='https://example.invalid/initial-tenant'
    listed=await client.get('/api/mcps')
    assert listed.status_code==200,listed.text
    public=json.dumps([dict(row) for row in await admin.fetch('SELECT metadata FROM capability_installations')],default=str)
    audits=json.dumps([dict(row) for row in await admin.fetch('SELECT metadata FROM security_audit_events')],default=str)
    assert all(secret not in listed.text+public+audits for secret in (TOKEN,NEW_TOKEN))
    with pytest.raises(asyncpg.PostgresError,match='immutable'):
        await admin.execute("UPDATE mcp_secret_revisions SET payload='{}'")
    assert len([call for call in catalog.calls if call[0]=='get']) == 1  # rotation uses the saved template


async def test_bound_native_mcp_cannot_be_disabled_or_deleted_until_unbound(native_mcps):
    client,sessions,admin,catalog=native_mcps
    identifier=await install(client)
    denied=await client.post('/api/agents',json=agent_body(mcp_binding_ids=[identifier]))
    assert denied.status_code==422,denied.text
    await share_mcp(client, identifier)
    created=await client.post('/api/agents',json=agent_body(mcp_binding_ids=[identifier]))
    assert created.status_code==201,created.text
    agent=created.json()['id']
    assert (await client.patch('/api/mcps/'+identifier,json={'enabled':False})).status_code==409
    assert (await client.delete('/api/mcps/'+identifier)).status_code==409
    assert await admin.fetchval('SELECT enabled FROM capability_installations WHERE id=$1',identifier) is True
    unbound=await client.patch('/api/agents/'+agent,json={'mcp_binding_ids':[]})
    assert unbound.status_code==200,unbound.text
    assert (await client.patch('/api/mcps/'+identifier,json={'enabled':False})).status_code==200
    assert (await client.delete('/api/mcps/'+identifier)).status_code==204
    assert await admin.fetchval('SELECT count(*) FROM capability_mcp_revisions WHERE capability_id=$1',identifier)==0
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions WHERE capability_id=$1',identifier)==1


async def test_native_mcp_revoked_management_cannot_modify_private_revision(native_mcps):
    client,sessions,admin,catalog=native_mcps
    identifier=await install(client)
    await admin.execute('DELETE FROM role_bindings')
    changed=await client.patch('/api/mcps/'+identifier,json={'values':{'token':NEW_TOKEN}})
    assert changed.status_code==403,changed.text
    assert NEW_TOKEN not in changed.text
    assert (await client.delete('/api/mcps/'+identifier)).status_code==403
    attempted=await client.post('/api/mcps',json={'scope_id':'scope','hub_id':'fixture-mcp','card_id':'remote',
        'name':'forbidden','values':{'token':NEW_TOKEN,'tenant':'other'}})
    assert attempted.status_code==403,attempted.text
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions')==1


async def test_native_mcp_invalid_private_input_never_leaks_in_error_or_commits(native_mcps):
    client,sessions,admin,catalog=native_mcps
    response=await client.post('/api/mcps',json={'scope_id':'scope','hub_id':'fixture-mcp','card_id':'remote',
        'values':{'token':TOKEN,'tenant':['invalid',TOKEN]}})
    assert response.status_code in (400,422),response.text
    assert TOKEN not in response.text
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==0
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions')==0


async def test_native_mcp_rechecks_authority_after_catalog_io(native_mcps):
    client,sessions,admin,catalog=native_mcps
    original=catalog.get_mcp
    async def revoked(user_id,card_id):
        await admin.execute('DELETE FROM role_bindings')
        return await original(user_id,card_id)
    catalog.get_mcp=revoked
    response=await client.post('/api/mcps',json={'scope_id':'scope','hub_id':'fixture-mcp','card_id':'remote',
        'values':{'token':TOKEN,'tenant':'initial-tenant'}})
    assert response.status_code==403,response.text
    assert TOKEN not in response.text
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==0
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions')==0
