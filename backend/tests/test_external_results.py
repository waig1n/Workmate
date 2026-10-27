"""Only authenticated producer receipts can drive external-result continuation."""
import asyncio
import json

import pytest
from workmate_contracts.external import ExternalResultReceipt
from workmate_product.application.external_results import record_external_receipt,accept_external_resume
from workmate_product.application.commands import CommandConflict,CommandRejected
from workmate_contracts import Command,snapshot_digest
from test_durable_projection import projection_database  # noqa: F401
from test_realtime_redis_log import log  # noqa: F401
from test_durable_interactions import CALL,request,park,consume


async def verified(*args):
    return True


async def ready(sessions,admin):
    call={**CALL,'state':'submitted'}
    await request(sessions,admin,[call],kind='REQUIRE_EXTERNAL_EXECUTION')
    seq=await park(admin,[call])
    await consume(sessions)
    for table in ('runtime_sessions','runtime_task_executions'):
        await admin.execute(f"UPDATE {table} SET snapshot_id=$1,state_version=1,status='awaiting_interaction'",str(seq))
    await admin.execute("UPDATE runtime_sessions SET owner_id='worker',lease_until=clock_timestamp()+interval '30 seconds'")
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    pending=state['pending_interactions'][0]
    receipt=ExternalResultReceipt(receipt_id='receipt',producer_id='producer',job_id='job',
        conversation_id='c',session_id='s',task_id='t',reply_id='reply',execution_epoch=1,
        tool_call_id=call['id'],tool_name=call['name'],input_digest=snapshot_digest({'input':call['input'],'name':call['name']}),
        result={'type':'tool_result','id':call['id'],'name':call['name'],'output':'actual external result',
            'state':'success','metadata':{},'created_at':'2026-09-16T00:00:00+00:00','finished_at':'2026-09-16T00:00:01+00:00'})
    command=Command(command_id='resume-external',kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
        product_task_id='t',reply_id='reply',expected_epoch=1,interaction_id=pending['interaction_id'],
        request_version=pending['request_version'],payload={'resume_kind':'external_result','receipt_id':'receipt',
            'snapshot_id':str(seq),'state_version':1})
    return receipt,command


async def test_receipt_and_resume_are_once_without_fake_user_confirmation(projection_database):
    sessions,admin=projection_database
    receipt,command=await ready(sessions,admin)
    async def submit():
        async with sessions.begin() as db:
            await record_external_receipt(db,receipt,verify_producer=verified)
            return await accept_external_resume(db,command,actor_user_id='u',authorize_resume=verified)
    results=await asyncio.gather(submit(),submit())
    assert sorted(r.duplicate for r in results)==[False,True]
    assert await admin.fetchval('SELECT count(*) FROM runtime_external_receipts')==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'")==0
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='external_result_received'")==1
    assert await admin.fetchval('SELECT status FROM runtime_sessions')=='awaiting_interaction'


async def test_untrusted_producer_and_conflicting_result_cannot_publish(projection_database):
    sessions,admin=projection_database
    receipt,command=await ready(sessions,admin)
    async def denied(*args):
        return False
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await record_external_receipt(db,receipt,verify_producer=denied)
    async with sessions.begin() as db:
        await record_external_receipt(db,receipt,verify_producer=verified)
    async with sessions.begin() as db:
        with pytest.raises(CommandConflict):
            await record_external_receipt(db,receipt.model_copy(update={'result':{**receipt.result,'output':'changed'}}),verify_producer=verified)
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await accept_external_resume(db,command,actor_user_id='u',authorize_resume=denied)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


