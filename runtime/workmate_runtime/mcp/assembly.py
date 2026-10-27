"""Pin Product MCP revisions into reusable, Session-scoped native clients."""
import asyncio
import json
from urllib.parse import quote
import uuid

import mcp.types
from workmate_product.mcp.repository import load_execution_mcp_definitions
from .gateway import MCPGatewayError,MemoryGatewayClient
from .remote import RemoteGatewayClient
from .policy import build_http_policy
from .tools import MCPConnection,NativeMCPTools,MCPActingBinding


async def assemble_mcp_tools(context,binding,sessions,guard,registry,*,storage_node_id,knowledge_enabled=False, product_api_url=None):
    owner=guard.owner
    if (not context.initial_spec.mcp_configurations
            and not getattr(binding.workspace,'_native_mcp_connections',{})):
        return (),None
    async with sessions.begin() as db:
        latest,definitions=await load_execution_mcp_definitions(db,session_id=owner.session_id,
            task_id=owner.task_id,worker_id=owner.worker_id,execution_epoch=owner.execution_epoch,
            storage_node_id=storage_node_id,knowledge_enabled=knowledge_enabled)
    if latest.initial_spec!=context.initial_spec or latest.permission_version!=context.permission_version:
        raise MCPGatewayError('MCP preparation authority changed')
    workspace=binding.workspace
    if not definitions and not getattr(workspace,'_native_mcp_connections',{}):
        return (),None
    gateway=workspace._gateway
    if not isinstance(gateway,(MemoryGatewayClient,RemoteGatewayClient)):
        raise MCPGatewayError('Worker MCP sandbox gateway is unavailable')
    if not hasattr(workspace,'_native_mcp_connections'):
        workspace._native_mcp_connections={}
        workspace._native_mcp_lock=asyncio.Lock()
    connections=workspace._native_mcp_connections
    params={'agent_id':context.agent_id,'session_id':owner.session_id}

    async def request(method,path,body=None):
        completed=False
        task=None
        resource_id=f'mcp-preparation:{uuid.uuid4()}'
        async def probe(): return completed
        async def cleanup():
            if task is not None: await asyncio.shield(task)
        registry.register_external(owner,resource_id,tool_call_id=resource_id,cleanup=cleanup,probe=probe)
        async def execute():
            nonlocal completed
            status,raw=await gateway.exec_request(method,path,params=params,body=body)
            if status!=200:
                raise MCPGatewayError('MCP configuration operation did not complete')
            payload=json.loads(raw)
            completed=True
            await registry.mark_finished(owner,resource_id)
            return payload
        task=asyncio.create_task(execute())
        task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async with workspace._native_mcp_lock:
        target={definition.capability_id:definition for definition in definitions}
        if len({definition.client.name for definition in definitions})!=len(definitions):
            raise MCPGatewayError('MCP configuration names must be unique within a Session')
        for capability_id,connection in tuple(connections.items()):
            definition=target.get(capability_id)
            if definition is not None and connection.revision==definition.secret_version:
                if connection.failed or connection.unresolved:
                    raise MCPGatewayError('Previous MCP operation has an unknown outcome')
                continue
            if connection.failed or connection.unresolved:
                raise MCPGatewayError('Cannot replace MCP Session with unresolved operations')
            try:
                await request('DELETE',f'/mcps/{quote(connection.name,safe="")}')
            except BaseException:
                connection.failed=True
                raise
            del connections[capability_id]
        for definition in definitions:
            if definition.capability_id in connections:
                continue
            connection=MCPConnection(definition.capability_id,definition.secret_version,definition.client.name)
            connections[definition.capability_id]=connection
            try:
                payload = definition.client.model_dump(mode='json')
                if getattr(getattr(definition, 'card', None), 'hub_id', None) == 'workmate-custom-http':
                    # Derived from the private published card, never browser/model input.
                    payload['_workmate_http_policy'] = await build_http_policy(product_api_url)
                await request('POST','/mcps',payload)
                raw=await request('GET',f'/mcps/{quote(connection.name,safe="")}/tools')
                connection.descriptors=tuple(mcp.types.Tool.model_validate(tool) for tool in raw)
            except BaseException:
                connection.failed=True
                raise
        native=NativeMCPTools(gateway,guard,registry,context.agent_id,tuple(connections.values()))
    return native.tools(),MCPActingBinding(native)
