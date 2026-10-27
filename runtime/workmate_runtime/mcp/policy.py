"""Worker-derived network policy for privately published custom HTTP MCPs."""
import asyncio
from urllib.parse import urlsplit

from . import http_transport
from .gateway import MCPGatewayError


async def build_http_policy(product_api_url: str | None) -> dict:
    host = urlsplit(product_api_url).hostname if product_api_url else None
    hosts = ['gateway', 'runtime', 'postgres', 'redis', 'rustfs'] + ([host] if host else [])
    addresses = set()
    async with asyncio.timeout(10):
        for platform_host in hosts:
            try:
                addresses.update(await http_transport._resolve_host(platform_host, 80))
            except OSError:
                if platform_host == host:
                    raise MCPGatewayError('Platform network identity is unavailable') from None
    return {'blocked_hosts': hosts, 'blocked_addresses': sorted(addresses)}
