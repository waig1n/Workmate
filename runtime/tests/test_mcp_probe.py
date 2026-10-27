import pytest
from workmate_runtime.mcp.probe import validate_target, probe_configuration
from workmate_product.mcp.definitions import MCPConfiguration

@pytest.mark.parametrize('ip', ['127.0.0.1', '169.254.169.254', '::1', '0.0.0.0', '224.0.0.1'])
async def test_blocks_unsafe_addresses(ip):
    async def resolve(host, port): return [ip]
    with pytest.raises(ValueError):
        await validate_target('https://enterprise.example/mcp', resolver=resolve)

async def test_private_enterprise_target_allowed():
    async def resolve(host, port):
        if host != 'enterprise.example': raise OSError()
        return ['10.20.30.40']
    assert await validate_target('https://enterprise.example/mcp', resolver=resolve) == '10.20.30.40'

async def test_platform_alias_address_blocked():
    async def resolve(host, port): return ['10.20.30.40']
    with pytest.raises(ValueError):
        await validate_target('https://enterprise.example/mcp', resolver=resolve,
                              blocked_hosts=('gateway',))

async def test_stdio_rejected_without_execution():
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'stdio_mcp','command':'secret'})
    result=await probe_configuration(config,storage_node_id='node')
    assert result['ok'] is False
    assert result['tools'] == []
    assert result['error_code'] == 'protocol_error'
    assert result['storage_node_id'] == 'node'

async def test_sdk_initializes_and_lists_without_tool_execution(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from workmate_runtime.mcp import probe
    calls=[]
    async def target(*args, **kwargs): return '10.20.30.40'
    @asynccontextmanager
    async def streams(*args, **kwargs): yield (None, None, None)
    class Session:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): calls.append('closed')
        async def initialize(self): calls.append('initialize')
        async def list_tools(self, cursor=None):
            calls.append(('list', cursor))
            return SimpleNamespace(tools=[SimpleNamespace(name='lookup', description='secret'), SimpleNamespace(name='prefix-hidden-token-suffix', description='')], nextCursor=None)
    monkeypatch.setattr(probe, 'validate_target', target)
    monkeypatch.setattr(probe, 'streamable_http_client', streams)
    monkeypatch.setattr(probe, 'ClientSession', Session)
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'http_mcp','url':'https://example.com/mcp','headers':{'Authorization':'Bearer hidden-token'}})
    result=await probe.probe_configuration(config,storage_node_id='node')
    assert result['ok'] is True
    assert result['tools'] == [{'name':'lookup','description':''}]
    assert calls == ['initialize',('list',None),'closed']

async def test_probe_errors_never_reflect_secret(monkeypatch):
    from workmate_runtime.mcp import probe
    async def target(*args, **kwargs): raise ValueError('secret-token')
    monkeypatch.setattr(probe, 'validate_target', target)
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'http_mcp','url':'https://example.com/mcp'})
    result=await probe.probe_configuration(config,storage_node_id='node')
    assert not result['ok']
    assert 'secret-token' not in str(result)

async def test_pinned_transport_preserves_host_and_blocks_redirect():
    import httpx
    from workmate_runtime.mcp.probe import PinnedTransport
    transport=PinnedTransport('https://example.com/mcp','10.20.30.40')
    async def handler(request):
        assert request.url.host == '10.20.30.40'
        assert request.headers['host'] == 'example.com'
        assert request.extensions['sni_hostname'] == 'example.com'
        return httpx.Response(302,headers={'location':'http://127.0.0.1/'})
    await transport.inner.aclose()
    transport.inner=httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError): await client.get('https://example.com/mcp')

