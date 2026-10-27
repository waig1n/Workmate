"""Self-contained HTTP admission used in Worker probes and sandbox gateways.

Included in the sandbox gateway bundle; keep dependencies independent of Worker/Product.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
import httpx
from agentscope.mcp import MCPClient
from pydantic import PrivateAttr
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client


class MCPTargetBlocked(ValueError):
    """Target violates the configured connection boundary."""


class MCPDNSFailure(ValueError):
    """Target did not resolve to any address."""


async def _resolve_host(host, port):
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({row[4][0] for row in rows})


async def validate_target(url, *, blocked_hosts=(), blocked_addresses=(), resolver=_resolve_host, allow_query=False):
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
            parsed.username or parsed.password or (parsed.query and not allow_query) or parsed.fragment):
        raise ValueError('Invalid MCP target')
    host = parsed.hostname.lower().rstrip('.')
    forbidden = {'localhost', 'gateway', 'runtime', 'postgres', 'redis', 'rustfs', 'metadata.google.internal',
                 socket.gethostname().lower(), *blocked_hosts}
    if host in forbidden or host.endswith('.localhost'):
        raise MCPTargetBlocked('Blocked MCP target')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addresses = await resolver(host, port)
    blocked = set(blocked_addresses)
    for name in forbidden:
        try:
            blocked.update(await resolver(name, port))
        except OSError:
            pass
    if not addresses:
        raise MCPDNSFailure('Unresolved MCP target')
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
                or ip.is_reserved or address in blocked or address == '100.100.100.200'
                or getattr(ip, 'ipv4_mapped', None)):
            raise MCPTargetBlocked('Blocked MCP target')
    return addresses[0]


class BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > 1024 * 1024:
                raise ValueError('MCP response exceeds discovery limit')
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    """Never resolve the supplied hostname again; preserve HTTPS verification/SNI."""
    def __init__(self, url, address, *, discovery_limit=True):
        self.origin = httpx.URL(url)
        self.address = address
        self.discovery_limit = discovery_limit
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (request.url.scheme, request.url.host, request.url.port) != (
                self.origin.scheme, self.origin.host, self.origin.port):
            raise MCPTargetBlocked('MCP origin changed')
        request.headers['accept-encoding'] = 'identity'
        request.headers['host'] = self.origin.netloc.decode()
        request.extensions['sni_hostname'] = self.origin.host
        request.url = request.url.copy_with(host=self.address)
        response = await self.inner.handle_async_request(request)
        if response.headers.get('content-encoding', 'identity').lower() != 'identity':
            await response.aclose()
            raise ValueError('Compressed MCP discovery responses are disabled')
        if response.is_redirect:
            await response.aclose()
            raise MCPTargetBlocked('MCP redirects are disabled')
        if self.discovery_limit:
            response.stream = BoundedStream(response.stream)
        return response

    async def aclose(self):
        await self.inner.aclose()


class GovernedHTTPMCPClient(MCPClient):
    _blocked_hosts: tuple = PrivateAttr(default=())
    _blocked_addresses: tuple = PrivateAttr(default=())

    @asynccontextmanager
    async def _create_http_client(self):
        config = self.mcp_config
        async with asyncio.timeout(min(config.timeout or 30, 30)):
            address = await validate_target(config.url, blocked_hosts=self._blocked_hosts,
                                            blocked_addresses=self._blocked_addresses)
        def factory(**kwargs):
            return httpx.AsyncClient(**kwargs, transport=PinnedTransport(
                config.url, address, discovery_limit=False), trust_env=False, follow_redirects=False)
        path = urlsplit(config.url).path
        if path.endswith('/sse') or path.endswith('/messages/'):
            async with sse_client(config.url, headers=config.headers, timeout=config.timeout or 30,
                    sse_read_timeout=config.timeout or 30, httpx_client_factory=factory) as streams:
                yield streams
        else:
            async with factory(headers=config.headers, timeout=config.timeout or 30) as client:
                async with streamable_http_client(config.url, http_client=client) as streams:
                    yield streams


def governed_client_factory(spec):
    spec = dict(spec)
    policy = spec.pop('_workmate_http_policy', None)
    if policy is None:
        return MCPClient.model_validate(spec)
    if not isinstance(policy, dict) or spec.get('mcp_config', {}).get('type') != 'http_mcp':
        raise ValueError('Invalid MCP network policy')
    client = GovernedHTTPMCPClient.model_validate(spec)
    client._blocked_hosts = tuple(policy.get('blocked_hosts', ()))
    client._blocked_addresses = tuple(policy.get('blocked_addresses', ()))
    return client
