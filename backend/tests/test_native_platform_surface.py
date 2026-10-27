"""Native API never exposes legacy token-only platform execution endpoints."""
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.main import build_app


PREFIX = '/internal/runtime/platform-tools'
TOKEN = 'fixture-legacy-internal-token'
REQUESTS = [
    ('profile', {'runtime_agent_id': 'agent'}),
    ('execute', {
        'runtime_agent_id': 'agent', 'session_id': 'session',
        'task_id': 'task', 'tool_call_id': 'call',
        'tool_name': 'agent_create',
        'arguments': {'slug': 'unauthorized', 'display_name': 'Unauthorized'},
    }),
]


def test_native_app_does_not_register_legacy_platform_routes():
    app = build_app()
    paths = set(app.openapi()['paths'])
    assert not {PREFIX+'/profile', PREFIX+'/execute'} & paths


@pytest.mark.parametrize('endpoint,payload', REQUESTS)
@pytest.mark.parametrize('with_token', [False, True])
async def test_native_legacy_platform_calls_are_404_even_with_valid_old_token(
    monkeypatch, endpoint, payload, with_token,
):
    monkeypatch.setenv('RUNTIME_INTERNAL_TOKEN', TOKEN)
    app = build_app()
    database_accesses = []

    async def forbidden_database():
        database_accesses.append(True)
        raise AssertionError('Legacy platform route reached database')
        yield  # This dependency must never be entered.

    app.dependency_overrides[get_db] = forbidden_database
    headers = {'X-Workmate-Internal-Token': TOKEN} if with_token else {}
    async with AsyncClient(transport=ASGITransport(app, raise_app_exceptions=False),
                           base_url='http://test') as client:
        response = await client.post(PREFIX+'/'+endpoint, json=payload, headers=headers)
    assert response.status_code == 404, response.text
    assert database_accesses == []