@pytest.mark.parametrize('protocol', ['streamable', 'sse'])
async def test_real_fastmcp_streamable_protocol(monkeypatch, protocol, caplog):
    import asyncio
    import socket
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from workmate_runtime.mcp import probe
    server = FastMCP('probe-fixture', stateless_http=True, json_response=True)
    @server.tool()
    def lookup(value: str) -> str:
        raise AssertionError('Discovery must never execute a tool')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    app = server.streamable_http_app() if protocol == 'streamable' else server.sse_app()
    http_server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
    running = asyncio.create_task(http_server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not http_server.started:
                await asyncio.sleep(.01)
        # This local fixture bypasses target admission only; production policy
        # still rejects loopback and the actual pinned HTTP transport is used.
        async def fixture_target(*args, **kwargs): return '127.0.0.1'
        monkeypatch.setattr(probe, 'validate_target', fixture_target)
        config=MCPConfiguration(name='test',is_stateful=True,mcp_config={
            'type':'http_mcp','url':f'http://127.0.0.1:{port}/' + ('mcp' if protocol == 'streamable' else 'sse')})
        import logging
        catalog_config = config.model_copy(deep=True)
        catalog_config.mcp_config.url += '?api_key=real-query-secret'
        with caplog.at_level(logging.DEBUG, logger='httpx'):
            result=await probe.probe_configuration(catalog_config, storage_node_id='fixture')
        assert 'real-query-secret' not in caplog.text
        assert result['ok'], result
        assert result['tools'] == [{'name':'lookup','description':''}]
        # The real execution client uses the same policy and both SDK protocols.
        from workmate_runtime.mcp import http_transport
        monkeypatch.setattr(http_transport, 'validate_target', fixture_target)
        client = http_transport.governed_client_factory({**config.model_dump(), '_workmate_http_policy': {}})
        await client.connect()
        try:
            tools = await client.list_tools()
            assert [tool.name for tool in tools] == ['mcp__test__lookup']
        finally:
            await client.close()
    finally:
        http_server.should_exit = True
        await asyncio.wait_for(running, 5)
        sock.close()


def test_private_fragments_include_bearer_token_and_url_path():
    from workmate_runtime.mcp.probe import private_fragments
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={
        'type':'http_mcp','url':'https://example.com/private-token/mcp',
        'headers':{'Authorization':'Bearer hidden-token','X-API-Key':'key-token'}})
    assert {'hidden-token','key-token','private-token'} <= private_fragments(config.mcp_config)

