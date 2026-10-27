"""Custom connections reuse native MCP governance without a public catalog."""
import json
import pytest
from test_native_mcps import native_mcps, native_agents, projection_database, TOKEN  # noqa: F401


def body(**changes):
    return {'scope_id': 'scope', 'name': 'company-tools', 'display_name': 'Company tools',
            'description': 'Internal tools', 'url': 'https://tools.example.invalid/mcp',
            'headers': {'Authorization': 'Bearer ' + TOKEN}, 'timeout': 30,
            'is_stateful': False, **changes}


async def test_custom_create_edit_and_read_never_expose_headers(native_mcps):
    client, sessions, admin, catalog = native_mcps
    client._transport.app.state.mcp_catalogs = {}
    response = await client.post('/api/mcps/custom', json=body())
    assert response.status_code == 201, response.text
    assert TOKEN not in response.text and response.json()['usable'] is False
    identifier = response.json()['id']
    original = await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions')
    view = await client.get(f'/api/mcps/{identifier}/configuration')
    assert view.status_code == 200, view.text
    assert view.json()['source'] == 'custom'
    assert view.json()['header_names'] == ['Authorization']
    assert TOKEN not in view.text and 'Bearer' not in view.text
    changed = await client.patch(f'/api/mcps/{identifier}/custom', json={'timeout': 45})
    assert changed.status_code == 200, changed.text
    current = await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions')
    assert current != original
    payload = json.loads(await admin.fetchval('SELECT payload FROM mcp_secret_revisions WHERE id=$1', current))
    assert payload['client']['mcp_config']['headers'] == body()['headers']
    assert payload['client']['mcp_config']['timeout'] == 45
    cleared = await client.patch(f'/api/mcps/{identifier}/custom', json={'headers': {}})
    assert cleared.status_code == 200, cleared.text
    current = await admin.fetchval('SELECT secret_version FROM capability_mcp_revisions')
    payload = json.loads(await admin.fetchval('SELECT payload FROM mcp_secret_revisions WHERE id=$1', current))
    assert payload['client']['mcp_config']['headers'] == {}
    audits = await admin.fetch('SELECT metadata FROM security_audit_events')
    assert TOKEN not in str(audits)
    assert catalog.calls == []


@pytest.mark.parametrize('changes', [
    {'url': 'file:///etc/passwd'}, {'url': 'https://user:password@tools.example/mcp'},
    {'url': 'https://tools.example/mcp?token=' + TOKEN}, {'url': 'https://tools.example/mcp#fragment'},
    {'headers': {'Authorization': TOKEN + '\r\nX-Other: value'}}, {'timeout': 0},
    {'headers': {'Host': 'gateway'}}, {'command': 'sh'}, {'is_stateful': 'false'},
])
async def test_custom_invalid_private_input_is_redacted(native_mcps, changes):
    client, sessions, admin, catalog = native_mcps
    response = await client.post('/api/mcps/custom', json=body(**changes))
    assert response.status_code in (400, 422), response.text
    assert TOKEN not in response.text
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions') == 0


async def test_custom_management_is_required_for_read_write_and_probe(native_mcps):
    client, sessions, admin, catalog = native_mcps
    created = await client.post('/api/mcps/custom', json=body())
    assert created.status_code == 201, created.text
    identifier = created.json()['id']
    await admin.execute('DELETE FROM role_bindings')
    for method, path, payload in [
        ('get', f'/api/mcps/{identifier}/configuration', None),
        ('patch', f'/api/mcps/{identifier}/custom', {'headers': {}}),
        ('post', f'/api/mcps/{identifier}/probe', {}),
        ('post', '/api/mcps/custom', body(name='denied')),
    ]:
        response = await client.request(method, path, **({'json': payload} if payload is not None else {}))
        assert response.status_code in (403, 404), response.text
    assert await admin.fetchval('SELECT count(*) FROM mcp_secret_revisions') == 1


async def test_catalog_edit_uses_saved_template_when_catalog_disabled(native_mcps):
    from test_native_mcps import install
    client, sessions, admin, catalog = native_mcps
    identifier = await install(client)
    client._transport.app.state.mcp_catalogs = {}
    response = await client.get(f'/api/mcps/{identifier}/configuration')
    assert response.status_code == 200, response.text
    assert response.json()['source'] == 'catalog'
    assert response.json()['configured_fields'] == ['tenant', 'token']
    assert TOKEN not in response.text
    response = await client.patch(f'/api/mcps/{identifier}', json={'values': {'token': 'rotated'}})
    assert response.status_code == 200, response.text
    assert 'rotated' not in response.text


