"""Redis SSE HTTP admission and disconnects over isolated PostgreSQL/Redis."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.requests import ClientDisconnect
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

from test_durable_projection import projection_database  # noqa: F401
from test_business_reset_composition import composition  # noqa: F401
from app.api.routers.tasks import router
from app.auth.security import create_access_token
from app.db import get_db
from app.services.conversation_business_state import (
    ConversationBusinessState, ResetCapacityExceeded, ResetStateConflict,
)
from app.services.conversation_business_state import ConversationUnavailable
from app.services.realtime_product_stream import RealtimeProductStream, StreamCapacityExceeded
from workmate_product.config.realtime import RealtimePolicy
from workmate_product.projections.store import ProjectionConflict
from workmate_product.realtime.reader import ReaderCapacityExceeded, ReaderClosed, GenerationMismatch
from workmate_product.realtime.redis_log import LogLost, ReplayGap
from workmate_product.persistence.repositories.event_logs import LogUnavailable


@pytest_asyncio.fixture
async def http_scene(composition, monkeypatch):
    sessions, admin, log, manager, readers = composition
    from app.auth import security
    monkeypatch.setattr(security, 'get_settings', lambda: SimpleNamespace(
        jwt_secret='isolated-http-test-signing-key-0123456789',
        jwt_algorithm='HS256', jwt_expire_hours=1))
    web = FastAPI()
    web.include_router(router, prefix='/api')
    policy = RealtimePolicy().model_copy(update={
        'authorize_interval_seconds': .02, 'heartbeat_interval_seconds': .02,
        'reset_retry_seconds': .02,
    })
    stream = RealtimeProductStream(manager, log, readers[0], policy=policy)
    web.state.product_stream = stream
    web.state.product_sessions = sessions
    requests = []
    async def dependency():
        async with sessions() as db:
            requests.append(db)
            yield db
    web.dependency_overrides[get_db] = dependency
    value = SimpleNamespace(web=web, sessions=sessions, admin=admin, stream=stream,
        reader=readers[0], manager=manager, log=log, requests=requests,
        path='/api/tasks/conversations/c/events', token=create_access_token('u'))
    try:
        yield value
    finally:
        await stream.close()


async def get(scene, *, path=None, token=None, headers=None):
    async with AsyncClient(transport=ASGITransport(app=scene.web, raise_app_exceptions=False),
                           base_url='http://product') as client:
        return await client.get(path or scene.path, headers={
            'Authorization':'Bearer ' + (token or scene.token), **(headers or {})})


@pytest.mark.parametrize('error,status', [
    (ValueError('malformed cursor'), 400),
    (ConversationUnavailable('hidden'), 404),
    (ProjectionConflict('projection changed'), 409),
    (ResetStateConflict('binding changed'), 409),
    (ResetCapacityExceeded('snapshot budget'), 413),
    (StreamCapacityExceeded('connections full'), 503),
    (ReaderCapacityExceeded('reader budget'), 503),
    (RedisConnectionError('unavailable'), 503),
    (RedisTimeoutError('timeout'), 503),
    (LogUnavailable('rotation in progress'), 503),
    (LogLost('lost during handshake'), 503),
    (ReplayGap('trim during handshake'), 503),
    (GenerationMismatch('generation changed'), 503),
    (ReaderClosed('reader shutting down'), 503),
])
async def test_realtime_handshake_errors_are_http_errors_before_headers(http_scene, monkeypatch, error, status):
    opening = AsyncMock(side_effect=error)
    monkeypatch.setattr(http_scene.stream, 'open', opening)
    response = await get(http_scene)
    assert response.status_code == status, response.text
    opening.assert_awaited_once()
    assert not response.headers.get('content-type', '').startswith('text/event-stream')
    assert not http_scene.stream._handles


async def test_actual_bad_cursor_is_400_before_any_sse(http_scene):
    response = await get(http_scene, headers={'Last-Event-ID':'rt1.!!!!'})
    assert response.status_code == 400, response.text
    assert not http_scene.stream._handles
    assert not http_scene.reader._hubs


@pytest.mark.parametrize('mutation,status', [
    ('missing_token',401), ('disabled',401), ('stale_token',401),
    ('password_change',403), ('other_owner',404), ('deleted',404),
])
async def test_http_auth_guards_run_before_stream_admission(http_scene, monkeypatch, mutation, status):
    opening = AsyncMock(side_effect=AssertionError('Unauthorized request reached stream'))
    monkeypatch.setattr(http_scene.stream, 'open', opening)
    headers = {}
    if mutation == 'missing_token':
        headers['Authorization'] = ''
    elif mutation == 'disabled':
        await http_scene.admin.execute("UPDATE users SET status='disabled'")
    elif mutation == 'stale_token':
        await http_scene.admin.execute('UPDATE users SET token_version=1')
    elif mutation == 'password_change':
        await http_scene.admin.execute('UPDATE users SET must_change_password=true')
    elif mutation == 'deleted':
        await http_scene.admin.execute('UPDATE conversations SET deleted_at=now()')
    else:
        await http_scene.admin.execute("INSERT INTO users(id,email,normalized_email,password_hash,display_name) VALUES ('other','other@example.test','other@example.test','x','Other'); UPDATE conversations SET user_id='other'")
    response = await get(http_scene, headers=headers)
    assert response.status_code == status, response.text
    opening.assert_not_awaited()


async def drive_stream(scene, *, stop_at='body', on_body=None, spec_version='2.3'):
    disconnected = asyncio.Event()
    disconnect_boundary = asyncio.Event()
    messages = []
    request_sent = False
    status = None
    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {'type':'http.request','body':b'','more_body':False}
        await disconnected.wait()
        return {'type':'http.disconnect'}
    async def send(message):
        nonlocal status
        messages.append(message)
        if message['type'] == 'http.response.start':
            status = message['status']
            if stop_at in {'start', 'cancel_start'} or status != 200:
                disconnect_boundary.set()
            if stop_at == 'cancel_start':
                raise asyncio.CancelledError()
            if stop_at == 'start' and spec_version == '2.4':
                raise OSError('client disconnected before first body')
            if stop_at == 'start' or message['status'] != 200:
                disconnected.set()
        if message['type'] == 'http.response.body' and message.get('body'):
            if on_body is not None and status == 200:
                await on_body(message)
            disconnect_boundary.set()
            if stop_at == 'external_cancel':
                await asyncio.Event().wait()
            if stop_at == 'body':
                if spec_version == '2.4':
                    raise OSError('client disconnected while sending body')
                disconnected.set()
    scope = {'type':'http','asgi':{'version':'3.0','spec_version':spec_version},
        'http_version':'1.1','method':'GET','scheme':'http','path':scene.path,
        'raw_path':scene.path.encode(),'query_string':b'', 'root_path':'',
        'headers':[(b'authorization', ('Bearer '+scene.token).encode())],
        'server':('product',80),'client':('test',1234)}
    serving = asyncio.create_task(scene.web(scope, receive, send))
    boundary_wait = asyncio.create_task(disconnect_boundary.wait())
    try:
        # Authentication and stream admission use real PG/Redis. Their latency
        # must not consume the deadline that verifies disconnect cleanup.
        ready, _ = await asyncio.wait(
            {serving, boundary_wait}, timeout=30,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not ready:
            raise TimeoutError('SSE did not reach the disconnect boundary')
        if stop_at == 'external_cancel' and not serving.done():
            serving.cancel()
        await asyncio.wait_for(serving, 3)
    except asyncio.CancelledError:
        if stop_at not in {'cancel_start', 'external_cancel'}:
            raise
    except ClientDisconnect:
        if spec_version != '2.4' or stop_at not in {'start', 'body'}:
            raise
    finally:
        if not serving.done():
            serving.cancel()
        boundary_wait.cancel()
        await asyncio.gather(serving, boundary_wait, return_exceptions=True)
    return messages


@pytest.mark.parametrize('spec_version', ['2.3', '2.4'])
@pytest.mark.parametrize('stop_at', ['body', 'start', 'cancel_start', 'external_cancel'])
async def test_http_disconnect_closes_only_its_handle_and_releases_request_transaction(http_scene, monkeypatch, stop_at, spec_version):
    scene = http_scene
    other = await scene.stream.open('c', authority=ConversationBusinessState(scene.sessions,'u',token_version=0))
    opened = []
    original = scene.stream.open
    async def open_checked(cid, *, authority, last_event_id=None):
        assert isinstance(authority, ConversationBusinessState)
        assert authority.actor_id == 'u' and authority.token_version == 0
        assert not scene.requests[-1].in_transaction()
        assert scene.sessions.kw['bind'].pool.checkedout() == 0
        handle = await original(cid, authority=authority, last_event_id=last_event_id)
        opened.append(handle)
        return handle
    monkeypatch.setattr(scene.stream, 'open', open_checked)
    try:
        messages = await drive_stream(scene, stop_at=stop_at, spec_version=spec_version)
        assert next(message['status'] for message in messages if message['type']=='http.response.start') == 200
        assert len(opened) == 1
        assert opened[0].closed
        assert scene.stream._handles == {other}
        assert not other.closed and scene.reader._hubs
        assert sum(len(hub.subscribers) for hub in scene.reader._hubs.values()) == 1
        assert scene.sessions.kw['bind'].pool.checkedout() == 0
    finally:
        await other.aclose()


async def test_http_authority_keeps_jwt_version_for_live_revocation(http_scene, monkeypatch):
    scene = http_scene
    opening = scene.stream.open
    handles = []
    async def capture(*args, **kwargs):
        handle = await opening(*args, **kwargs)
        handles.append(handle)
        return handle
    monkeypatch.setattr(scene.stream, 'open', capture)
    revoked = False
    async def revoke_after_first_body(message):
        nonlocal revoked
        if not revoked:
            revoked = True
            await scene.admin.execute('UPDATE users SET token_version=1')
            handles[0].next_authorize = 0
    try:
        await drive_stream(scene, stop_at='revocation', on_body=revoke_after_first_body)
    except ConversationUnavailable:
        pass
    except ExceptionGroup as errors:
        assert all(isinstance(error, ConversationUnavailable) for error in errors.exceptions)
    assert revoked and len(handles) == 1
    assert handles[0].closed
    assert not scene.stream._handles and not scene.reader._hubs