async def test_probe_router_sanitizes_invalid_body_and_checks_readiness():
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from workmate_runtime.mcp.probe_router import create_probe_router
    settings=SimpleNamespace(storage_node_id='node',runtime=SimpleNamespace(product_api_url='http://gateway:8080'))
    ready=[True]
    app=FastAPI()
    app.include_router(create_probe_router(settings,lambda:ready[0]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://worker') as client:
        response=await client.post('/workmate/mcps/probe',json={'configuration':{'secret':'private-token'}})
        assert response.status_code == 400
        assert 'private-token' not in response.text
        ready[0]=False
        response=await client.post('/workmate/mcps/probe',json={})
        assert response.status_code == 503

async def test_probe_router_bounds_concurrency_and_releases_slots(monkeypatch):
    import asyncio
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from workmate_runtime.mcp import probe_router
    entered=0
    full=asyncio.Event()
    release=asyncio.Event()
    async def probe(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 4: full.set()
        await release.wait()
        return {'ok':True,'tools':[],'storage_node_id':'node'}
    monkeypatch.setattr(probe_router,'probe_configuration',probe)
    settings=SimpleNamespace(storage_node_id='node',runtime=SimpleNamespace(product_api_url='http://gateway:8080'))
    app=FastAPI()
    app.include_router(probe_router.create_probe_router(settings,lambda:True))
    body={'configuration':{'name':'test','is_stateful':True,'mcp_config':{'type':'http_mcp','url':'https://enterprise.local/mcp'}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://worker') as client:
        requests=[asyncio.create_task(client.post('/workmate/mcps/probe',json=body)) for _ in range(4)]
        try:
            await asyncio.wait_for(full.wait(), 2)
            assert (await client.post('/workmate/mcps/probe',json=body)).status_code == 429
        finally:
            release.set()
            responses=await asyncio.gather(*requests)
        assert all(response.status_code == 200 for response in responses)
        assert (await client.post('/workmate/mcps/probe',json=body)).status_code == 200

async def test_catalog_query_probe_allows_query_but_redacts_transport_logs(monkeypatch, caplog):
    import logging
    from workmate_runtime.mcp import probe
    config=MCPConfiguration(name='catalog',is_stateful=True,mcp_config={
        'type':'http_mcp','url':'https://enterprise.example/mcp?api_key=query-secret'})
    async def target(url, **kwargs):
        assert kwargs['allow_query'] is True
        for name in ('httpx', 'httpcore.connection', 'mcp.client.sse', 'mcp.client.streamable_http', 'client'):
            logging.getLogger(name).error('Request %s', url, exc_info=ValueError('query-secret'))
        raise ValueError('query-secret')
    monkeypatch.setattr(probe, 'validate_target', target)
    with caplog.at_level(logging.DEBUG):
        result=await probe.probe_configuration(config,storage_node_id='node')
        logging.getLogger('httpx').info('ordinary request after probe')
    assert 'query-secret' not in str(result)
    assert 'query-secret' not in caplog.text
    assert 'ordinary request after probe' in caplog.text
    assert 'query-secret' in probe.private_fragments(config.mcp_config)

async def test_catalog_query_target_keeps_network_boundaries():
    from workmate_runtime.mcp.http_transport import validate_target
    async def resolver(host, port):
        if host == 'enterprise.example': return ['10.20.30.40']
        raise OSError()
    url='https://enterprise.example/mcp?api_key=query-secret'
    with pytest.raises(ValueError): await validate_target(url,resolver=resolver)
    assert await validate_target(url,resolver=resolver,allow_query=True) == '10.20.30.40'
    with pytest.raises(ValueError):
        await validate_target(url,resolver=resolver,allow_query=True,blocked_addresses=('10.20.30.40',))

@pytest.mark.parametrize('status,code', [(401,'authentication_required'),(403,'permission_denied'),(404,'endpoint_not_found')])
async def test_probe_classifies_nested_http_status_safely(monkeypatch,status,code):
    import httpx
    from workmate_runtime.mcp import probe
    error=httpx.HTTPStatusError('secret-token',request=httpx.Request('GET','https://example.com/?key=secret-token'),response=httpx.Response(status))
    async def target(*args,**kwargs): raise ExceptionGroup('private', [error])
    monkeypatch.setattr(probe,'validate_target',target)
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'http_mcp','url':'https://example.com/mcp'})
    result=await probe.probe_configuration(config,storage_node_id='node')
    assert result['error_code']==code
    assert 'secret-token' not in str(result)

@pytest.mark.parametrize('kind,code', [('timeout','timeout'),('dns','dns_error'),('tls','tls_error'),('refused','connection_refused'),('blocked','blocked_target')])
async def test_probe_safe_diagnostics(monkeypatch,kind,code):
    import socket, ssl
    from workmate_runtime.mcp import probe
    from workmate_runtime.mcp.http_transport import MCPTargetBlocked
    errors={'timeout':TimeoutError('secret'), 'dns':socket.gaierror('secret'),
            'tls':ssl.SSLCertVerificationError('secret'),'refused':ConnectionRefusedError('secret'),
            'blocked':MCPTargetBlocked('secret')}
    async def target(*args,**kwargs): raise errors[kind]
    monkeypatch.setattr(probe,'validate_target',target)
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'http_mcp','url':'https://example.com/mcp'})
    result=await probe.probe_configuration(config,storage_node_id='node')
    assert result['error_code']==code
    assert 'secret' not in str(result)

@pytest.mark.parametrize('status,code', [(401,'authentication_required'),(403,'permission_denied'),(404,'endpoint_not_found')])
async def test_actual_sdk_http_failures_preserve_safe_diagnostics(monkeypatch,status,code):
    import httpx
    from workmate_runtime.mcp import probe
    async def target(*args,**kwargs): return '10.20.30.40'
    async def handler(request): return httpx.Response(status, text='private-service-token')
    monkeypatch.setattr(probe,'validate_target',target)
    monkeypatch.setattr(probe,'PinnedTransport',lambda *args: httpx.MockTransport(handler))
    config=MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'http_mcp','url':'https://example.com/mcp'})
    result=await probe.probe_configuration(config,storage_node_id='node')
    assert result['error_code']==code
    assert 'private-service-token' not in str(result)


def test_probe_classifier_follows_typed_transport_causes():
    import httpx, ssl
    from workmate_runtime.mcp.probe import classify_probe_error
    wrapper=httpx.ConnectError('private-url')
    wrapper.__cause__=ssl.SSLCertVerificationError('private-certificate')
    assert classify_probe_error(ExceptionGroup('group',[wrapper])) == 'tls_error'
    assert classify_probe_error(httpx.ConnectError('private-url')) == 'network_error'
    assert classify_probe_error(httpx.ReadTimeout('private-url')) == 'timeout'
    assert classify_probe_error(ValueError('private-url')) == 'protocol_error'