@pytest.mark.parametrize('resource_done,call_ids',[(True,['a']),(False,['a']),(True,['a','b'])])
async def test_real_core_external_result_keeps_reply_and_requires_actual_job_completion(projection_database,resource_done,call_ids,log,monkeypatch):
    pytest.importorskip('agentscope')
    import os
    import asyncpg
    from agentscope.message import ToolCallBlock,TextBlock
    from workmate_runtime.execution.controller import ExecutionAborted
    from workmate_runtime.execution.running import RunningExecutionController
    from workmate_runtime.execution.external_results import ExternalExecutionController,ExternalResultResources
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.permission_policy import ProductPermissionMiddleware,ExecutionPermissionPolicy
    from workmate_product.application.control_commands import accept_resume
    from test_core_engine import ConfirmTool,make_engine
    from test_execution_controller import prepare_reply
    sessions,admin=projection_database
    await admin.execute("""UPDATE tasks SET status='created';
        UPDATE runtime_sessions SET active_task_id=NULL,execution_epoch=0,status='accepted',reply_id=NULL;
        UPDATE runtime_task_executions SET execution_epoch=0,status='accepted',reply_id=NULL;
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('a',64));""")
    pool=await asyncpg.create_pool(os.environ['WORKMATE_TEST_POSTGRES_DSN'],min_size=1,max_size=4,
        server_settings={'search_path':await admin.fetchval('SHOW search_path')})
    from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.log_manager import EventLogManager
    writer_factory=RealtimeWriterFactory(sessions,log_manager=EventLogManager(sessions,log),
        log=log,policy=RealtimePolicy())
    log.max_bytes=1048576
    def display(controller):
        controller.realtime_writer_factory=writer_factory
        return controller
    # Final-only model fixtures do not emit Core body events. Exercise the
    # current display protocol with an actual streamed chunk before the final.
    import test_core_engine
    from agentscope.model import ChatResponse
    original_model=test_core_engine.ScriptModel
    class StreamingModel(original_model):
        async def _call_api(self,*args,**kwargs):
            source=await super()._call_api(*args,**kwargs)
            async def chunks():
                async for chunk in source:
                    yield ChatResponse(content=chunk.content,is_last=False)
                    yield chunk
            return chunks()
    monkeypatch.setattr(test_core_engine,'ScriptModel',StreamingModel)
    registry=TaskResourceRegistry()
    process=None
    try:
        tool=ConfirmTool()
        tool.is_external_tool=True
        tool.is_concurrency_safe=True
        prepare,_=prepare_reply([[ToolCallBlock(id=cid,name=tool.name,input='{}') for cid in call_ids]],tool=tool)
        async def prepare_registered(owner,snapshot,guard):
            registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
            return await prepare(owner,snapshot,guard)
        first=await display(RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup)).start(
            'start','worker','node',prepare_registered)
        while await consume(sessions) is not None:
            pass
        async def make_command(command_id,payload):
            state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
            wanted='external_execution' if payload.get('resume_kind')=='external_result' else 'permission'
            pending=next(p for p in state['pending_interactions'] if p['kind']==wanted)
            execution=await admin.fetchrow('SELECT * FROM runtime_task_executions')
            return Command(command_id=command_id,kind='ResumeInteraction',conversation_id='c',runtime_session_id='s',
                product_task_id='t',reply_id=first.reply_id,expected_epoch=1,interaction_id=pending['interaction_id'],
                request_version=pending['request_version'],payload={**payload,'snapshot_id':execution['snapshot_id'],
                    'state_version':execution['state_version']})
        async def restore(owner,snapshot,guard):
            return make_engine([[TextBlock(text='external completed')]],state=snapshot.to_state(),tool=tool,
                middlewares=[ProductPermissionMiddleware(ExecutionPermissionPolicy(task_id='t',approval_profile='standard')),guard])
        for cid in call_ids:
            approval=await make_command('approve-'+cid,{'decision_version':'approved-'+cid,
                'decisions':[{'tool_call_id':cid,'approved':True}]})
            async with sessions.begin() as db:
                await accept_resume(db,approval,actor_user_id='u',authorize_resume=verified)
            submitted=await display(RunningExecutionController(pool,verify_cleanup=registry.verify_cleanup)).resume('approve-'+cid,'worker','node',restore,authorize_resume=verified)
            while await consume(sessions) is not None:
                pass
        assert submitted.reply_id==first.reply_id and submitted.pending_tool_call_ids==tuple(call_ids) and tool.calls==0
        assert await admin.fetchval('SELECT status FROM tasks')=='running'
        receipt=ExternalResultReceipt(receipt_id='result',producer_id='provider',job_id='job',conversation_id='c',
            session_id='s',task_id='t',reply_id=first.reply_id,execution_epoch=1,tool_call_id='a',tool_name=tool.name,
            input_digest=snapshot_digest({'name':tool.name,'input':'{}'}),result={'type':'tool_result','id':'a',
                'name':tool.name,'output':'provider finished','state':'success','metadata':{},
                'created_at':'2026-09-16T00:00:00+00:00','finished_at':'2026-09-16T00:00:01+00:00'})
        command=await make_command('external',{'resume_kind':'external_result','receipt_id':'result'})
        async with sessions.begin() as db:
            await record_external_receipt(db,receipt,verify_producer=verified)
            await accept_external_resume(db,command,actor_user_id='u',authorize_resume=verified)
        resources=ExternalResultResources(registry)
        process=await asyncio.create_subprocess_exec('/bin/sleep','0' if resource_done else '60')
        if resource_done:
            await process.wait()
        async def probe():
            return process.returncode is not None
        async def cleanup():
            if process.returncode is None:
                process.terminate()
            await process.wait()
        resources.register(first.owner,'job','provider','a',cleanup=cleanup,probe=probe)
        controller=ExternalExecutionController(pool,verify_external_result=resources.verify_result,
            verify_cleanup=registry.verify_cleanup,verify_preparation_cleanup=registry.verify_preparation_cleanup)
        display(controller)
        if resource_done:
            result=await controller.resume('external','worker','node',restore,authorize_resume=verified)
            if len(call_ids)>1:
                assert result.status=='awaiting_interaction' and result.pending_tool_call_ids==('b',)
                while await consume(sessions) is not None:
                    pass
                second=receipt.model_copy(update={'receipt_id':'result2','job_id':'job2','tool_call_id':'b',
                    'result':{**receipt.result,'id':'b'}})
                command2=await make_command('external2',{'resume_kind':'external_result','receipt_id':'result2'})
                async with sessions.begin() as db:
                    await record_external_receipt(db,second,verify_producer=verified)
                    await accept_external_resume(db,command2,actor_user_id='u',authorize_resume=verified)
                resources.register(first.owner,'job2','provider','b',cleanup=cleanup,probe=probe)
                result=await controller.resume('external2','worker','node',restore,authorize_resume=verified)
            assert result.status=='completed' and result.reply_id==first.reply_id and tool.calls==0
            while await consume(sessions) is not None:
                pass
            assert await admin.fetchval("SELECT snapshot_phase FROM task_messages WHERE snapshot_version>0")=='final'
            assert 'external completed' in await admin.fetchval("SELECT content::text FROM task_messages WHERE snapshot_version>0")
            assert not await admin.fetchval("SELECT count(*) FROM runtime_events WHERE payload->>'type' LIKE 'TEXT_BLOCK_%'")
            assert not await admin.fetchval("SELECT count(*) FROM product_events WHERE payload->>'type' LIKE 'TEXT_MESSAGE_%'")
            assert await controller.resume('external','worker','node',restore,authorize_resume=verified) is None
            assert 'provider finished' in await admin.fetchval('SELECT payload::text FROM runtime_snapshots ORDER BY state_version DESC LIMIT 1')
        else:
            with pytest.raises(ExecutionAborted):
                await controller.resume('external','worker','node',restore,authorize_resume=verified)
            assert await admin.fetchval('SELECT status FROM runtime_sessions')=='awaiting_interaction'
        assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
        assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='hitl_decision'")==len(call_ids)
    finally:
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        await pool.close()


