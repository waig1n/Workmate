"""Worker retirement fairness, missing-owner safety and shutdown isolation."""
import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest
from workmate_product.persistence.models import Conversation, RuntimeSession, ConversationRetirement
from test_durable_projection import projection_database


async def test_missing_owner_requests_do_not_starve_local_retirement(projection_database):
    from workmate_runtime.execution.retirement import RetirementWorker
    sessions, admin = projection_database
    async with sessions.begin() as db:
        for index in range(101):
            cid, sid = f'retire-{index:03}', f'session-{index:03}'
            db.add(Conversation(id=cid, agent_id='a', user_id='u', deleted_at=datetime.now(timezone.utc)))
            await db.flush()
            db.add(RuntimeSession(id=sid,conversation_id=cid,storage_node_id='node'))
            await db.flush()
            db.add(ConversationRetirement(conversation_id=cid,session_id=sid,storage_node_id='node',requested_by='u'))
    calls = []
    async def retire(sid):
        calls.append(sid)
        return sid == 'session-100'
    service = SimpleNamespace(node_id='node',worker_id='worker',active_sessions=frozenset())
    worker = RetirementWorker(sessions, service, SimpleNamespace(retire_session=retire))
    await worker.tick()
    assert len(calls) == 100
    await worker.tick()
    assert await admin.fetchval("SELECT completed_by FROM conversation_retirements WHERE conversation_id='retire-100'") == 'worker'
    assert await admin.fetchval('SELECT count(*) FROM conversation_retirements WHERE completed_at IS NULL') == 100
    assert len(worker.failures) == 100


async def test_scan_error_is_retried_without_killing_retirement_loop():
    from workmate_runtime.execution.retirement import RetirementWorker
    worker = RetirementWorker(None,None,None)
    shutdown = asyncio.Event()
    calls = []
    async def tick():
        calls.append(True)
        if len(calls) == 1:
            raise OSError('private connection information')
        shutdown.set()
    worker.tick = tick
    async with asyncio.timeout(2):
        await worker.serve(shutdown)
    assert len(calls) == 2 and worker.failures == {}


async def test_retirement_failure_cannot_skip_application_workspace_cleanup():
    from workmate_runtime.execution.composition import WorkerApplication
    closed = []
    async def serve(shutdown): pass
    async def recover(): return {}
    async def broken(shutdown): raise RuntimeError('retirement failed')
    async def close(): closed.append(True)
    service = SimpleNamespace(serve=serve,failures={})
    async def sweep(**kwargs): pass
    from workmate_runtime.config import RuntimeSettings
    workspaces = SimpleNamespace(resource_registry=SimpleNamespace(_scopes={}),close=close,sweep_idle=sweep)
    application = WorkerApplication(service,SimpleNamespace(settings=RuntimeSettings()),None,workspaces,None,SimpleNamespace(recover_pending=recover),
                                    SimpleNamespace(serve=broken))
    with pytest.raises(RuntimeError,match='retirement failed'):
        await application.serve(asyncio.Event())
    assert closed == [True]


async def test_active_and_mismatched_retirement_never_acknowledge(projection_database):
    from workmate_runtime.execution.retirement import RetirementWorker
    sessions, admin = projection_database
    await admin.execute("UPDATE tasks SET status='completed'; UPDATE conversations SET active_product_task_id=NULL, deleted_at=clock_timestamp(); UPDATE runtime_sessions SET active_task_id=NULL")
    await admin.execute("INSERT INTO conversation_retirements(conversation_id,session_id,storage_node_id,execution_epoch,requested_by) VALUES ('c','s','node',0,'u')")
    calls = []
    async def retire(sid): calls.append(sid); return True
    service = SimpleNamespace(node_id='node',worker_id='worker',active_sessions={'s'})
    worker = RetirementWorker(sessions,service,SimpleNamespace(retire_session=retire))
    await worker.tick()
    assert calls == []
    service.active_sessions = set()
    await worker.tick()  # wrap cursor
    await worker.tick()
    assert worker.failures['s'] == 'RuntimeError'  # epoch does not match request
    assert calls == []  # Reject before any Workspace cleanup side effect
    assert await admin.fetchval('SELECT completed_at FROM conversation_retirements') is None


