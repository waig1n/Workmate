"""Pinned registry mapping and bounded, secret-safe Product MCP catalog I/O."""
import json
import httpx
import pytest

from workmate_product.mcp.github_catalog import GitHubMCPCatalog, MAX_RESPONSE_BYTES
from workmate_product.mcp.definitions import MCPDefinitionError, render_mcp


def entry(identifier='remote-id', *, remotes=None, packages=None):
    return {'server':{'id':identifier,'name':'io.github.example/demo','description':'Useful server',
        'updated_at':'2026-01-01T00:00:00Z','version_detail':{'version':'1.2.3'},
        'repository':{'url':'https://example.invalid/repo','readme':'Detailed README'},
        'remotes':remotes or [],'packages':packages or []},
        'x-github':{'display_name':'Demo','primary_language':'Python','name_with_owner':'example/repo'}}


@pytest.mark.asyncio
async def test_remote_preferred_and_public_pagination_matches_registry():
    requests=[]
    def serve(request):
        requests.append(request)
        return httpx.Response(200,json={'servers':[entry(remotes=[{'url':'https://mcp.invalid',
            'headers':[{'value':'Bearer {TOKEN}','is_secret':True}]}],packages=[{'runtime_hint':'npx','name':'ignored'}]),
            entry('unreachable')], 'metadata':{'next_cursor':'next'}})
    async with GitHubMCPCatalog(api_token='registry-secret',transport=httpx.MockTransport(serve)) as hub:
        page=await hub.list_mcps('u',q='demo',cursor='opaque cursor',limit=2)
    card=page.cards[0]
    assert len(page.cards)==1 and page.next_cursor=='next'
    assert card.is_stateful is False and card.config_template.type=='http_mcp'
    assert card.config_template.headers=={'Authorization':'Bearer ${TOKEN}'}
    assert card.inputs_schema['properties']['TOKEN']['writeOnly'] is True
    assert card.readme is None and card.version=='1.2.3' and card.updated_at
    assert render_mcp(card,{'TOKEN':'secret'}).client.mcp_config.headers=={'Authorization':'Bearer secret'}
    assert requests[0].url.params==httpx.QueryParams({'limit':2,'cursor':'opaque cursor'})
    assert requests[0].headers['Authorization']=='Bearer registry-secret'
    assert hub._client is None


@pytest.mark.asyncio
@pytest.mark.parametrize('runtime,expected',[('npx',['-y','demo@1']),('uvx',['demo']),
    ('uv',['tool','run','demo']),('docker',['run','-i','--rm','demo'])])
async def test_supported_package_mapping_and_optional_inputs(runtime,expected):
    source=entry('package',packages=[{'runtime_hint':runtime,'name':'demo','version':'1',
        'environment_variables':[{'name':'TOKEN','is_required':True,'is_secret':True},
            {'name':'OPTIONAL'}, {'name':'FIXED','value':'fixed'},
            {'name':'COMPOSED','value':'prefix {VALUE}','variables':{'VALUE':{'default':'value'}}}]}])
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=source))) as hub:
        card=await hub.get_mcp('u','package')
    assert card.is_stateful and card.config_template.command==runtime
    assert card.config_template.args==expected and card.readme=='Detailed README'
    rendered=render_mcp(card,{'TOKEN':'private'})
    assert rendered.client.mcp_config.env=={'TOKEN':'private','FIXED':'fixed','COMPOSED':'prefix value'}


@pytest.mark.asyncio
async def test_search_empty_page_keeps_next_cursor():
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={
        'servers':[entry(remotes=[{'url':'https://mcp.invalid'}])],'metadata':{'next_cursor':'keep'}}))) as hub:
        page=await hub.list_mcps('u',q='no-match')
    assert page.cards==[] and page.next_cursor=='keep'


