"""Full Access is a durable upgrade, never an edited initial execution spec."""
import asyncio
import json

import pytest
from test_realtime_redis_log import log  # noqa: F401
from workmate_product.application.commands import CommandRejected
from workmate_product.application.full_access import accept_full_access
from test_durable_projection import projection_database  # noqa: F401
from test_control_commands import parked,allow


async def prepared(sessions,admin):
    command=await parked(sessions,admin)
    await admin.execute("""UPDATE runtime_sessions SET owner_id='worker',lease_until=clock_timestamp()+interval '30 seconds';
        INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
        VALUES ('t','s','worker',1,'workspace');
        INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest)
        VALUES ('t','s',1,'{"approval_profile":"standard"}',repeat('d',64));""")
    return command.model_copy(update={'payload':{**command.payload,'full_access':True}})


async def accept(sessions,command,authorize=allow):
    async with sessions.begin() as db:
        return await accept_full_access(db,command,actor_user_id='u',authorize_resume=authorize)


async def test_upgrade_is_once_audited_without_mutating_task_or_snapshot(projection_database):
    sessions,admin=projection_database
    command=await prepared(sessions,admin)
    snapshot=await admin.fetchval('SELECT payload FROM runtime_snapshots')
    results=await asyncio.gather(accept(sessions,command),accept(sessions,command))
    assert sorted(r.duplicate for r in results)==[False,True]
    assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==1
    assert await admin.fetchval('SELECT approval_profile FROM tasks')=='standard'
    assert json.loads(await admin.fetchval('SELECT payload FROM runtime_execution_specs'))['approval_profile']=='standard'
    assert await admin.fetchval('SELECT payload FROM runtime_snapshots')==snapshot
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_enabled'")==1
    decision=json.loads(await admin.fetchval("SELECT payload FROM task_events WHERE event_type='hitl_decision'"))
    assert decision['scope']=='task' and decision['permissionMode']=='bypass'
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='permission_mode_changed'")==1


@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_sessions SET owner_id=NULL",
    "UPDATE runtime_sessions SET owner_id='other'",
    'UPDATE runtime_sessions SET execution_epoch=2',
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
])
async def test_unproven_or_stale_sandbox_cannot_upgrade(projection_database,mutation):
    sessions,admin=projection_database
    command=await prepared(sessions,admin)
    await admin.execute(mutation)
    with pytest.raises(CommandRejected):
        await accept(sessions,command)
    assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


async def test_current_authorization_denial_rolls_back_upgrade(projection_database):
    sessions,admin=projection_database
    command=await prepared(sessions,admin)
    async def deny(*args):
        return False
    with pytest.raises(CommandRejected):
        await accept(sessions,command,deny)
    assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==0


async def test_forged_full_access_flag_cannot_be_claimed_without_upgrade(projection_database):
    pytest.importorskip('agentscope')
    from test_control_commands import admit
    from workmate_runtime.transport.controls import ResumeStore
    sessions,admin=projection_database
    command=await prepared(sessions,admin)
    command=command.model_copy(update={'payload':{**command.payload,'full_access':False}})
    await admit(sessions,command)
    await admin.execute("UPDATE runtime_commands SET payload=jsonb_set(payload,'{full_access}','true') WHERE id='resume'")
    assert await ResumeStore(admin).claim_resume('resume','worker','node',authorize_resume=allow) is None
    assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==0
    assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'


