"""A joined sandbox rejection becomes durable without a Reply or new snapshot."""
import pytest
from test_native_admission import admission_database,create
from test_durable_projection import projection_database
from app.services.execution_admission import TaskAdmission


async def claim(sessions):
    from workmate_runtime.transport.product_pool import ProductWorkerPool
    from workmate_runtime.transport.postgres import ExecutionStore
    from sqlalchemy import text
    service=TaskAdmission(sessions,storage_node_id='node')
    await create(sessions,service,task_id='full',approval_profile='full_access')
    async with sessions() as db:
        command=await db.scalar(text("SELECT id FROM runtime_commands WHERE task_id='full'"))
    pool=ProductWorkerPool(sessions.kw['bind'])
    async with pool.acquire() as db:
        owner=await ExecutionStore(db).claim_start(command,'worker','node',lease_seconds=30)
    return pool,command,owner


@pytest.mark.parametrize('proof_kind', ['missing','worker','epoch','session'])
async def test_initial_full_access_cannot_begin_without_owner_proof(admission_database,proof_kind):
    from workmate_runtime.transport.postgres import ExecutionStore,LostOwnership
    sessions,admin=admission_database
    pool,command,owner=await claim(sessions)
    if proof_kind!='missing':
        await admin.execute('''INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id)
            VALUES ('full',$1,$2,$3,'workspace')''', 's' if proof_kind=='session' else owner.session_id,
            'other' if proof_kind=='worker' else owner.worker_id,
            owner.execution_epoch+int(proof_kind=='epoch'))
    async with pool.acquire() as db:
        with pytest.raises(LostOwnership,match='sandbox'):
            await ExecutionStore(db).begin_reply(owner,'reply',{'type':'REPLY_START','reply_id':'reply','session_id':owner.session_id})
    assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE task_id='full'")==0


@pytest.mark.parametrize('reason',['sandbox_verification_failed','model_unavailable'])
@pytest.mark.parametrize('mutation',['none','epoch','lease','stop','unjoined','dirty'])
async def test_preparation_rejection_requires_live_exact_owner_and_cleanup(admission_database,mutation,reason):
    from workmate_runtime.transport.preparation_rejection import PreparationRejectionStore
    from workmate_runtime.transport.postgres import LostOwnership
    sessions,admin=admission_database
    pool,command,owner=await claim(sessions)
    if mutation=='epoch': await admin.execute("UPDATE runtime_sessions SET execution_epoch=execution_epoch+1 WHERE id=$1",owner.session_id)
    if mutation=='lease': await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",owner.session_id)
    if mutation=='stop': await admin.execute("UPDATE tasks SET status='cancelling' WHERE id='full'")
    async with pool.acquire() as db:
        async def finish():
            return await PreparationRejectionStore(db).finish(owner,command,'node',
                preparation_joined=mutation!='unjoined',resources_quiescent=mutation!='dirty',reason=reason)
        if mutation=='none':
            await finish()
        else:
            with pytest.raises((ValueError,LostOwnership)): await finish()
            assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE task_id='full'")==0
    if mutation=='none':
        assert await admin.fetchval("SELECT status FROM runtime_task_executions WHERE task_id='full'")=='failed'
        assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE task_id='full'")==1
        assert await admin.fetchval("SELECT status FROM tasks WHERE id='full'")=='created'
        from workmate_runtime.execution.reconciliation import inspect_sessions
        observations = await inspect_sessions(admin, worker_id='worker', storage_node_id='node')
        assert next(item for item in observations if item.task_id=='full').category == 'terminal_delivery_pending'


async def test_stop_cannot_overwrite_committed_preparation_rejection(admission_database):
    from workmate_runtime.transport.preparation_rejection import PreparationRejectionStore
    from workmate_product.application.control_commands import accept_stop
    from workmate_product.application.commands import CommandRejected
    from workmate_contracts import Command
    sessions,admin=admission_database
    pool,command,owner=await claim(sessions)
    async with pool.acquire() as db:
        await PreparationRejectionStore(db).finish(owner,command,'node',preparation_joined=True,resources_quiescent=True)
    async with sessions.begin() as db:
        with pytest.raises(CommandRejected,match='failed before execution'):
            await accept_stop(db,Command(command_id='late-stop',kind='StopTask',conversation_id='new',
                product_task_id='full',runtime_session_id=owner.session_id,expected_epoch=owner.execution_epoch,
                payload={'reason':'user_requested'}),actor_user_id='u')
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE id='late-stop'")==0
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='full'")=='created'


@pytest.mark.parametrize('failure', ['sandbox','model'])
@pytest.mark.parametrize('cleanup', ['proven','dirty','late_stop'])
async def test_controller_rejects_only_joined_clean_preparation(admission_database,cleanup,failure):
    from workmate_runtime.execution.stopping_preparation import PreparingExecutionController
    from workmate_runtime.execution.controller import SandboxVerificationFailed,ExecutionAborted
    from workmate_product.application.control_commands import accept_stop
    from workmate_contracts import Command
    sessions,admin=admission_database
    pool,command,owner=await claim(sessions)
    finished=False
    async def prepare(*args):
        nonlocal finished
        from workmate_runtime.assembly.models import ModelUnavailable
        try: raise (ModelUnavailable if failure == 'model' else SandboxVerificationFailed)('fixture rejection')
        finally: finished=True
    async def verify(current):
        assert finished and current==owner
        if cleanup=='late_stop':
            async with sessions.begin() as db:
                await accept_stop(db,Command(command_id='race-stop',kind='StopTask',conversation_id='new',
                    product_task_id='full',runtime_session_id=owner.session_id,expected_epoch=owner.execution_epoch,
                    payload={'reason':'user_requested'}),actor_user_id='u')
        return cleanup!='dirty'
    controller=PreparingExecutionController(pool,verify_preparation_cleanup=verify,verify_cleanup=verify)
    guard=controller._new_guard(owner,command,'node')
    if cleanup=='dirty':
        with pytest.raises(ExecutionAborted,match='not proven quiescent'):
            await controller._execute(guard,prepare)
        assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE task_id='full'")==0
    else:
        result=await controller._execute(guard,prepare)
        assert result.status==('cancelled' if cleanup=='late_stop' else 'failed')
        assert result.reply_id is None
        if cleanup == 'proven':
            payload = await admin.fetchval("SELECT payload->>'reason' FROM runtime_events WHERE task_id='full'")
            assert payload == ('model_unavailable' if failure == 'model' else 'sandbox_verification_failed')
        assert await admin.fetchval("SELECT count(*) FROM runtime_events WHERE task_id='full'")==1


async def test_rejection_rejects_unknown_reason_before_writing():
    from workmate_runtime.transport.preparation_rejection import PreparationRejectionStore
    with pytest.raises(ValueError, match='reason'):
        await PreparationRejectionStore(None).finish(None,None,None,
            preparation_joined=True,resources_quiescent=True,reason='arbitrary')
