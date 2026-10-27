"""Route MCP connection tests to an available Worker without reflecting secrets."""
import re
import httpx
from fastapi import HTTPException


# Public error vocabulary mirrors the internal probe contract. Never use Worker error text.
PROBE_ERRORS = {'blocked_target': 'MCP target is blocked by network policy; use an approved address reachable '
                   'from the Worker',
 'dns_error': 'MCP hostname could not be resolved from the Worker',
 'connection_refused': 'MCP connection was refused; check the service listener, port and Worker '
                       'network access',
 'network_error': 'MCP network connection failed; check Worker routing and firewall access',
 'tls_error': 'MCP TLS validation failed; check the server certificate and hostname',
 'timeout': 'MCP connection test timed out',
 'authentication_required': 'MCP authentication failed; check the configured credentials',
 'permission_denied': 'MCP service denied access to this connection',
 'endpoint_not_found': 'MCP endpoint was not found; check the configured path',
 'protocol_error': 'MCP protocol initialization or tool discovery failed',
 'worker_unavailable': 'No available Worker could complete the MCP connection test',
 'busy': 'MCP connection test capacity is busy; retry shortly'}

async def probe_mcp(configuration, *, settings, node_directory=None, transport=None):
    node_id = None
    base_url = settings.runtime_base_url
    if settings.worker_nodes:
        candidates = node_directory.candidates() if node_directory is not None else ()
        if not candidates:
            raise HTTPException(503, {'code':'worker_unavailable','message':PROBE_ERRORS['worker_unavailable']})
        node_id = candidates[0]
        base_url = str(node_directory.nodes[node_id].base_url)
    if not settings.runtime_internal_token:
        raise HTTPException(503, {'code':'worker_unavailable','message':PROBE_ERRORS['worker_unavailable']})
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport, follow_redirects=False,
                trust_env=False, headers={'X-Workmate-Internal-Token': settings.runtime_internal_token}) as client:
            response = await client.post(str(base_url).rstrip('/') + '/workmate/mcps/probe',
                json={'configuration': configuration.model_dump(mode='json')})
            if response.status_code == 429:
                raise HTTPException(429, {'code':'busy','message':PROBE_ERRORS['busy']})
            response.raise_for_status()
            data = response.json()
        actual_node = data.get('storage_node_id')
        if not isinstance(actual_node, str) or (node_id and actual_node != node_id):
            raise ValueError('Worker identity mismatch')
        ok = data.get('ok') is True
        tools = []
        for item in data.get('tools', [])[:100] if ok else []:
            name = item.get('name')
            if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name):
                tools.append({'name': name, 'description': ''})
        code = data.get('error_code') if isinstance(data.get('error_code'), str) else None
        code = None if ok else code if code in PROBE_ERRORS else 'protocol_error'
        return {'ok': ok, 'tools': tools, 'storage_node_id': actual_node,
                'error_code': code, 'error': None if ok else PROBE_ERRORS[code]}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise HTTPException(503, {'code':'worker_unavailable','message':PROBE_ERRORS['worker_unavailable']}) from None