@pytest.mark.parametrize('denied,repeat',[(False,False),(True,False),(False,True)])
async def test_real_core_resume_uses_exact_reply_and_keeps_deny_ceiling(projection_database,log,monkeypatch,denied,repeat):
    pytest.importorskip('agentscope')
    import os
    import asyncpg
    from agentscope.message import ToolCallBlock,TextBlock
    from workmate_contracts import Command
    from workmate_runtime.execution.running import RunningExecutionController
    from workmate_runtime.assembly.agent import _ExecutionConfiguration
    from workmate_runtime.permission_policy import ProductPermissionMiddleware,ExecutionPermissionPolicy
    from app.adapter.durable_interactions import interaction_projector
    from workmate_product.projections.store import consume_next
    from test_core_engine import ConfirmTool,make_engine
    from test_execution_controller import prepare_reply
    from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_product.config.realtime import RealtimePolicy
    from test_parked_stop import quiescent
    import test_core_engine
    from agentscope.model import ChatResponse

    class StreamingModel(test_core_engine.ScriptModel):
        async def _call_api(self, *args, **kwargs):
            original = await super()._call_api(*args, **kwargs)
            async def stream():
                async for response in original:
                    yield ChatResponse(content=response.content, is_last=False)
                    yield response
            return stream()

    monkeypatch.setattr(test_core_engine, 'ScriptModel', StreamingModel)
    sessions,admin=projection_database
    writer=RealtimeWriterFactory(sessions,log_manager=EventLogManager(sessions,log),
        log=log,policy=RealtimePolicy())
    await admin.execute("""UPDATE tasks SET status='created';
        UPDATE runtime_sessions SET active_task_id=NULL,execution_epoch=0,status='accepted',reply_id=NULL;
        UPDATE runtime_task_executions SET execution_epoch=0,status='accepted',reply_id=NULL;
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('a',64));""")
    schema=await admin.fetchval('SHOW search_path')
    pool=await asyncpg.create_pool(os.environ['WORKMATE_TEST_POSTGRES_DSN'],min_size=1,max_size=4,
        server_settings={'search_path':schema})
    try:
        initial_tool=ConfirmTool()
        if repeat:
            initial_tool.name='agent_create'  # Product-mandated confirmation survives BYPASS.
        initial_tool.safety_ask=True
        prepare,_=prepare_reply([[ToolCallBlock(id='a',name=initial_tool.name,input='{}')]],tool=initial_tool)
        controller=RunningExecutionController(pool,verify_cleanup=quiescent,realtime_writer_factory=writer)
        first=await controller.start('start','worker','node',prepare)
        while True:
            async with sessions.begin() as db:
                source=await consume_next(db,'c',project=interaction_projector())
            if source is None:
                break
        state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
        pending=state['pending_interactions'][0]
        execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
        await admin.execute("""INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
            VALUES ('t','s','worker',1,'workspace');
            INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest)
            VALUES ('t','s',1,'{"approval_profile":"standard"}',repeat('d',64));""")
        command=Command(command_id='resume',kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
            product_task_id='t',reply_id=first.reply_id,expected_epoch=1,interaction_id=pending['interaction_id'],
            request_version=pending['request_version'],payload={'decision_version':'upgrade','snapshot_id':execution['snapshot_id'],
                'state_version':execution['state_version'],'full_access':True,'decisions':[{'tool_call_id':'a','approved':True}]})
        await accept(sessions,command)
        tool=ConfirmTool()
        tool.name=initial_tool.name
        tool.safety_ask=repeat
        responses=[[ToolCallBlock(id='second',name=tool.name,input='{}')]] if repeat else [[TextBlock(text='done')]]
        seen=[]
        async def resume(owner,snapshot,guard):
            engine=make_engine(responses,state=snapshot.to_state(),tool=tool,
                middlewares=[ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id='t',approval_profile='standard',
                    denied_tools=[tool.name] if denied else [])),_ExecutionConfiguration('full_access','/workspace'),guard])
            assert engine.snapshot()==snapshot
            seen.append(engine)
            return engine
        async def authorized(*args):
            return True
        result=await controller.resume('resume','worker','node',resume,authorize_resume=authorized)
        assert result.reply_id==first.reply_id and result.owner==first.owner
        assert tool.calls==(0 if denied else 1)
        if repeat:
            assert result.status=='awaiting_interaction'
            assert seen[-1].agent.state.permission_context.mode.value=='bypass'
            while True:
                async with sessions.begin() as db:
                    source=await consume_next(db,'c',project=interaction_projector())
                if source is None:
                    break
            state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
            pending=state['pending_interactions'][0]
            execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
            command2=command.model_copy(update={'command_id':'resume2','interaction_id':pending['interaction_id'],
                'request_version':pending['request_version'],'payload':{'decision_version':'upgrade2',
                    'snapshot_id':execution['snapshot_id'],'state_version':execution['state_version'],
                    'full_access':True,'decisions':[{'tool_call_id':'second','approved':True}]}})
            await accept(sessions,command2)
            responses=[[TextBlock(text='done')]]
            result=await controller.resume('resume2','worker','node',resume,authorize_resume=authorized)
            assert result.reply_id==first.reply_id and tool.calls==2
            assert await admin.fetchval('SELECT count(*) FROM runtime_permission_upgrades')==2
        assert seen[-1].agent.state.permission_context.mode.value=='default'
        assert await controller.resume('resume','worker','node',resume,authorize_resume=authorized) is None
        assert await admin.fetchval("SELECT status FROM runtime_commands WHERE id='resume'")=='completed'
        while True:
            async with sessions.begin() as db:
                source=await consume_next(db,'c',project=interaction_projector())
            if source is None:
                break
        assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='task.full_access_reset'")==1
        message=await admin.fetchrow("SELECT content,snapshot_phase FROM task_messages WHERE role='assistant'")
        assert json.loads(message['content'])['text']=='done'
        assert message['snapshot_phase']=='final'
        assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type' LIKE 'TEXT_BLOCK_%'")==0
    finally:
        await pool.close()


