from types import SimpleNamespace
import httpx
import pytest
from app.services.worker_nodes import WorkerNodeDirectory, STALE_AFTER


def node(name='one', enabled=True):
    return SimpleNamespace(storage_node_id=name,base_url='http://'+name,enabled=enabled)


async def test_identity_authenticated_readiness_expiry_and_draining():
    now = [0]
    async def handler(request):
        assert request.url.path == '/workmate/node'
        assert request.headers['X-Workmate-Internal-Token'] == 'secret'
        return httpx.Response(200,json={'artifact_storage_backend':'s3','role':'worker','ready':True,'storage_node_id':request.url.host})
    async with WorkerNodeDirectory([node(),node('draining',False)],'secret',
            transport=httpx.MockTransport(handler),clock=lambda:now[0]) as directory:
        assert directory.candidates() == ('one',)
        assert directory.available('draining')
        now[0] = STALE_AFTER
        assert directory.candidates() == ()
        assert not directory.available('draining')


@pytest.mark.parametrize('failure',['identity','not-ready','http','invalid','timeout','local'])
async def test_unavailable_nodes_do_not_block_gateway_startup(failure):
    async def handler(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('test')
        if failure == 'invalid':
            return httpx.Response(200,content='not-json')
        return httpx.Response(503 if failure=='http' else 200,json={
            'artifact_storage_backend':'local' if failure=='local' else 's3','role':'worker','ready':failure!='not-ready',
            'storage_node_id':'wrong' if failure=='identity' else 'one'})
    async with WorkerNodeDirectory([node()],'secret',transport=httpx.MockTransport(handler)) as directory:
        assert directory.candidates() == ()


async def test_monitor_shutdown_joins_inflight_probe(monkeypatch):
    import asyncio
    import app.services.worker_nodes as module
    monkeypatch.setattr(module,'PROBE_INTERVAL',0.001)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return httpx.Response(200,json={'artifact_storage_backend':'s3','role':'worker','ready':True,'storage_node_id':'one'})
    async with WorkerNodeDirectory([node()],'secret',transport=httpx.MockTransport(handler)) as directory:
        await asyncio.wait_for(entered.wait(),1)
    assert cancelled.is_set()
    assert directory._task.done()
    assert directory._client.is_closed


async def test_missing_service_token_does_not_probe():
    async def forbidden(request):
        raise AssertionError('Unauthenticated probe')
    async with WorkerNodeDirectory([node()],'',transport=httpx.MockTransport(forbidden)) as directory:
        assert directory.candidates() == ()
