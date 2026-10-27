import pytest
from agentscope.mcp import MCPClient
from workmate_runtime.mcp.http_transport import GovernedHTTPMCPClient, governed_client_factory


def test_only_private_policy_marker_selects_governed_client():
    spec={'name':'test','is_stateful':True,'mcp_config':{'type':'http_mcp','url':'https://enterprise.example/mcp'}}
    assert type(governed_client_factory(spec)) is MCPClient
    governed=governed_client_factory({**spec,'_workmate_http_policy':{'blocked_hosts':['gateway'],'blocked_addresses':['10.0.0.2']}})
    assert isinstance(governed, GovernedHTTPMCPClient)
    assert '_workmate_http_policy' not in governed.model_dump()
    assert governed._blocked_addresses == ('10.0.0.2',)

async def test_governed_client_blocks_loopback_before_network():
    client=governed_client_factory({'name':'test','is_stateful':True,
        'mcp_config':{'type':'http_mcp','url':'http://127.0.0.1:1/mcp'},'_workmate_http_policy':{}})
    with pytest.raises(ValueError):
        async with client._create_http_client():
            pytest.fail('Unsafe client was opened')

async def test_execution_denies_platform_ip_even_when_hostname_differs():
    from workmate_runtime.mcp.http_transport import validate_target
    async def resolver(host, port):
        if host == 'enterprise.example': return ['10.20.30.40']
        raise OSError()
    with pytest.raises(ValueError):
        await validate_target('https://enterprise.example/mcp', resolver=resolver,
                              blocked_addresses=('10.20.30.40',))

async def test_assembly_derives_policy_only_from_private_published_card(monkeypatch):
    import json
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import httpx
    from agentscope.workspace._gateway_client import GatewayClient
    from workmate_runtime.mcp.assembly import assemble_mcp_tools
    from workmate_runtime.mcp.gateway import MemoryGatewayClient
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.transport.postgres import ExecutionOwner
    from workmate_product.mcp.definitions import MCPConfiguration
    owner=ExecutionOwner('s','t','w',1)
    registry=TaskResourceRegistry()
    registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
    configuration=MCPConfiguration(name='test',is_stateful=True,
        mcp_config={'type':'http_mcp','url':'https://enterprise.example/mcp'})
    spec=SimpleNamespace(mcp_configurations=('version',))
    context=SimpleNamespace(initial_spec=spec,permission_version=1,agent_id='a')
    definition=SimpleNamespace(capability_id='c',secret_version='v1',client=configuration,
                               card=SimpleNamespace(hub_id='workmate-custom-http'))
    @asynccontextmanager
    async def begin(): yield object()
    async def load(*args, **kwargs): return context,(definition,)
    async def resolve(host, port): return ['10.20.30.40']
    monkeypatch.setattr('workmate_runtime.mcp.assembly.load_execution_mcp_definitions', load)
    monkeypatch.setattr('workmate_runtime.mcp.http_transport._resolve_host', resolve)
    bodies=[]
    async def server(request):
        if request.url.path == '/health': return httpx.Response(200,json={'instance_nonce':'n'})
        if request.method == 'POST': bodies.append(json.loads(request.content))
        return httpx.Response(200,json=[] if request.method == 'GET' else {'ok':True})
    gateway=MemoryGatewayClient(GatewayClient(object(),12345,auth_token='t',instance_nonce='n'),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
    try:
        await assemble_mcp_tools(context,SimpleNamespace(workspace=SimpleNamespace(_gateway=gateway)),
            SimpleNamespace(begin=begin),SimpleNamespace(owner=owner),registry,
            storage_node_id='node',product_api_url='http://control.internal:8080')
    finally:
        await gateway.aclose()
    assert bodies[0]['_workmate_http_policy'] == {'blocked_hosts':['gateway','runtime','postgres','redis','rustfs','control.internal'],
                                          'blocked_addresses':['10.20.30.40']}
    assert '_workmate_http_policy' not in configuration.model_dump()

@pytest.mark.parametrize('url', ['file:///etc/passwd','https://u:p@example.com/mcp',
                                 'https://example.com/mcp?token=private','https://localhost/mcp'])
async def test_target_admission_rejects_invalid_or_private_urls(url):
    from workmate_runtime.mcp.http_transport import validate_target
    with pytest.raises(ValueError): await validate_target(url)

async def test_pinned_transport_never_sends_credentials_to_changed_origin():
    import httpx
    from workmate_runtime.mcp.http_transport import PinnedTransport
    async with httpx.AsyncClient(transport=PinnedTransport('https://enterprise.example/mcp','10.0.0.1')) as client:
        with pytest.raises(ValueError, match='origin'):
            await client.get('https://other.example/mcp', headers={'Authorization':'secret'})

async def test_discovery_stream_limit_closes_owned_stream():
    import httpx
    from workmate_runtime.mcp.http_transport import BoundedStream
    class Stream(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield b'a' * (1024 * 1024)
            yield b'b'
        async def aclose(self): self.closed=True
    source=Stream()
    stream=BoundedStream(source)
    with pytest.raises(ValueError, match='limit'):
        async for _ in stream: pass
    await stream.aclose()
    assert source.closed

async def test_platform_policy_fails_closed_when_product_host_cannot_resolve(monkeypatch):
    from workmate_runtime.mcp.gateway import MCPGatewayError
    from workmate_runtime.mcp.policy import build_http_policy

    async def unavailable(host, port):
        raise OSError('DNS unavailable')

    monkeypatch.setattr('workmate_runtime.mcp.http_transport._resolve_host', unavailable)
    with pytest.raises(MCPGatewayError, match='Platform network identity'):
        await build_http_policy('http://control.internal:8080')
    # Optional Compose names may not exist in a non-Compose deployment.
    policy = await build_http_policy(None)
    assert policy['blocked_addresses'] == []
    assert 'gateway' in policy['blocked_hosts']