@pytest.mark.parametrize('probe', ['isolated','same_namespace','failed','real'])
async def test_sandbox_proof_uses_bound_backend_result(projection_database,tmp_path,monkeypatch,probe):
    pytest.importorskip('agentscope')
    import os
    from types import SimpleNamespace
    from workmate_runtime.assembly.sandbox_proof import attest_task_sandbox
    from workmate_runtime.assembly.workspaces import WorkspaceIdentity
    from workmate_runtime.execution.controller import ExecutionAborted
    from workmate_runtime.transport.postgres import ExecutionOwner
    from test_worker_workspaces import manager
    sessions,admin=projection_database
    await admin.execute("""UPDATE runtime_sessions SET owner_id='worker',status='preparing',reply_id=NULL,
        lease_until=clock_timestamp()+interval '30 seconds';
        UPDATE runtime_task_executions SET status='preparing',reply_id=NULL;""")
    if probe == 'real':
        # Exercise production backend selection: nested containers retain PID
        # isolation without the host-only user namespace mount assumptions.
        from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
        workspaces=manager(tmp_path,factory=WorkerMCPWorkspace)
    else:
        workspaces=manager(tmp_path)
    owner=ExecutionOwner('s','t','worker',1)
    # A real Worker renews authority while the sandbox initializes. This unit
    # fixture has no Worker supervisor, so keep the same 30-second lease alive
    # through the production, owner-fenced renewal path instead of increasing it.
    from workmate_runtime.transport.postgres import ExecutionStore
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    renewal_stop = asyncio.Event()
    async def renew_preparation():
        pool = ProductWorkerPool(sessions.kw['bind'])
        while not renewal_stop.is_set():
            async with pool.acquire() as connection:
                await ExecutionStore(connection).renew(owner, lease_seconds=30)
            try:
                await asyncio.wait_for(renewal_stop.wait(), 5)
            except TimeoutError:
                pass
    renewal = asyncio.create_task(renew_preparation()) if probe == 'real' else None
    try:
        binding=await workspaces.bind_task(WorkspaceIdentity('u','a','c','s','node'),owner,
            fresh_claim=True,all_resource_paths_tracked=True,read_model_input_types=['text/plain'])
        calls=[]
        async def exec_shell(command,**kwargs):
            calls.append(command)
            namespace=os.readlink('/proc/self/ns/pid') if probe=='same_namespace' else 'pid:[1]'
            return SimpleNamespace(exit_code=1 if probe=='failed' else 0,stdout=namespace.encode())
        if probe!='real':
            monkeypatch.setattr(binding.backend,'exec_shell',exec_shell)
        if probe in {'isolated','real'}:
            await attest_task_sandbox(binding,owner,sessions)
            assert await admin.fetchval('SELECT workspace_id FROM runtime_sandbox_attestations')==binding.workspace.workspace_id
        else:
            with pytest.raises(ExecutionAborted):
                await attest_task_sandbox(binding,owner,sessions)
            assert await admin.fetchval('SELECT count(*) FROM runtime_sandbox_attestations')==0
        assert calls==([] if probe=='real' else [['/usr/bin/readlink','/proc/self/ns/pid']])
    finally:
        renewal_stop.set()
        try:
            if renewal is not None:
                await renewal
        finally:
            assert await workspaces.resource_registry.verify_preparation_cleanup(owner)
            await workspaces.close()


async def test_initial_schema_permission_proofs_are_immutable(projection_database):
    import asyncpg
    sessions,admin=projection_database
    command=await prepared(sessions,admin)
    await accept(sessions,command)
    for table in ('runtime_sandbox_attestations','runtime_permission_upgrades'):
        with pytest.raises(asyncpg.RaiseError,match='immutable'):
            await admin.execute(f"UPDATE {table} SET worker_id='forged'")


@pytest.mark.parametrize('provider',['docker','kubernetes','e2b','daytona','opensandbox'])
@pytest.mark.parametrize('verified',[True,False])
async def test_provider_proof_controls_upgrade_and_audit(projection_database,provider,verified):
    from workmate_product.application.sandbox_proof import sandbox_proof_sql
    sessions,admin=projection_database
    command=await parked(sessions,admin)
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=clock_timestamp()+interval '30 seconds'")
    evidence={'uid':60000,'no_new_privs':True} if verified else {'uid':0,'no_new_privs':True}
    await admin.execute("""INSERT INTO runtime_sandbox_attestations
        (task_id,session_id,worker_id,execution_epoch,workspace_id,isolation,containment,instance_id,evidence)
        VALUES ('t','s','worker',1,'workspace',$1,'dedicated_uid','actual-instance',$2::json)""",provider,json.dumps(evidence))
    await admin.execute("""INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest)
        VALUES ('t','s',1,'{"approval_profile":"standard"}',repeat('d',64))""")
    command=command.model_copy(update={'payload':{**command.payload,'full_access':True}})
    assert bool(await admin.fetchval(f'SELECT {sandbox_proof_sql()} FROM runtime_sandbox_attestations p')) is verified
    if not verified:
        with pytest.raises(CommandRejected):
            await accept(sessions,command)
    else:
        await accept(sessions,command)
        metadata=json.loads(await admin.fetchval("SELECT metadata FROM security_audit_events WHERE event_type='task.full_access_enabled'"))
        assert metadata['workspace_isolation']==provider