@pytest.mark.parametrize('mutation',[
    "UPDATE tasks SET status='cancelling'",
    "UPDATE runtime_sessions SET state_version=99",
    "UPDATE runtime_projection_cursors SET last_runtime_seq=0",
    "UPDATE runtime_task_executions SET execution_epoch=2",
    "UPDATE agents SET deleted_at=clock_timestamp()",
    "UPDATE task_projection_states SET state=jsonb_set(state,'{interaction_boundary_id}','\"wrong\"')",
])
async def test_external_resume_rejects_stale_boundary_or_authority(projection_database,mutation):
    sessions,admin=projection_database
    receipt,command=await ready(sessions,admin)
    async with sessions.begin() as db:
        await record_external_receipt(db,receipt,verify_producer=verified)
    await admin.execute(mutation)
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await accept_external_resume(db,command,actor_user_id='u',authorize_resume=verified)
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


async def test_receipts_are_immutable_and_worker_rechecks_owner(projection_database):
    from workmate_runtime.transport.external_results import ExternalResumeStore
    sessions,admin=projection_database
    receipt,command=await ready(sessions,admin)
    async with sessions.begin() as db:
        await record_external_receipt(db,receipt,verify_producer=verified)
        await accept_external_resume(db,command,actor_user_id='u',authorize_resume=verified)
    import asyncpg
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await admin.execute("UPDATE runtime_external_receipts SET digest=repeat('a',64)")
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await admin.execute('DELETE FROM runtime_external_receipts')
    store=ExternalResumeStore(admin)
    assert await store.claim_resume(command.command_id,'wrong','node',authorize_resume=verified) is None
    async def denied(*args):
        return False
    assert await store.claim_resume(command.command_id,'worker','node',authorize_resume=denied) is None
    assert await store.receipt('missing') is None


