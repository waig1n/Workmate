"""Bounded, non-redirecting HTTP and cancellation-safe connection lifecycle."""
import asyncio
import json
from functools import wraps
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE = 2 * 1024 * 1024


class ChannelTransportError(RuntimeError):
    """Messages are fixed codes, never SDK exceptions containing credentials."""


def safe_transport(method):
    """Suppress raw transport errors that may embed tokens, URLs or payloads."""
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except ChannelTransportError:
            raise
        except Exception:
            raise ChannelTransportError('platform_transport_failed') from None
    return wrapped


def websocket_url(value: str, suffix: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'wss' or not parsed.hostname
        or not (parsed.hostname == suffix or parsed.hostname.endswith('.' + suffix))
        or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        raise ChannelTransportError('invalid_platform_endpoint')
    return value


class Adapter:
    def __init__(self, credentials: dict):
        self.ready = asyncio.Event()
        self._credentials = dict(credentials)
        self._http = httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False)
        self._socket = None

    def supports_typing(self) -> bool:
        return False

    async def sync_typing(self, targets):
        pass

    async def stop_typing(self, target):
        pass

    def supports_stream(self, target: dict) -> bool:
        return False

    async def stream(self, target, text, *, stream_id, receipt=None, finish=False, sequence=1):
        raise ChannelTransportError('stream_unsupported')

    async def request(self, method: str, url: str, **kwargs) -> dict:
        try:
            async with self._http.stream(method, url, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    raise ChannelTransportError('platform_http_error')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE:
                        raise ChannelTransportError('platform_response_too_large')
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ChannelTransportError('invalid_platform_response')
                return value
        except (httpx.HTTPError, ValueError):
            raise ChannelTransportError('platform_request_failed') from None

    @safe_transport
    async def listen(self, emit, shutdown: asyncio.Event):
        runner = asyncio.create_task(self.run(emit))
        stopper = asyncio.create_task(shutdown.wait())
        try:
            done, _ = await asyncio.wait((runner, stopper), return_when=asyncio.FIRST_COMPLETED)
            if runner in done:
                await runner
        finally:
            self.ready.clear()
            runner.cancel()
            stopper.cancel()
            await asyncio.gather(runner, stopper, return_exceptions=True)
            if self._socket is not None:
                await self._socket.close()
                self._socket = None

    @safe_transport
    async def close(self):
        self.ready.clear()
        try:
            if self._socket is not None:
                await self._socket.close()
        finally:
            self._socket = None
            await self._http.aclose()

    @staticmethod
    def check(data, key='errcode'):
        if data.get(key, 0) not in (0, '0', None):
            raise ChannelTransportError('platform_rejected_request')
