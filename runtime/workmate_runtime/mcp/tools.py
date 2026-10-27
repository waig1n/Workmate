"""Task-tracked MCP calls over Session-owned gateway infrastructure."""
import asyncio
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass,field
import json
import re
from urllib.parse import quote

import mcp.types
from agentscope._utils._common import _json_loads_with_repair
from agentscope.message import ToolCallState
from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChunk
from agentscope.workspace._gateway_client import GatewayMCPTool
from .gateway import MCPGatewayError,MemoryGatewayClient
from .remote import RemoteGatewayClient


@dataclass
class MCPConnection:
    capability_id: str
    revision: str
    name: str
    descriptors: tuple=()
    unresolved: set=field(default_factory=set)
    failed: bool=False


class TrackedMCPTool(GatewayMCPTool):
    is_state_injected=True

    def __init__(self,adapter,connection,descriptor):
        super().__init__(connection.name,descriptor,adapter.gateway,
            agent_id=adapter.agent_id,session_id=adapter.guard.owner.session_id)
        self.name=f'mcp__{connection.name}__{re.sub(r"[^A-Za-z0-9_-]","x",descriptor.name)}'
        self.adapter,self.connection=adapter,connection

    async def __call__(self,_agent_state=None,**arguments):
        adapter=self.adapter
        call=adapter.current.get()
        if (call is None or call.name!=self.name or call.reply_id!=adapter.guard.reply_id
                or call.arguments!=arguments):
            raise MCPGatewayError('MCP operation requires its exact allowed Core ToolCall')
        await adapter.guard.check()
        owner=adapter.guard.owner
        resource_id=f'mcp:{call.id}'
        completed=False
        task=None
        async def probe():
            return completed
        async def cleanup():
            if task is not None:
                await asyncio.shield(task)
        adapter.registry.register_external(owner,resource_id,tool_call_id=call.id,cleanup=cleanup,probe=probe)
        self.connection.unresolved.add((owner,call.id))
        async def execute():
            nonlocal completed
            try:
                status,raw=await adapter.gateway.exec_request('POST',
                    f'/mcps/{quote(self.connection.name,safe="")}/tools/{quote(self._tool.name,safe="")}',
                    params={'agent_id':adapter.agent_id,'session_id':owner.session_id},body={'arguments':arguments})
                if status!=200:
                    raise MCPGatewayError('MCP operation has an unknown outcome')
                chunk=ToolChunk.model_validate(json.loads(raw)['chunk'])
                completed=True
                self.connection.unresolved.discard((owner,call.id))
                await adapter.registry.mark_finished(owner,resource_id)
                return chunk
            except BaseException:
                self.connection.failed=True
                raise MCPGatewayError('MCP operation has an unknown outcome') from None
        task=asyncio.create_task(execute())
        task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        return await asyncio.shield(task)


@dataclass(frozen=True)
class _Call:
    id: str
    name: str
    reply_id: str
    arguments: dict


class MCPActingBinding(MiddlewareBase):
    def __init__(self,adapter): self.adapter=adapter

    async def on_acting(self,agent,input_kwargs,next_handler):
        call=input_kwargs['tool_call']
        tool=self.adapter.by_name.get(call.name)
        token=None
        if tool is not None:
            if (call.state!=ToolCallState.ALLOWED or agent.state.session_id!=self.adapter.guard.owner.session_id
                    or agent.state.reply_id!=self.adapter.guard.reply_id):
                raise MCPGatewayError('MCP acting identity is stale')
            arguments=_json_loads_with_repair(call.input,tool.input_schema)
            if not isinstance(arguments,dict):
                raise MCPGatewayError('MCP input is not an object')
            token=self.adapter.current.set(_Call(call.id,call.name,agent.state.reply_id,arguments))
        try:
            async with aclosing(next_handler(**input_kwargs)) as stream:
                async for event in stream: yield event
        finally:
            if token is not None: self.adapter.current.reset(token)


class NativeMCPTools:
    def __init__(self,gateway,guard,registry,agent_id,connections):
        if not isinstance(gateway,(MemoryGatewayClient,RemoteGatewayClient)):
            raise MCPGatewayError('Native MCP requires memory-only authenticated transport')
        self.gateway,self.guard,self.registry,self.agent_id=gateway,guard,registry,agent_id
        self.current=ContextVar('workmate_mcp_acting',default=None)
        tools=[TrackedMCPTool(self,connection,descriptor) for connection in connections for descriptor in connection.descriptors]
        self.by_name={tool.name:tool for tool in tools}
        if len(self.by_name)!=len(tools):
            raise MCPGatewayError('MCP tools collide after provider name normalization')

    def tools(self): return tuple(self.by_name.values())