async def test_initial_schema_external_receipt_is_immutable(projection_database):
    sessions,admin=projection_database
    receipt,command=await ready(sessions,admin)
    async with sessions.begin() as db:
        await record_external_receipt(db,receipt,verify_producer=verified)
        await accept_external_resume(db,command,actor_user_id='u',authorize_resume=verified)
    import asyncpg
    with pytest.raises(asyncpg.RaiseError,match='immutable'):
        await admin.execute("UPDATE runtime_external_receipts SET job_id='forged'")


async def test_unknown_or_restarted_job_ledger_never_proves_completion():
    pytest.importorskip('agentscope')
    from workmate_runtime.execution.external_results import ExternalResultResources
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.transport.postgres import ExecutionOwner
    from types import SimpleNamespace
    owner=ExecutionOwner('s','t','worker',1)
    registry=TaskResourceRegistry()
    registry.open_scope(owner,fresh_claim=True,all_resource_paths_tracked=True)
    resources=ExternalResultResources(registry)
    receipt=SimpleNamespace(producer_id='producer',job_id='job',tool_call_id='a')
    assert await resources.verify_result(owner,receipt) is False
    resources.register(owner,'job','producer','a',cleanup=verified,probe=verified)
    with pytest.raises(ValueError,match='already registered'):
        resources.register(owner,'job','producer','a',cleanup=verified,probe=verified)
    assert await resources.verify_result(owner,receipt) is True
    assert await ExternalResultResources(registry).verify_result(owner,receipt) is False


async def test_receipt_identity_and_terminal_shape_are_enforced(projection_database):
    from pydantic import ValidationError
    sessions,admin=projection_database
    receipt,command=await ready(sessions,admin)
    for update in ({'tool_name':'other'},{'input_digest':'0'*64},{'execution_epoch':2}):
        forged=receipt.model_copy(update=update)
        if 'tool_name' in update:
            forged=forged.model_copy(update={'result':{**receipt.result,'name':'other'}})
        async with sessions.begin() as db:
            with pytest.raises(CommandRejected):
                await record_external_receipt(db,forged,verify_producer=verified)
    async with sessions.begin() as db:
        with pytest.raises(ValidationError):
            await record_external_receipt(db,receipt.model_copy(update={'result':{**receipt.result,'state':'running'}}),verify_producer=verified)
        with pytest.raises(CommandRejected):
            await accept_external_resume(db,command,actor_user_id='u',authorize_resume=verified)
    async with sessions() as db:
        with pytest.raises(CommandRejected,match='transaction'):
            await record_external_receipt(db,receipt,verify_producer=verified)
