"""Memory-only transport to the trusted Core Gateway; no sandbox request files."""
import json
import httpx
from agentscope.workspace._gateway_client import GatewayClient

MAX_GATEWAY_BYTES=20*1024*1024


class MCPGatewayError(RuntimeError):
    pass


class MemoryGatewayClient(GatewayClient):
    """Retain Core proxy contracts while removing credential-bearing tempfiles."""
    def __init__(self,source:GatewayClient,*,http_client=None):
        if (not isinstance(source,GatewayClient) or not source.auth_token or not source.instance_nonce
                or not isinstance(source.gateway_port,int) or not 1<=source.gateway_port<=65535):
            raise MCPGatewayError('A nonce-bound authenticated Core gateway is required')
        super().__init__(source.backend,source.gateway_port,timeout=source.timeout,
            auth_token=source.auth_token,instance_nonce=source.instance_nonce)
        self._http=http_client or httpx.AsyncClient(timeout=source.timeout,follow_redirects=False,trust_env=False)
        self._closed=False

    async def _request(self,method,path,*,params=None,body=None,authenticated=True):
        if self._closed or not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
            raise MCPGatewayError('Invalid or closed MCP gateway request')
        headers={'Authorization':f'Bearer {self.auth_token}'} if authenticated else {}
        try:
            async with self._http.stream(method,f'http://127.0.0.1:{self.gateway_port}{path}',
                                         params=params,json=body,headers=headers) as response:
                result=bytearray()
                async for chunk in response.aiter_bytes(64*1024):
                    if len(result)+len(chunk)>MAX_GATEWAY_BYTES:
                        raise MCPGatewayError('MCP gateway response exceeds the transport limit')
                    result.extend(chunk)
                return response.status_code,bytes(result)
        except Exception:
            raise MCPGatewayError('MCP gateway request did not produce a complete response') from None

    async def exec_request(self,method,path,*,params=None,body=None,include_auth=True):
        if include_auth:
            status,raw=await self._request('GET','/health',authenticated=False)
            try:
                valid=status==200 and json.loads(raw).get('instance_nonce')==self.instance_nonce
            except Exception:
                valid=False
            if not valid:
                raise MCPGatewayError('MCP gateway instance identity is unavailable')
        return await self._request(method,path,params=params,body=body,authenticated=include_auth)

    async def aclose(self):
        if not self._closed:
            await self._http.aclose()
            self._closed=True