@pytest.mark.parametrize('blocker', ['gates', 'task', 'command'])
async def test_deferred_retirement_never_touches_workspace_before_execution_closes(projection_database, blocker):
    from workmate_runtime.execution.retirement import RetirementWorker
    sessions, admin = projection_database
    await admin.execute("UPDATE tasks SET status='failed'; UPDATE conversations SET deleted_at=clock_timestamp()")
    if blocker != 'gates':
        await admin.execute("UPDATE conversations SET active_product_task_id=NULL; UPDATE runtime_sessions SET active_task_id=NULL")
    if blocker == 'task':
        await admin.execute("UPDATE tasks SET status='running'")
    if blocker == 'command':
        await admin.execute("""INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
            VALUES ('pending','s','c','t','u','StopTask','{}',repeat('a',64))""")
    await admin.execute("INSERT INTO conversation_retirements(conversation_id,session_id,storage_node_id,execution_epoch,requested_by) VALUES ('c','s','node',1,'u')")
    calls = []
    async def retire(sid): calls.append(sid); return True
    worker = RetirementWorker(sessions, SimpleNamespace(node_id='node', worker_id='worker', active_sessions=set()), SimpleNamespace(retire_session=retire))
    await worker.tick()
    assert calls == []
    assert await admin.fetchval('SELECT completed_at FROM conversation_retirements') is None


async def test_deferred_failed_retirement_completes_only_after_gates_close(projection_database):
    from workmate_runtime.execution.retirement import RetirementWorker
    sessions, admin = projection_database
    await admin.execute("UPDATE tasks SET status='failed'; UPDATE conversations SET deleted_at=clock_timestamp()")
    await admin.execute("INSERT INTO conversation_retirements(conversation_id,session_id,storage_node_id,execution_epoch,requested_by) VALUES ('c','s','node',1,'u')")
    calls = []
    async def retire(sid): calls.append(sid); return True
    worker = RetirementWorker(sessions, SimpleNamespace(node_id='node', worker_id='worker', active_sessions=set()), SimpleNamespace(retire_session=retire))
    await worker.tick()
    assert calls == []
    # Fixture models a separately verified closed boundary, not deletion doing cleanup.
    await admin.execute("UPDATE conversations SET active_product_task_id=NULL; UPDATE runtime_sessions SET active_task_id=NULL,status='failed'")
    await worker.tick()  # wrap cursor
    await worker.tick()
    assert calls == ['s']
    assert await admin.fetchval('SELECT completed_at FROM conversation_retirements') is not None
    assert await admin.fetchval('SELECT status FROM tasks') == 'failed'


async def test_historical_workspace_blocks_global_retirement(projection_database):
    from workmate_runtime.execution.retirement import RetirementWorker
    sessions, admin = projection_database
    await admin.execute("""UPDATE tasks SET status='failed';
        UPDATE conversations SET active_product_task_id=NULL,deleted_at=clock_timestamp();
        UPDATE runtime_sessions SET active_task_id=NULL,superseded_at=clock_timestamp();
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('new','c','node');
        INSERT INTO conversation_retirements(conversation_id,session_id,storage_node_id,execution_epoch,requested_by)
        VALUES ('c','new','node',0,'u');""")
    calls = []
    async def retire(sid): calls.append(sid); return True
    worker = RetirementWorker(sessions, SimpleNamespace(node_id='node',worker_id='worker',active_sessions=set()),
                              SimpleNamespace(retire_session=retire))
    await worker.tick()
    assert calls == []
    assert worker.failures['new'] == 'HistoricalWorkspacesRequireRetirement'
    assert await admin.fetchval('SELECT completed_at FROM conversation_retirements') is None
