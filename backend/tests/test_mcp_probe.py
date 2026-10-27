from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from app.services.mcp_probe import probe_mcp
from workmate_product.mcp.definitions import MCPConfiguration

@pytest.fixture
def configuration():
    return MCPConfiguration(name='test',is_stateful=True,mcp_config={'type':'http_mcp','url':'https://example.com/mcp'})

@pytest.fixture
def settings():
    return SimpleNamespace(runtime_base_url='http://runtime:8000',runtime_internal_token='internal-secret',worker_nodes=[])

async def test_probe_routes_with_auth_and_drops_remote_descriptions(configuration, settings):
    async def handler(request):
        assert request.url.path == '/workmate/mcps/probe'
        assert request.headers['X-Workmate-Internal-Token'] == 'internal-secret'
        return httpx.Response(200,json={'ok':True,'storage_node_id':'node','tools':[{'name':'lookup','description':'secret'}]})
    result=await probe_mcp(configuration,settings=settings,transport=httpx.MockTransport(handler))
    assert result['tools'] == [{'name':'lookup','description':''}]

async def test_probe_no_ready_node_fails_closed(configuration, settings):
    settings.worker_nodes=[object()]
    with pytest.raises(HTTPException) as error:
        await probe_mcp(configuration,settings=settings)
    assert error.value.status_code == 503

async def test_probe_does_not_reflect_worker_failure(configuration, settings):
    async def handler(request): return httpx.Response(500,text='secret-token')
    with pytest.raises(HTTPException) as error:
        await probe_mcp(configuration,settings=settings,transport=httpx.MockTransport(handler))
    assert 'secret-token' not in error.value.detail

@pytest.mark.parametrize('code', ['blocked_target','dns_error','connection_refused','network_error','tls_error','timeout','authentication_required','permission_denied','endpoint_not_found','protocol_error'])
async def test_product_uses_only_known_safe_diagnostic_text(configuration,settings,code):
    async def handler(request):
        return httpx.Response(200,json={'ok':False,'storage_node_id':'node','tools':[],
                                       'error_code':code,'error':'secret-token'})
    result=await probe_mcp(configuration,settings=settings,transport=httpx.MockTransport(handler))
    assert result['error_code']==code
    assert 'secret-token' not in str(result)

async def test_product_does_not_trust_unknown_error_code(configuration,settings):
    async def handler(request):
        return httpx.Response(200,json={'ok':False,'storage_node_id':'node','tools':[],
                                       'error_code':'secret-token','error':'secret-token'})
    result=await probe_mcp(configuration,settings=settings,transport=httpx.MockTransport(handler))
    assert result['error_code']=='protocol_error'
    assert 'secret-token' not in str(result)

async def test_product_preserves_safe_busy_status(configuration,settings):
    async def handler(request):
        return httpx.Response(429,json={'detail':{'code':'private-token','message':'private-token'}})
    with pytest.raises(HTTPException) as error:
        await probe_mcp(configuration,settings=settings,transport=httpx.MockTransport(handler))
    assert error.value.status_code == 429
    assert error.value.detail['code'] == 'busy'
    assert 'private-token' not in str(error.value.detail)
