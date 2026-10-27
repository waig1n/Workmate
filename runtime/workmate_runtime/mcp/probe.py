"""Bounded MCP discovery from the Worker network, never tool execution."""
import asyncio
import logging
import socket
import ssl
import errno
from contextvars import ContextVar
import re
from urllib.parse import urlsplit, unquote, parse_qsl

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from contextlib import asynccontextmanager
from .http_transport import validate_target, PinnedTransport, MCPTargetBlocked, MCPDNSFailure

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

def classify_probe_error(error):
    """Inspect typed causes, never vendor messages that may embed credentials."""
    pending, errors, seen = [error], [], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        errors.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
    for current in errors:
        if isinstance(current, MCPTargetBlocked):
            return 'blocked_target'
        if isinstance(current, httpx.HTTPStatusError):
            code = {401:'authentication_required',403:'permission_denied',404:'endpoint_not_found',
                    429:'busy'}.get(current.response.status_code)
            if code:
                return code
    if any(isinstance(item, (socket.gaierror, MCPDNSFailure)) for item in errors):
        return 'dns_error'
    if any(isinstance(item, ssl.SSLError) for item in errors):
        return 'tls_error'
    if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in errors):
        return 'timeout'
    if any(isinstance(item, ConnectionRefusedError) or
           isinstance(item, OSError) and item.errno == errno.ECONNREFUSED for item in errors):
        return 'connection_refused'
    if any(isinstance(item, (httpx.NetworkError, OSError)) for item in errors):
        return 'network_error'
    return 'protocol_error'


MAX_TOOLS = 100
MAX_PAGES = 5
PROBE_SECONDS = 15

# These are the loggers used by the locked MCP/httpx/httpcore transport stack.
# Query-bearing catalog URLs and remote messages are private. Suppress those
# records only within a probe context (inherited by SDK child tasks), before
# any handler/formatter sees message, exception or extra fields. Other tasks
# and unrelated application loggers retain their ordinary logging behavior.
_private_probe = ContextVar('private_mcp_probe', default=False)


class _PrivateProbeLogs(logging.Filter):
    def filter(self, record):
        return not _private_probe.get()


_probe_log_filter = _PrivateProbeLogs()
for _name in ('httpx', 'httpcore.connection', 'httpcore.http11', 'httpcore.http2',
              'httpcore.proxy', 'httpcore.socks', 'mcp.client.sse',
              'mcp.client.streamable_http', 'mcp.shared.tool_name_validation', 'client'):
    logging.getLogger(_name).addFilter(_probe_log_filter)



@asynccontextmanager
async def probe_streams(config, client):
    path = urlsplit(config.url).path
    if path.endswith('/sse') or path.endswith('/messages/'):
        # SSE SDK owns this client context; return the already configured client.
        # Its same-origin endpoint messages retain pinning and redirect controls.
        @asynccontextmanager
        async def borrowed(**kwargs):
            yield client
        async with sse_client(config.url, headers=config.headers, timeout=10,
                              sse_read_timeout=10, httpx_client_factory=borrowed) as streams:
            yield streams[0], streams[1], None
    else:
        async with streamable_http_client(config.url, http_client=client) as streams:
            yield streams


def private_fragments(config):
    values = set()
    for value in (config.headers or {}).values():
        values.add(value)
        values.update(value.split())
    # Custom URLs cannot carry query credentials; path tokens are also private.
    values.update(unquote(part) for part in urlsplit(config.url).path.split('/') if part)
    values.update(value for _, value in parse_qsl(urlsplit(config.url).query))
    return {value for value in values if value}


async def preserve_probe_http_status(response):
    # MCP SDK turns POST 404 into an untyped "Session terminated" error.
    # Preserve the HTTP evidence before that conversion; optional GET/DELETE
    # behavior remains owned by the SDK.
    if response.request.method == 'POST' and response.status_code in {401, 403, 404, 429}:
        response.raise_for_status()


async def probe_configuration(configuration, *, storage_node_id, blocked_hosts=()):
    result = {'ok': False, 'tools': [], 'error': None, 'error_code': None, 'storage_node_id': storage_node_id}
    config = configuration.mcp_config
    if config.type != 'http_mcp':
        return {**result, 'error_code': 'protocol_error', 'error': PROBE_ERRORS['protocol_error']}
    log_token = _private_probe.set(True)
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            address = await validate_target(config.url, blocked_hosts=blocked_hosts, allow_query=True)
            async with httpx.AsyncClient(transport=PinnedTransport(config.url, address),
                    headers=config.headers, timeout=min(config.timeout or 10, 10),
                    follow_redirects=False, trust_env=False,
                    event_hooks={'response':[preserve_probe_http_status]}) as client:
                async with probe_streams(config, client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        names = []
                        cursor = None
                        for _ in range(MAX_PAGES):
                            page = await session.list_tools(cursor=cursor)
                            for tool in page.tools:
                                # Never reflect remote descriptions, schemas or credentials.
                                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', tool.name):
                                    continue
                                if any(value in tool.name for value in private_fragments(config)):
                                    continue
                                if tool.name not in names:
                                    names.append(tool.name)
                                if len(names) >= MAX_TOOLS:
                                    break
                            cursor = page.nextCursor
                            if not cursor or len(names) >= MAX_TOOLS:
                                break
                        result['tools'] = [{'name': name, 'description': ''} for name in names]
                        result['ok'] = True
    except Exception as error:
        # SDK/transport exception text can contain URLs and authorization headers.
        code = classify_probe_error(error)
        result.update(ok=False, tools=[], error_code=code, error=PROBE_ERRORS[code])
    finally:
        _private_probe.reset(log_token)
    return result