@pytest.mark.asyncio
async def test_card_id_is_one_encoded_path_segment():
    requests=[]
    identifier='../evil?token=private#fragment'
    def serve(request):
        requests.append(request)
        return httpx.Response(200,json=entry(identifier,remotes=[{'url':'https://mcp.invalid'}]))
    async with GitHubMCPCatalog(transport=httpx.MockTransport(serve)) as hub:
        await hub.get_mcp('u',identifier)
    assert requests[0].url.raw_path==b'/v0/servers/..%2Fevil%3Ftoken%3Dprivate%23fragment'
    assert requests[0].url.host=='api.mcp.github.com' and not requests[0].url.query


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[302,401,404,429,500])
async def test_upstream_error_never_exposes_body_or_authorization(status):
    calls=[]
    def serve(request):
        calls.append(request)
        return httpx.Response(status,text='PRIVATE-UPSTREAM-BODY',headers={'location':'https://untrusted.invalid'})
    async with GitHubMCPCatalog(api_token='PRIVATE-TOKEN',transport=httpx.MockTransport(serve)) as hub:
        with pytest.raises((MCPDefinitionError,KeyError)) as failed:
            await hub.get_mcp('u','identifier')
    assert 'PRIVATE' not in str(failed.value) and len(calls)==1


class Chunks(httpx.AsyncByteStream):
    def __init__(self): self.read_count=0
    async def __aiter__(self):
        for _ in range(MAX_RESPONSE_BYTES//1024+100):
            self.read_count+=1
            yield b'x'*1024


@pytest.mark.asyncio
async def test_stream_body_limit_stops_reading_before_exhaustion():
    chunks=Chunks()
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=chunks))) as hub:
        with pytest.raises(MCPDefinitionError,match='size'):
            await hub.list_mcps('u')
    assert chunks.read_count<=MAX_RESPONSE_BYTES//1024+1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[b'not json',b'[]',b'{"servers": "bad"}'])
async def test_malformed_response_is_sanitized(payload):
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=payload))) as hub:
        with pytest.raises(MCPDefinitionError): await hub.list_mcps('u')


@pytest.mark.asyncio
async def test_changed_detail_identity_is_rejected():
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=entry(
        'wrong',remotes=[{'url':'https://mcp.invalid'}])))) as hub:
        with pytest.raises(MCPDefinitionError): await hub.get_mcp('u','expected')


@pytest.mark.asyncio
async def test_transport_failure_suppresses_secret_bearing_exception():
    def fail(request):
        raise httpx.ConnectError('PRIVATE transport diagnostics',request=request)
    async with GitHubMCPCatalog(transport=httpx.MockTransport(fail)) as hub:
        with pytest.raises(MCPDefinitionError) as error:
            await hub.list_mcps('u')
    assert 'PRIVATE' not in str(error.value) and error.value.__suppress_context__


@pytest.mark.asyncio
async def test_lifecycle_required_and_reentrant_open_rejected():
    hub=GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={})))
    with pytest.raises(MCPDefinitionError,match='lifecycle'):
        await hub.list_mcps('u')
    async with hub:
        client=hub._client
        with pytest.raises(MCPDefinitionError,match='already open'):
            await hub.__aenter__()
    assert client.is_closed and hub._client is None


@pytest.mark.asyncio
@pytest.mark.parametrize('params',[{'limit':True},{'limit':0},{'limit':101},{'cursor':1},{'q':[]}])
async def test_invalid_query_never_sends_request(params):
    with pytest.raises(MCPDefinitionError,match='pagination'):
        await GitHubMCPCatalog().list_mcps('u',**params)


@pytest.mark.parametrize('base_url',['https://user:secret@example.invalid','https://example.invalid?token=secret',
    'https://example.invalid/path','file:///tmp/registry'])
def test_non_registry_base_urls_rejected_without_echoing_configuration(base_url):
    with pytest.raises(MCPDefinitionError) as error:
        GitHubMCPCatalog(base_url=base_url)
    assert base_url not in str(error.value)


@pytest.mark.asyncio
async def test_unsupported_package_skipped_and_missing_card_is_generic():
    source=entry('unsupported',packages=[{'runtime_hint':'unknown','name':'demo'},
        {'runtime_hint':'npx'}])
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=source))) as hub:
        with pytest.raises(KeyError,match='MCP card not found'):
            await hub.get_mcp('u','unsupported')


@pytest.mark.asyncio
async def test_input_enum_schema_and_nested_placeholder_mapping():
    source=entry('vars',packages=[{'runtime_hint':'uvx','name':'demo','environment_variables':[
        {'name':''}, {'name':'SETTING','is_required':True,'choices':['a','b'],'default':'a'}]}])
    async with GitHubMCPCatalog(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=source))) as hub:
        card=await hub.get_mcp('u','vars')
    assert card.inputs_schema['properties']['SETTING']['enum']==['a','b']
    assert render_mcp(card,{}).client.mcp_config.env=={'SETTING':'a'}
