import json
from types import SimpleNamespace
import httpx,pytest
from agentscope.workspace._gateway_client import GatewayClient
from workmate_runtime.mcp.gateway import MemoryGatewayClient,MCPGatewayError

@pytest.mark.asyncio
async def test_private_gateway_requests_stay_in_memory_and_nonce_precedes_auth():
    class Backend:
        async def write_file(self,*args):
            raise AssertionError('Secrets must not be written into the sandbox')
        async def exec_shell(self,*args,**kwargs):
            raise AssertionError('Secrets must not be placed in shell arguments')
    observed=[]
    async def server(request):
        observed.append(request)
        if request.url.path=='/health':
            assert 'authorization' not in request.headers
            return httpx.Response(200,json={'instance_nonce':'nonce'})
        assert request.headers['authorization']=='Bearer gateway-token'
        assert json.loads(request.content)=={'secret':'private-value'}
        return httpx.Response(200,json={'ok':True})
    source=GatewayClient(Backend(),12345,auth_token='gateway-token',instance_nonce='nonce')
    http=httpx.AsyncClient(transport=httpx.MockTransport(server))
    gateway=MemoryGatewayClient(source,http_client=http)
    status,raw=await gateway.exec_request('POST','/mcps',body={'secret':'private-value'})
    assert status==200 and json.loads(raw)=={'ok':True}
    assert len(observed)==2
    await gateway.aclose()
    assert http.is_closed

