"""Dedicated knowledge transport preserves product contracts without App control."""
import json

import httpx
import pytest

from app.adapter.knowledge_client import KnowledgeServiceClient
from app.adapter.errors import RuntimeError as ServiceError


def client(handler, token='fixture-token'):
    return KnowledgeServiceClient('http://knowledge.test/', token, timeout=7,
                                  transport=httpx.MockTransport(handler))


async def test_knowledge_request_auth_json_query_and_encoded_identifier():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'documents': []})
    result = await client(handler).knowledge_request('POST', 'owner', 'kb/a?b#c',
        suffix='/search', body={'query': 'question'}, params={'limit': 3})
    assert result == {'documents': []}
    request = seen[0]
    assert request.url.raw_path == b'/workmate/knowledge/kb%2Fa%3Fb%23c/search?limit=3'
    assert request.headers['X-User-ID'] == 'owner'
    assert request.headers['X-Workmate-Internal-Token'] == 'fixture-token'
    assert json.loads(request.content) == {'query': 'question'}
    assert request.extensions['timeout']['read'] == 7


async def test_root_request_and_empty_204_preserve_return_contract():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(204)
    assert await client(handler).knowledge_request('DELETE', 'owner') is None
    assert seen[0].url.path == '/workmate/knowledge'


async def test_upload_encodes_both_ids_and_preserves_multipart():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={'id': 'document'})
    result = await client(handler).knowledge_upload(
        'owner', 'kb/a', 'notes.txt', 'text/plain', b'uploaded bytes', 'doc?b')
    assert result == {'id': 'document'}
    request = seen[0]
    assert request.method == 'PUT'
    assert request.url.raw_path == b'/workmate/knowledge/kb%2Fa/staged/doc%3Fb'
    assert request.headers['X-User-ID'] == 'owner'
    assert request.headers['X-Workmate-Internal-Token'] == 'fixture-token'
    assert b'filename="notes.txt"' in request.content
    assert b'Content-Type: text/plain' in request.content
    assert b'uploaded bytes' in request.content


async def test_content_preserves_bytes_headers_and_encoded_route():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b'hello', headers={
            'Content-Type': 'text/plain', 'Content-Disposition': 'inline; filename="notes.txt"'})
    assert await client(handler).knowledge_content('owner', 'kb/a', 'doc?b', 5) == (
        b'hello', 'text/plain', 'inline; filename="notes.txt"')
    assert seen[0].url.raw_path == b'/workmate/knowledge/kb%2Fa/documents/doc%3Fb/content'
    assert seen[0].headers['X-Workmate-Internal-Token'] == 'fixture-token'
    assert seen[0].headers['X-User-ID'] == 'owner'


async def test_content_defaults_when_upstream_omits_media_headers():
    assert await client(lambda _: httpx.Response(200, content=b'')).knowledge_content(
        'owner', 'kb', 'doc', 1) == (b'', 'application/octet-stream', 'attachment')


async def test_streaming_limit_stops_reading_and_closes_upstream():
    class Chunks(httpx.AsyncByteStream):
        read_count = 0
        closed = False
        async def __aiter__(self):
            for data in (b'123', b'456', b'never consumed'):
                self.read_count += 1
                yield data
        async def aclose(self):
            self.closed = True
    stream = Chunks()
    service = client(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(ServiceError) as error:
        await service.knowledge_content('owner', 'kb', 'doc', 5)
    assert error.value.status_code == 413
    assert stream.read_count == 2
    assert stream.closed


async def invoke(service, operation):
    if operation == 'request':
        return await service.knowledge_request('GET', 'owner', 'kb')
    if operation == 'upload':
        return await service.knowledge_upload('owner', 'kb', 'x.txt', 'text/plain', b'x', 'doc')
    return await service.knowledge_content('owner', 'kb', 'doc', 100)


@pytest.mark.parametrize('operation', ['request', 'upload', 'content'])
@pytest.mark.parametrize('status', [403, 409, 500])
async def test_upstream_failures_preserve_status_without_leaking_body(operation, status):
    service = client(lambda _: httpx.Response(status, json={'detail': 'private-secret-upstream'}))
    with pytest.raises(ServiceError) as error:
        await invoke(service, operation)
    assert error.value.status_code == status
    assert 'private-secret-upstream' not in str(error.value)
    assert 'private-secret-upstream' not in error.value.detail


@pytest.mark.parametrize('operation', ['request', 'upload', 'content'])
async def test_network_failure_is_sanitized_503(operation):
    def handler(request):
        raise httpx.ConnectError('private-secret-in-network-error', request=request)
    with pytest.raises(ServiceError) as error:
        await invoke(client(handler), operation)
    assert error.value.status_code == 503
    assert 'private-secret' not in str(error.value)
    assert 'private-secret' not in error.value.detail


@pytest.mark.parametrize('operation', ['request', 'upload', 'content'])
@pytest.mark.parametrize('token', ['', None])
async def test_missing_service_token_never_sends_request(operation, token):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    with pytest.raises((ServiceError, ValueError)):
        await invoke(client(handler, token=token), operation)
    assert seen == []


def test_knowledge_client_has_no_app_execution_or_session_control_surface():
    service = client(lambda _: httpx.Response(200, json={}))
    for name in ('chat', 'stop', 'stream', 'create_session', 'sync_knowledge_session',
                 'get_security_capabilities', 'list_agents', 'create_agent'):
        assert not hasattr(service, name), name


@pytest.mark.parametrize('operation',['request','upload'])
async def test_invalid_success_json_is_sanitized_502(operation):
    with pytest.raises(ServiceError) as error:
        await invoke(client(lambda _: httpx.Response(200,content=b'private-secret-not-json')),operation)
    assert error.value.status_code==502
    assert 'private-secret' not in str(error.value)
