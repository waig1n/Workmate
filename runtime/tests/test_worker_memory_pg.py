"""Real PostgreSQL memory namespace placement is durable across Workers."""
from polling_worker import polling_composition
import asyncio
import pytest
from workmate_runtime.assembly.memory import bind_memory_node,MemoryUnavailable
from test_execution_context import database


async def test_namespace_first_node_binding_cannot_be_replaced(database,polling_composition):
    sessions,admin=database
    results=await asyncio.gather(bind_memory_node(sessions,'u','a','node-one'),
        bind_memory_node(sessions,'u','a','node-two'),return_exceptions=True)
    assert sum(result is None for result in results)==1
    assert sum(isinstance(result,MemoryUnavailable) for result in results)==1
    node=await admin.fetchval('SELECT storage_node_id FROM runtime_memory_namespaces')
    await bind_memory_node(sessions,'u','a',node)
    with pytest.raises(MemoryUnavailable):
        await bind_memory_node(sessions,'u','a','third-node')
    assert await admin.fetchval('SELECT count(*) FROM runtime_memory_namespaces')==1


async def test_real_controller_stop_joins_memory_reply_before_cleanup(database,tmp_path,monkeypatch,polling_composition):
    from agentscope.message import TextBlock
    from workmate_runtime.assembly.memory import WorkerMemoryManager
    from workmate_runtime.execution.composition import compose_worker
    from workmate_runtime.config import RuntimeSettings
    from test_execution_context import record
    from test_core_engine import ScriptModel
    from test_worker_workspaces import StubWorkspace
    sessions,admin=database
    await record(sessions)
    entered=asyncio.Event()
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',lambda *_:ScriptModel([],entered))
    async def no_attestation(*args): pass
    monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',no_attestation)
    async def bind(user,agent):
        await bind_memory_node(sessions,user,agent,'node')
    memory=WorkerMemoryManager(tmp_path/'memory',ScriptModel([]),bind_node=bind)
    app=compose_worker(sessions.kw['bind'],RuntimeSettings(reme_enabled=True),
        storage_node_id='node',workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',
        max_file_bytes=1024,max_files=3,workspace_factory=StubWorkspace,drain_seconds=0)
    app.assembler.memory_manager=memory
    shutdown=asyncio.Event()
    running=asyncio.create_task(app.serve(shutdown))
    try:
        await asyncio.wait_for(entered.wait(),10)
        async with admin.transaction():
            await admin.execute("UPDATE tasks SET status='cancelling' WHERE id='t'")
            await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,
                kind,expected_epoch,payload,content_digest,reply_id)
                SELECT 'stop',id,conversation_id,active_task_id,'u','StopTask',execution_epoch,'{}',
                repeat('d',64),reply_id FROM runtime_sessions WHERE id='s'""")
        async with asyncio.timeout(12):
            while await admin.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")!='completed':
                assert not app.service.failures,app.service.failures
                await asyncio.sleep(.03)
        assert all(entry.references==0 for entry in memory.instances.values())
        assert await admin.fetchval('SELECT status FROM runtime_sessions')=='interrupted'
        assert memory.health()['pending_writes']==0
    finally:
        shutdown.set()
        await asyncio.wait_for(running,10)
        await memory.close()