@pytest.mark.asyncio
async def test_wrong_nonce_prevents_secret_transmission_and_transport_errors_are_sanitized():
    observed=[]
    async def server(request):
        observed.append(request)
        return httpx.Response(200,json={'instance_nonce':'different'})
    source=GatewayClient(object(),12345,auth_token='private-token',instance_nonce='expected')
    gateway=MemoryGatewayClient(source,http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
    try:
        with pytest.raises(MCPGatewayError):
            await gateway.exec_request('POST','/mcps',body={'secret':'private-value'})
        assert len(observed)==1 and not observed[0].content and 'authorization' not in observed[0].headers
    finally:
        await gateway.aclose()

@pytest.mark.asyncio
async def test_session_actor_owns_connect_calls_and_close_despite_cancelled_waiter():
    import asyncio
    from workmate_runtime.mcp.actor import SessionMCPActor,MCPActorError
    tasks=[]
    started,release=asyncio.Event(),asyncio.Event()
    class Stack:
        async def aclose(self):
            tasks.append(asyncio.current_task())
    class Client:
        is_stateful=True
        is_connected=True
        _stack=Stack()
        async def connect(self): tasks.append(asyncio.current_task())
        async def list_raw_tools(self):
            tasks.append(asyncio.current_task()); return []
        async def get_tool(self,name):
            async def call(**kwargs):
                tasks.append(asyncio.current_task())
                started.set()
                await release.wait()
                return {'result':kwargs['value']}
            return call
    actor=SessionMCPActor(Client())
    await actor.ready()
    waiter=asyncio.create_task(actor.submit('call','counter',{'value':1}))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    assert actor.busy
    with pytest.raises(MCPActorError): await actor.submit('close')
    release.set()
    await actor.submit('tools')
    assert not actor.busy
    assert await actor.submit('call','counter',{'value':2})=={'result':2}
    await actor.submit('close')
    assert len(set(tasks))==1
    assert actor.client._stack is None

@pytest.mark.asyncio
async def test_actor_failed_close_retains_exact_transport_and_blocks_false_retry():
    from workmate_runtime.mcp.actor import SessionMCPActor,MCPActorError
    class Stack:
        async def aclose(self): raise RuntimeError('private secret')
    class Client:
        is_stateful=True
        is_connected=True
        _stack=Stack()
        async def connect(self): pass
        async def list_raw_tools(self): return []
    client=Client()
    stack=client._stack
    actor=SessionMCPActor(client)
    await actor.ready()
    with pytest.raises(MCPActorError,match='unknown outcome') as error:
        await actor.submit('close')
    assert 'private' not in str(error.value)
    assert client._stack is stack and actor.failed
    with pytest.raises(MCPActorError): await actor.submit('close')

@pytest.mark.asyncio
async def test_gateway_routes_preserve_failed_actor_and_do_not_echo_configuration():
    from workmate_runtime.mcp.server import GatewayState,build_app
    class Client:
        name='private'
        is_stateful=True
        is_connected=False
        async def connect(self): raise RuntimeError('secret-value')
    state=GatewayState(lambda spec:Client())
    app=build_app(state,auth_token='token',instance_nonce='nonce')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://gateway') as client:
        assert (await client.post('/mcps',json={})).status_code==401
        headers={'Authorization':'Bearer token'}
        response=await client.post('/mcps?agent_id=a&session_id=s',headers=headers,json={'secret':'secret-value'})
        assert response.status_code==503 and 'secret-value' not in response.text
        assert ('a','s','private') in state.actors
        response=await client.delete('/mcps/private?agent_id=a&session_id=s',headers=headers)
        assert response.status_code==409 and ('a','s','private') in state.actors
        response=await client.post('/mcps?agent_id=a&session_id=s',headers=headers,json={})
        assert response.status_code==409

@pytest.mark.asyncio
async def test_worker_workspace_close_retains_handles_on_gateway_close_failure(monkeypatch):
    from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
    class Gateway:
        attempts=0
        async def aclose(self):
            self.attempts+=1
            if self.attempts==1: raise RuntimeError('cannot close')
    workspace=WorkerMCPWorkspace(workspace_id='test')
    gateway=Gateway()
    workspace._gateway=gateway
    backend=object()
    workspace._backend=backend
    workspace.is_alive=True
    with pytest.raises(RuntimeError): await workspace.close()
    assert workspace._gateway is gateway and workspace._backend is backend and workspace.is_alive
    await workspace.close()
    assert gateway.attempts==2 and workspace._gateway is None and not workspace.is_alive

@pytest.mark.asyncio
async def test_task_rpc_cancellation_keeps_unknown_resource_until_actual_response():
    import asyncio
    import mcp.types
    from agentscope.message import ToolResultState
    from agentscope.tool import ToolChunk
    from workmate_runtime.mcp.tools import NativeMCPTools,MCPConnection,_Call
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.transport.postgres import ExecutionOwner
    owner=ExecutionOwner('s','t','w',1)
    registry=TaskResourceRegistry()
    registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
    started,release=asyncio.Event(),asyncio.Event()
    async def server(request):
        if request.url.path=='/health': return httpx.Response(200,json={'instance_nonce':'n'})
        started.set()
        await release.wait()
        return httpx.Response(200,json={'chunk':ToolChunk(content=[],state=ToolResultState.RUNNING).model_dump(mode='json')})
    gateway=MemoryGatewayClient(GatewayClient(object(),12345,auth_token='t',instance_nonce='n'),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
    async def check(): pass
    guard=SimpleNamespace(owner=owner,reply_id='reply',check=check)
    descriptor=mcp.types.Tool(name='state/get',inputSchema={'type':'object','properties':{'value':{'type':'integer'}}})
    connection=MCPConnection('c','revision','server',(descriptor,))
    native=NativeMCPTools(gateway,guard,registry,'agent',[connection])
    tool=native.tools()[0]
    token=native.current.set(_Call('call',tool.name,'reply',{'value':1}))
    task=asyncio.create_task(tool(value=1))
    native.current.reset(token)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert connection.unresolved
    assert not await registry.mark_finished(owner,'mcp:call')
    release.set()
    assert await registry.cleanup_resource(owner,'mcp:call')
    assert not connection.unresolved and not connection.failed
    await gateway.aclose()

@pytest.mark.asyncio
async def test_task_transport_failure_never_counts_as_remote_cancellation():
    import mcp.types
    from workmate_runtime.mcp.tools import NativeMCPTools,MCPConnection,_Call
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.transport.postgres import ExecutionOwner
    owner=ExecutionOwner('s','t','w',1)
    registry=TaskResourceRegistry()
    registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
    async def server(request):
        if request.url.path=='/health': return httpx.Response(200,json={'instance_nonce':'n'})
        raise httpx.ReadTimeout('secret configuration')
    gateway=MemoryGatewayClient(GatewayClient(object(),12345,auth_token='t',instance_nonce='n'),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
    async def check(): pass
    guard=SimpleNamespace(owner=owner,reply_id='reply',check=check)
    connection=MCPConnection('c','revision','server',(mcp.types.Tool(name='update',inputSchema={}),))
    native=NativeMCPTools(gateway,guard,registry,'agent',[connection])
    tool=native.tools()[0]
    token=native.current.set(_Call('call',tool.name,'reply',{}))
    try:
        with pytest.raises(MCPGatewayError,match='unknown'): await tool()
    finally: native.current.reset(token)
    await gateway.aclose()
    assert connection.failed and connection.unresolved
    assert not await registry.cleanup_resource(owner,'mcp:call')

@pytest.mark.asyncio
async def test_real_core_stdio_mcp_retains_session_counter_and_closes_in_actor(tmp_path):
    import sys
    from agentscope.mcp import MCPClient
    from workmate_runtime.mcp.actor import SessionMCPActor
    script=tmp_path/'counter.py'
    script.write_text('''from mcp.server.fastmcp import FastMCP
server=FastMCP('counter')
value=0
@server.tool()
def increment()->int:
    global value
    value+=1
    return value
server.run(transport='stdio')
''')
    client=MCPClient.model_validate({'name':'counter','is_stateful':True,
        'mcp_config':{'type':'stdio_mcp','command':sys.executable,'args':['-u',str(script)]}})
    from workmate_runtime.mcp.server import GatewayState,build_app
    state=GatewayState(lambda body:client)
    app=build_app(state,auth_token='token',instance_nonce='nonce')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://gateway',
            headers={'Authorization':'Bearer token'}) as http:
        params={'agent_id':'a','session_id':'s'}
        assert (await http.post('/mcps',params=params,json={})).status_code==200
        first=await http.post('/mcps/counter/tools/increment',params=params,json={'arguments':{}})
        second=await http.post('/mcps/counter/tools/increment',params=params,json={'arguments':{}})
        assert first.status_code==second.status_code==200
        assert first.json()['chunk']['content'][0]['text']=='1'
        assert second.json()['chunk']['content'][0]['text']=='2'
        assert (await http.delete('/mcps/counter',params=params)).status_code==200
    assert not state.actors and not client.is_connected

@pytest.mark.asyncio
async def test_session_revision_reuse_and_replacement_blocked_by_unfinished_rpc(monkeypatch):
    from contextlib import asynccontextmanager
    from workmate_runtime.mcp.assembly import assemble_mcp_tools
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.transport.postgres import ExecutionOwner
    from workmate_product.mcp.definitions import MCPConfiguration
    from workmate_product.mcp.secret_store import PrivateMCPDefinition
    owner=ExecutionOwner('s','t','w',1)
    registry=TaskResourceRegistry()
    registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
    spec=SimpleNamespace(mcp_configurations=('version',))
    context=SimpleNamespace(initial_spec=spec,permission_version=1,agent_id='a')
    configuration=MCPConfiguration(name='counter',is_stateful=True,
        mcp_config={'type':'stdio_mcp','command':'python'})
    definition=SimpleNamespace(capability_id='c',secret_version='v1',client=configuration)
    @asynccontextmanager
    async def begin(): yield object()
    async def load(*args,**kwargs): return context,(definition,)
    monkeypatch.setattr('workmate_runtime.mcp.assembly.load_execution_mcp_definitions',load)
    requests=[]
    async def server(request):
        if request.url.path=='/health': return httpx.Response(200,json={'instance_nonce':'n'})
        requests.append((request.method,request.url.path))
        if request.method=='GET':
            return httpx.Response(200,json=[{'name':'counter','inputSchema':{}}])
        return httpx.Response(200,json={'ok':True})
    gateway=MemoryGatewayClient(GatewayClient(object(),12345,auth_token='t',instance_nonce='n'),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
    workspace=SimpleNamespace(_gateway=gateway)
    binding=SimpleNamespace(workspace=workspace)
    guard=SimpleNamespace(owner=owner)
    sessions=SimpleNamespace(begin=begin)
    try:
        tools,_=await assemble_mcp_tools(context,binding,sessions,guard,registry,storage_node_id='node')
        connection=workspace._native_mcp_connections['c']
        assert len(tools)==1 and requests==[('POST','/mcps'),('GET','/mcps/counter/tools')]
        await assemble_mcp_tools(context,binding,sessions,guard,registry,storage_node_id='node')
        assert workspace._native_mcp_connections['c'] is connection and len(requests)==2
        definition.secret_version='v2'
        connection.unresolved.add((owner,'unfinished'))
        with pytest.raises(MCPGatewayError,match='unresolved'):
            await assemble_mcp_tools(context,binding,sessions,guard,registry,storage_node_id='node')
        assert len(requests)==2
        connection.unresolved.clear()
        await assemble_mcp_tools(context,binding,sessions,guard,registry,storage_node_id='node')
        assert requests[2:]==[('DELETE','/mcps/counter'),('POST','/mcps'),('GET','/mcps/counter/tools')]
        assert workspace._native_mcp_connections['c'].revision=='v2'
    finally: await gateway.aclose()

@pytest.mark.asyncio
async def test_worker_bootstrap_ships_gateway_bundle_and_uses_memory_gateway(monkeypatch,tmp_path):
    from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
    files={}
    commands=[]
    process=SimpleNamespace(returncode=None,pid=1234)
    class Backend:
        def join_path(self,*parts): return "/".join(parts)
        async def write_file(self,path,data): files[path]=data
        async def start_process(self,args,**kwargs): commands.append(args); return process
    workspace=WorkerMCPWorkspace(workspace_id='test',host_workdir=str(tmp_path),gateway_port=12345)
    workspace._backend=Backend()
    async def ready(): return True
    async def health(timeout):
        assert isinstance(workspace._gateway,MemoryGatewayClient)
        return True
    monkeypatch.setattr(workspace,'_bootstrap_is_ready',ready)
    monkeypatch.setattr(workspace,'_wait_gateway_health',health)
    await workspace._setup_mcp_gateway()
    source=files[workspace._gateway_script]
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        assert '__main__.py' in archive.namelist()
        assert b'class SessionMCPActor' in archive.read('workmate_runtime/mcp/actor.py')
        assert b'async def run(' in archive.read('workmate_runtime/mcp/server.py')
        assert not any(name.startswith('workmate_product/') for name in archive.namelist())
    assert len(commands)==1 and workspace._gateway_process is process
    await workspace._gateway.aclose()

@pytest.mark.asyncio
async def test_gateway_process_descendant_failure_retains_exact_handle_for_retry(monkeypatch):
    from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
    workspace=WorkerMCPWorkspace(workspace_id='test')
    class Process:
        pid=1234
        returncode=0
        async def wait(self): return 0
    process=Process()
    workspace._gateway_process=process
    monkeypatch.setattr('workmate_runtime.mcp.workspace.os.killpg',lambda *args:None)
    with pytest.raises(MCPGatewayError,match='descendants'): await workspace._stop_gateway_process()
    assert workspace._gateway_process is process
    def absent(*args): raise ProcessLookupError()
    monkeypatch.setattr('workmate_runtime.mcp.workspace.os.killpg',absent)
    await workspace._stop_gateway_process()
    assert workspace._gateway_process is None

@pytest.mark.asyncio
async def test_gateway_routes_reject_invalid_scope_input_and_unknown_tools():
    from workmate_runtime.mcp.server import GatewayState,build_app
    state=GatewayState()
    app=build_app(state,auth_token='token',instance_nonce='nonce')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://gateway',
            headers={'Authorization':'Bearer token'}) as client:
        assert (await client.get('/health')).json()['instance_nonce']=='nonce'
        assert (await client.post('/mcps',json={})).status_code==400
        assert (await client.post('/mcps?agent_id=a&session_id=s',json={})).status_code==400
        assert (await client.get('/mcps/missing/tools')).status_code==404
        assert (await client.post('/mcps/missing/tools/unknown',json={'arguments':1})).status_code==400
    await state.close()

@pytest.mark.asyncio
async def test_mcp_acting_binding_validates_allowed_original_call_and_restores_context():
    import mcp.types
    from agentscope.message import ToolCallState
    from workmate_runtime.mcp.tools import NativeMCPTools,MCPConnection,MCPActingBinding
    from workmate_runtime.transport.postgres import ExecutionOwner
    owner=ExecutionOwner('s','t','w',1)
    gateway=MemoryGatewayClient(GatewayClient(object(),12345,auth_token='t',instance_nonce='n'))
    guard=SimpleNamespace(owner=owner,reply_id='r')
    connection=MCPConnection('c','v','server',(mcp.types.Tool(name='read',inputSchema={}),))
    adapter=NativeMCPTools(gateway,guard,object(),'agent',[connection])
    binding=MCPActingBinding(adapter)
    name=adapter.tools()[0].name
    call=SimpleNamespace(id='call',name=name,state=ToolCallState.ALLOWED,input='{}')
    agent=SimpleNamespace(state=SimpleNamespace(session_id='s',reply_id='r'))
    async def handle(**kwargs):
        assert adapter.current.get().id=='call'
        yield 'executed'
    try:
        assert [event async for event in binding.on_acting(agent,{'tool_call':call},handle)]==['executed']
        assert adapter.current.get() is None
        agent.state.reply_id='wrong'
        with pytest.raises(MCPGatewayError):
            async for event in binding.on_acting(agent,{'tool_call':call},handle): pass
    finally: await gateway.aclose()


@pytest.mark.asyncio
async def test_fixed_python_gateway_failure_still_cleans_and_never_becomes_alive(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
    from workmate_runtime.workspaces import WorkmateSandboxWorkspace
    process = SimpleNamespace(returncode=None, pid=1234)
    backend = SimpleNamespace(
        exec_shell=AsyncMock(side_effect=AssertionError('duplicate import probe')),
        join_path=lambda *parts: '/'.join(parts),
        write_file=AsyncMock(), start_process=AsyncMock(return_value=process),
    )
    workspace = WorkerMCPWorkspace(workspace_id='failure', host_workdir=str(tmp_path), gateway_port=12345)
    cleaned = []
    async def provision():
        workspace._backend = backend
    async def stop():
        if workspace._gateway_process is not None:
            cleaned.append(workspace._gateway_process)
            workspace._gateway_process = None
    health = AsyncMock(return_value=False)
    monkeypatch.setattr(WorkmateSandboxWorkspace, '_gateway_python', property(lambda self: '/usr/local/bin/python'))
    monkeypatch.setattr(workspace, '_provision_backend', provision)
    monkeypatch.setattr(workspace, '_ensure_workspace_layout', AsyncMock())
    monkeypatch.setattr(workspace, '_wait_gateway_health', health)
    monkeypatch.setattr(workspace, '_stop_gateway_process', stop)
    with pytest.raises(MCPGatewayError, match='did not become ready'):
        await workspace.initialize()
    backend.exec_shell.assert_not_awaited()
    backend.start_process.assert_awaited_once()
    health.assert_awaited_once_with(30.0)
    assert cleaned == [process]
    assert not workspace.is_alive
    assert workspace._gateway is None and workspace._backend is None
