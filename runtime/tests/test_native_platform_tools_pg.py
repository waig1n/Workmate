"""Native exact Core ToolCall -> shared Product mutation, real PostgreSQL."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from agentscope.message import ToolCallBlock,ToolCallState
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.assembly.platform_tools import NativePlatformTools,PlatformCallBinding
from test_platform_mutations import platform_database,approve,request
from test_execution_context import database


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_stream", [False, True])
async def test_native_tool_create_uses_product_actor_and_exact_call_receipt(platform_database, durable_stream):
    sessions,admin=platform_database
    req=request()
    await approve(sessions,req)
    async def check(): pass
    guard=SimpleNamespace(owner=ExecutionOwner('s','t','worker',1),reply_id='reply',node_id='node',check=check)
    context=SimpleNamespace(session_id='s',task_id='t',agent_id='a',actor_user_id='u',conversation_id='c')
    adapter=NativePlatformTools(sessions,context,guard,None)
    middleware=PlatformCallBinding(adapter)
    agent=SimpleNamespace(state=SimpleNamespace(session_id='s',reply_id='reply'))
    tool=next(t for t in adapter.tools() if t.name=='agent_create')
    call=ToolCallBlock(id=req.tool_call_id,name='agent_create',input=json.dumps(req.arguments),state=ToolCallState.ALLOWED)
    async def next_handler(**kwargs):
        result=await tool.call(**req.arguments)
        yield result
    if durable_stream:
        from workmate_runtime.execution.running import RunningExecutionController
        guard.next_core, guard.stop_claim = None, None
        queue = asyncio.Queue()
        engine = SimpleNamespace(reply_stream=lambda _: middleware.on_acting(
            agent, {'tool_call': call}, next_handler))
        await RunningExecutionController._produce(None, guard, engine, None, queue)
        chunks = [queue.get_nowait() for _ in range(queue.qsize())]
        assert adapter.current.get() is None
    else:
        chunks=[c async for c in middleware.on_acting(agent,{'tool_call':call},next_handler)]
    assert len(chunks)==1
    assert await admin.fetchval("SELECT created_by FROM agents WHERE slug='created'")=='u'
    assert await admin.fetchval('SELECT tool_call_id FROM tool_execution_receipts')=='call'
    await admin.execute("UPDATE runtime_sessions SET owner_id='new-worker'")
    with pytest.raises(ValueError,match='authority'):
        _=[c async for c in middleware.on_acting(agent,{'tool_call':call},next_handler)]


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',[False,'permission','hidden'])
async def test_native_skill_tool_packages_and_publishes_to_injected_object_store(platform_database,tmp_path,revoke):
    """Real acting identity, isolated packaging, Product receipt and blob injection."""
    from workmate_runtime.execution.resources import TaskResourceBackendMixin
    from workmate_runtime.assembly.platform_skill_package import package_skill
    from workmate_product.persistence.models import Scope,ScopeClosure,ScopeMembership,Role,Permission,RolePermission,RoleBinding
    from workmate_product.application.skill_repository import load_skill_artifact
    sessions,admin=platform_database
    async with sessions.begin() as db:
        db.add(Scope(id='org',kind='organization',name='Organization'))
        db.add_all([Role(id='r',key='manager',display_name='Manager'),Permission(id='p',key='skill.manage')])
        await db.flush()
        db.add_all([ScopeClosure(ancestor_id='org',descendant_id='org',depth=0),
            RolePermission(id='rp',role_id='r',permission_id='p'),RoleBinding(user_id='u',scope_id='org',role_id='r')])
    if revoke=='permission':
        async with sessions.begin() as db:
            db.add(ScopeMembership(scope_id='org',user_id='u'))
    owner=ExecutionOwner('s','t','worker',1)
    class Backend(TaskResourceBackendMixin):
        def __init__(self): self._execution_owner=owner
        async def _exec_with_input(self,argv,data):
            process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await process.communicate(data)
            return SimpleNamespace(exit_code=process.returncode,stdout=stdout,stderr=stderr)
    binding=SimpleNamespace(owner=owner,backend=Backend(),workspace=SimpleNamespace(workdir=str(tmp_path)))
    draft=tmp_path/'working'/'example'
    draft.mkdir(parents=True)
    (draft/'SKILL.md').write_text('---\nname: from-native-tool\ndescription: Example\n---\nBody')
    from test_skill_blob_storage import MemoryBlobStore
    writes=[]
    class ObservedStore(MemoryBlobStore):
        async def put_verified(self,digest,raw):
            assert sessions.kw['bind'].sync_engine.pool.checkedout()==0
            writes.append(digest)
            async with admin.transaction():
                await admin.fetchval("SELECT id FROM scopes WHERE id='org' FOR UPDATE NOWAIT")
                if revoke:
                    await admin.execute('DELETE FROM role_bindings')
            return await super().put_verified(digest,raw)
    store=ObservedStore()
    sessions.configure(info={'skill_blob_store':store})
    raw,summary=await package_skill(binding,'working/example')
    arguments={'scope_id':'org','skill_directory':'working/example','expected_sha256':summary['sha256'],
               'display_name':None,'description':None}
    await approve(sessions,request().model_copy(update={'tool_name':'skill_install','arguments':arguments}))
    async def check(): pass
    guard=SimpleNamespace(owner=owner,reply_id='reply',node_id='node',check=check)
    context=SimpleNamespace(session_id='s',task_id='t',agent_id='a',actor_user_id='u',conversation_id='c')
    adapter=NativePlatformTools(sessions,context,guard,binding)
    middleware=PlatformCallBinding(adapter)
    agent=SimpleNamespace(state=SimpleNamespace(session_id='s',reply_id='reply'))
    tool=next(t for t in adapter.tools() if t.name=='skill_install')
    call=ToolCallBlock(id='call',name='skill_install',input=json.dumps(arguments),state=ToolCallState.ALLOWED)
    async def execute(**kwargs):
        yield await tool.call(**arguments)
    if revoke=='hidden':
        from workmate_product.application.platform_types import PlatformToolError
        with pytest.raises(PlatformToolError) as denied:
            _=[c async for c in middleware.on_acting(agent,{'tool_call':call},execute)]
        assert denied.value.status_code==404
        assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==0
        assert writes==[summary['sha256']]
        return
    chunks=[c async for c in middleware.on_acting(agent,{'tool_call':call},execute)]
    assert len(chunks)==1
    assert adapter.current.get() is None
    assert writes==[summary['sha256']]
    if revoke:
        assert 'permission_denied' in json.dumps(chunks[0].model_dump(mode='json'))
        assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==0
        assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==0
        assert len(store.objects)==1
        return
    row=await admin.fetchrow('SELECT digest,storage_key FROM skill_artifacts')
    assert row['digest']==summary['sha256']
    assert row['storage_key'].endswith(summary['sha256']+'.zip')
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==1
    assert await admin.fetchval('SELECT count(*) FROM tool_execution_receipts')==1
    async with sessions() as db:
        assert (await load_skill_artifact(db,summary['sha256'])).archive==raw