async def test_probe_is_authorized_and_does_not_create_installation(native_mcps, monkeypatch):
    from app.services import mcp_probe
    client, sessions, admin, catalog = native_mcps
    calls = []
    async def probe(configuration, **kwargs):
        calls.append(configuration)
        return {'ok': True, 'tools': [{'name': 'lookup'}], 'storage_node_id': 'node'}
    monkeypatch.setattr(mcp_probe, 'probe_mcp', probe)
    configuration = {'name': 'company-tools', 'is_stateful': False,
                     'mcp_config': {'type': 'http_mcp', 'url': body()['url'], 'headers': body()['headers']}}
    response = await client.post('/api/mcps/probe', json={'scope_id': 'scope', 'configuration': configuration})
    assert response.status_code == 200, response.text
    assert response.json()['ok'] is True
    assert len(calls) == 1 and TOKEN not in response.text
    assert await admin.fetchval('SELECT count(*) FROM capability_installations') == 0
    created = await client.post('/api/mcps/custom', json=body())
    identifier = created.json()['id']
    response = await client.post(f'/api/mcps/{identifier}/probe', json={})
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert calls[-1].mcp_config.headers == body()['headers']


async def test_catalog_shape_errors_do_not_echo_credentials(native_mcps):
    client, _, _, _ = native_mcps
    response = await client.post('/api/mcps', json={'scope_id': 'scope', 'hub_id': 'fixture-mcp',
        'card_id': 'remote', 'values': TOKEN})
    assert response.status_code == 422 and TOKEN not in response.text


async def test_probe_rechecks_authority_and_revision_after_network(native_mcps, monkeypatch):
    from app.services import mcp_probe
    client, sessions, admin, _ = native_mcps
    created = await client.post('/api/mcps/custom', json=body())
    identifier = created.json()['id']
    async def changed(configuration, **kwargs):
        response = await client.patch(f'/api/mcps/{identifier}/custom', json={'timeout': 60})
        assert response.status_code == 200
        return {'ok': True, 'tools': []}
    monkeypatch.setattr(mcp_probe, 'probe_mcp', changed)
    response = await client.post(f'/api/mcps/{identifier}/probe', json={})
    assert response.status_code == 409, response.text
    async def revoked(configuration, **kwargs):
        await admin.execute('DELETE FROM role_bindings')
        return {'ok': True, 'tools': []}
    monkeypatch.setattr(mcp_probe, 'probe_mcp', revoked)
    response = await client.post(f'/api/mcps/{identifier}/probe', json={})
    assert response.status_code in (403, 404), response.text


async def test_saved_probe_rejects_disabled_during_network(native_mcps, monkeypatch):
    from app.services import mcp_probe
    client, _, _, _ = native_mcps
    created = await client.post('/api/mcps/custom', json=body())
    identifier = created.json()['id']
    async def disabled(configuration, **kwargs):
        response = await client.patch(f'/api/mcps/{identifier}', json={'enabled': False})
        assert response.status_code == 200
        return {'ok': True, 'tools': []}
    monkeypatch.setattr(mcp_probe, 'probe_mcp', disabled)
    response = await client.post(f'/api/mcps/{identifier}/probe', json={})
    assert response.status_code == 409, response.text


async def test_draft_probe_does_not_require_a_persisted_connection_name(native_mcps, monkeypatch):
    from app.services import mcp_probe
    client, _, admin, _ = native_mcps
    seen = []
    async def probe(configuration, **kwargs):
        seen.append(configuration)
        return {'ok': False, 'tools': [], 'error_code': 'authentication_required',
                'error': 'MCP server requires authentication', 'storage_node_id': 'node'}
    monkeypatch.setattr(mcp_probe, 'probe_mcp', probe)
    response = await client.post('/api/mcps/probe', json={
        'scope_id': 'scope', 'configuration': {'is_stateful': False,
            'mcp_config': {'type': 'http_mcp', 'url': body()['url'], 'headers': body()['headers']}}})
    assert response.status_code == 200, response.text
    assert response.json()['error_code'] == 'authentication_required'
    assert seen[0].name == 'mcp-probe'
    assert TOKEN not in response.text
    assert await admin.fetchval('SELECT count(*) FROM capability_installations') == 0


async def test_draft_loopback_diagnostic_does_not_make_network_call(native_mcps, monkeypatch):
    from app.services import mcp_probe
    client, _, _, _ = native_mcps
    async def unexpected(*args, **kwargs):
        pytest.fail('Blocked target must not start network discovery')
    monkeypatch.setattr(mcp_probe, 'probe_mcp', unexpected)
    response = await client.post('/api/mcps/probe', json={
        'scope_id': 'scope', 'configuration': {'is_stateful': False,
            'mcp_config': {'type': 'http_mcp', 'url': 'http://127.0.0.1:7676/mcp'}}})
    assert response.status_code == 422, response.text
    assert response.json()['detail']['code'] == 'blocked_target'
