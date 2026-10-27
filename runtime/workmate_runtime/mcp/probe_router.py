"""Internal, service-authenticated Worker MCP connection test endpoint."""
import asyncio
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from workmate_product.mcp.definitions import MCPConfiguration
from .probe import probe_configuration, PROBE_ERRORS


def create_probe_router(settings, ready):
    router = APIRouter()
    slots = asyncio.Semaphore(4)

    @router.post('/workmate/mcps/probe')
    async def probe(request: Request):
        if not ready():
            raise HTTPException(503, {'code':'worker_unavailable','message':PROBE_ERRORS['worker_unavailable']})
        if slots.locked():
            raise HTTPException(429, {'code':'busy','message':PROBE_ERRORS['busy']})
        try:
            body = await request.json()
            configuration = MCPConfiguration.model_validate(body['configuration'])
        except (ValueError, TypeError, KeyError, ValidationError):
            # FastAPI's default validation response would reflect credentials.
            raise HTTPException(400, 'Invalid MCP connection declaration') from None
        host = urlsplit(settings.runtime.product_api_url).hostname
        # There is no await between the capacity check and acquisition.
        if slots.locked():
            raise HTTPException(429, {'code':'busy','message':PROBE_ERRORS['busy']})
        async with slots:
            return await probe_configuration(configuration, storage_node_id=settings.storage_node_id,
                                             blocked_hosts=(host,) if host else ())

    return router
