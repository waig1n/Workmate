"""Read-only provider probes authenticate and close their SDK resources."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from workmate_runtime.sandbox.probe import probe_provider
from workmate_runtime.sandbox.base import WorkspaceBindingError


@pytest.mark.parametrize('provider',['docker','kubernetes','e2b','daytona','opensandbox'])
@pytest.mark.parametrize('failure',[None,RuntimeError('secret-credential-detail'),asyncio.CancelledError()])
async def test_read_only_probe_closes_resources(monkeypatch,provider,failure):
    calls=[]
    async def operation(*args,**kwargs):
        calls.append(('read',args,kwargs))
        if failure is not None:
            raise failure
        return SimpleNamespace(status_code=200)
    async def close():
        calls.append(('close',))
    settings=SimpleNamespace(provider=provider,kubeconfig=None,
        api_key=SimpleNamespace(get_secret_value=lambda:'test-key'),domain='test.example',
        api_url='https://test.example',target='test',protocol='https')
    if provider=='docker':
        monkeypatch.setattr('aiodocker.Docker',lambda:SimpleNamespace(version=operation,close=close))
    elif provider=='kubernetes':
        monkeypatch.setattr('kubernetes_asyncio.config.load_incluster_config',lambda **kwargs:None)
        monkeypatch.setattr('kubernetes_asyncio.client.ApiClient',lambda **kwargs:SimpleNamespace(close=close))
        monkeypatch.setattr('kubernetes_asyncio.client.CoreV1Api',lambda _:SimpleNamespace(get_api_resources=operation))
    elif provider=='e2b':
        class OwnedTransport:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): await close()
        class ApiClient:
            def __init__(self,*args,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): calls.append(('client_close',))
        monkeypatch.setattr('httpx.AsyncHTTPTransport',OwnedTransport)
        monkeypatch.setattr('e2b.api.client_async.AsyncApiClient',ApiClient)
        monkeypatch.setattr('e2b.connection_config.ConnectionConfig',lambda **kwargs:SimpleNamespace(**kwargs))
        monkeypatch.setattr('e2b.api.client.api.sandboxes.get_v2_sandboxes.asyncio_detailed',operation)
    elif provider=='daytona':
        async def listing(query,request_timeout):
            assert query.limit==1
            try:
                await operation()
                yield object()
            finally:
                calls.append(('iterator_close',))
        monkeypatch.setattr('daytona.AsyncDaytona',lambda _:SimpleNamespace(list=listing,close=close))
    else:
        async def create(config):
            return SimpleNamespace(list_sandbox_infos=operation,close=close)
        monkeypatch.setattr('opensandbox.SandboxManager.create',create)
    if isinstance(failure,asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await probe_provider(settings)
    elif failure:
        with pytest.raises(WorkspaceBindingError) as error:
            await probe_provider(settings)
        assert 'secret-credential-detail' not in str(error.value)
    else:
        await probe_provider(settings)
    assert any(call[0]=='read' for call in calls)
    assert ('close',) in calls
    if provider=='daytona':
        assert ('iterator_close',) in calls
    if provider=='e2b':
        assert ('client_close',) in calls
        assert next(c for c in calls if c[0]=='read')[2]['limit']==1
    if provider=='opensandbox':
        assert next(c for c in calls if c[0]=='read')[1][0].page_size==1


async def test_e2b_failed_auth_is_not_success(monkeypatch):
    from workmate_product.config.sandbox import E2BSettings
    from pydantic import SecretStr
    monkeypatch.setattr('e2b.api.client.api.sandboxes.get_v2_sandboxes.asyncio_detailed',
        AsyncMock(return_value=SimpleNamespace(status_code=401)))
    with pytest.raises(WorkspaceBindingError):
        await probe_provider(E2BSettings(api_key=SecretStr('test-key')))
