"""Product background projection is durable without an attached browser."""
import asyncio
import json
import pytest
from sqlalchemy import text
from test_durable_projection import projection_database,emit
from test_durable_delivery import seed
from workmate_product.projections.service import ProductProjectionService
from app.services.durable_runtime import compose_product_runtime


async def test_competing_services_consume_each_source_once_and_deliver(projection_database):
    sessions,admin=projection_database
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus('unused')
    sessions.configure(info={'wakeup_bus': bus})
    for seq,raw in enumerate([{'type':'REPLY_START','session_id':'s'},
            {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'read'},
            {'type':'TOOL_RESULT_END','tool_call_id':'call','state':'success'}],1):
        await emit(admin,seq,raw)
    first,second=compose_product_runtime(sessions),compose_product_runtime(sessions)
    for _ in range(4):
        await asyncio.gather(first.tick('projection'),second.tick('projection'))
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==3
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['tool_results']=={'call':'success'}
    assert not {'text','text_open','process_end'} & state.keys()
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_started'")==1

    assert 'conversation:c' in bus._pending


async def test_delivery_scans_captured_boundary_and_releases_once(projection_database):
    sessions,admin=projection_database
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus('unused')
    sessions.configure(info={'wakeup_bus': bus})
    await seed(sessions,admin)
    first,second=compose_product_runtime(sessions),compose_product_runtime(sessions)
    await asyncio.gather(first.tick('delivery'),second.tick('delivery'))
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==1
    assert await first.tick('delivery')==0

    assert 'admission' in bus._pending


async def test_projection_failure_rolls_back_watermark_and_restart_retries(projection_database):
    sessions,admin=projection_database
    from workmate_product.coordination import WakeupBus
    bus = WakeupBus('unused')
    sessions.configure(info={'wakeup_bus': bus})
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    async def broken(db,event,state,boundary):
        await db.execute(text("UPDATE tasks SET title='bad' WHERE id='t'"))
        raise RuntimeError('projector failed')
    failed=ProductProjectionService(sessions,project=broken)
    assert await failed.tick('projection')==0
    assert failed.failures[('projection','c')]=='RuntimeError'
    assert not bus._pending
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='Fixture'
    restarted=compose_product_runtime(sessions)
    assert await restarted.tick('projection')==1
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1


async def test_shutdown_cancels_and_joins_inflight_projection_before_return(projection_database):
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    entered,joined,shutdown=asyncio.Event(),asyncio.Event(),asyncio.Event()
    async def blocked(db,event,state,boundary):
        await db.execute(text("UPDATE tasks SET title='uncommitted' WHERE id='t'"))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()
    service=ProductProjectionService(sessions,project=blocked,poll_interval=.01)
    task=asyncio.create_task(service.serve(shutdown))
    await asyncio.wait_for(entered.wait(),3)
    shutdown.set()
    await asyncio.wait_for(task,3)
    assert joined.is_set() and not service.active_loops
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='t'")=='Fixture'


async def test_restarted_admission_scanner_persists_one_start_after_delivery(projection_database):
    from workmate_contracts import Command
    from workmate_product.application.commands import accept_start
    from workmate_product.persistence.models import Task
    sessions,admin=projection_database
    await seed(sessions,admin)
    async with sessions.begin() as db:
        db.add(Task(id='next',conversation_id='c',title='Queued'))
    async def allow(*args):
        return True
    async def admit(db,cid):
        session=(await db.execute(text('SELECT * FROM runtime_sessions WHERE conversation_id=:cid'),{'cid':cid})).mappings().one()
        command=Command(command_id='start-next',kind='StartTask',conversation_id=cid,runtime_session_id=session['id'],
                        product_task_id='next',expected_epoch=session['execution_epoch'])
        accepted=await accept_start(db,command,actor_user_id='u',storage_node_id='node',authorize_agent=allow)
        # This fixture stands in for the role's record_execution_spec callback.
        await db.execute(text("""INSERT INTO runtime_execution_specs(task_id,session_id,schema_version,payload,digest)
            VALUES ('next','s',1,'{}',repeat('a',64)) ON CONFLICT DO NOTHING"""))
        return accepted
    before=compose_product_runtime(sessions,admit_next=admit)
    assert await before.tick('admission')==0
    await before.tick('delivery')
    # No in-memory completion callback survives into these replacement instances.
    first,second=compose_product_runtime(sessions,admit_next=admit),compose_product_runtime(sessions,admit_next=admit)
    await asyncio.gather(first.tick('admission'),second.tick('admission'))
    assert await admin.fetchval("SELECT count(*) FROM runtime_commands WHERE kind='StartTask'")==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='next'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions') is None
    assert not first.failures and not second.failures


async def test_failed_admission_leaves_durable_queue_retryable(projection_database):
    from workmate_product.persistence.models import Task
    sessions,admin=projection_database
    await seed(sessions,admin)
    async with sessions.begin() as db:
        db.add(Task(id='next',conversation_id='c',title='Queued'))
    service=compose_product_runtime(sessions)
    await service.tick('delivery')
    async def failed(db,cid):
        await db.execute(text("UPDATE tasks SET title='bad' WHERE id='next'"))
        raise ValueError('revoked or unavailable')
    service.admit_next=failed
    assert await service.tick('admission')==0
    assert service.failures[('admission','c')]=='ValueError'
    assert await admin.fetchval("SELECT title FROM tasks WHERE id='next'")=='Queued'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
    assert await admin.fetchval('SELECT count(*) FROM runtime_commands')==0


async def test_missing_source_is_not_acknowledged(projection_database):
    sessions,admin=projection_database
    await emit(admin,2,{'type':'REPLY_START','session_id':'s'})
    service=compose_product_runtime(sessions)
    assert await service.tick('projection')==0
    assert service.failures[('projection','c')]=='ProjectionConflict'
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0


async def test_no_start_delivery_needs_no_capture_file(projection_database):
    from test_unstarted_projection import seed as unstarted
    sessions,admin=projection_database
    await unstarted(admin)
    service=compose_product_runtime(sessions)
    assert await service.tick('projection')==1
    assert await service.tick('delivery')==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None
    assert await admin.fetchval('SELECT count(*) FROM artifacts')==0


async def test_serve_settles_terminal_and_delivery_without_browser_or_manual_tick(projection_database):
    from workmate_contracts import snapshot_digest
    from workmate_product.application.delivery_metadata import _digest
    sessions,admin=projection_database
    from test_durable_lifecycle import saved_display
    reference=await saved_display(sessions,admin,body='Answer')
    events=[{'type':'REPLY_START','session_id':'s'},
        {'type':'REPLY_END','finished_reason':'completed'},
        {'type':'EXECUTION_BOUNDARY','status':'completed','pending_tool_call_ids':[],'display_snapshot':reference,
         'boundary_evidence':{'snapshot_sha256':snapshot_digest({}),'pending_tools':[]}}]
    for seq,raw in enumerate(events,1):
        await emit(admin,seq,raw)
    await admin.execute("""INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,runtime_seq,
        execution_epoch,status,reply_id,payload) VALUES ('safe','s','t',1,3,1,'completed','reply','{}');
        UPDATE runtime_sessions SET status='completed',owner_id='worker',snapshot_id='safe',state_version=1;
        UPDATE runtime_task_executions SET status='completed',snapshot_id='safe',state_version=1;""")
    payload={'version':1,'source':'snapshot','declared_files':[],'warnings':[],'files':[]}
    await admin.execute('''INSERT INTO runtime_delivery_captures(task_id,session_id,execution_epoch,worker_id,
        storage_node_id,payload,digest,snapshot_id) VALUES ('t','s',1,'worker','node',$1::jsonb,$2,'safe')''',json.dumps(payload),_digest(payload))
    shutdown=asyncio.Event()
    service=compose_product_runtime(sessions,poll_interval=.01)
    running=asyncio.create_task(service.serve(shutdown))
    try:
        async with asyncio.timeout(5):
            while await admin.fetchval('SELECT active_product_task_id FROM conversations') is not None:
                await asyncio.sleep(.01)
        assert not service.failures
        assert await admin.fetchval('SELECT status FROM tasks')=='completed'
        assert await admin.fetchval('SELECT count(*) FROM task_messages')==1
        assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_finished'")==1
        assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='artifact_delivery'")==1
        assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==3
    finally:
        shutdown.set()
        await asyncio.wait_for(running,3)
    with pytest.raises(RuntimeError,match='once'):
        await service.serve(shutdown)


async def test_external_cancellation_joins_all_loops(projection_database):
    sessions,_=projection_database
    service=compose_product_runtime(sessions,poll_interval=.01)
    running=asyncio.create_task(service.serve(asyncio.Event()))
    await asyncio.sleep(.03)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert service.active_loops==0


async def test_invalid_admission_receipt_rolls_back_callback_changes(projection_database):
    from workmate_product.persistence.models import Task
    from workmate_product.application.commands import AcceptedCommand
    sessions,admin=projection_database
    await seed(sessions,admin)
    async with sessions.begin() as db:
        db.add(Task(id='next',conversation_id='c',title='Queued'))
    service=compose_product_runtime(sessions)
    await service.tick('delivery')
    async def incomplete(db,cid):
        await db.execute(text("UPDATE conversations SET active_product_task_id='next' WHERE id=:cid"),{'cid':cid})
        return AcceptedCommand('missing','next','accepted',False)
    service.admit_next=incomplete
    assert await service.tick('admission')==0
    assert service.failures[('admission','c')]=='ProjectionConflict'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations') is None


async def test_failed_conversation_does_not_starve_next_candidate_page(projection_database):
    from workmate_product.persistence.models import Conversation, Task
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    async with sessions.begin() as db:
        db.add(Conversation(id='z',user_id='u',agent_id='a'))
        await db.flush()
        db.add(Task(id='tz',conversation_id='z',title='Other'))
    await admin.execute("""INSERT INTO runtime_sessions(id,conversation_id,storage_node_id,active_task_id,execution_epoch,status,reply_id,runtime_seq)
        VALUES ('sz','z','node','tz',1,'running','rz',1);
        INSERT INTO runtime_task_executions(task_id,session_id,execution_epoch,status,reply_id)
        VALUES ('tz','sz',1,'running','rz');
        INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
        VALUES ('ez','sz',1,1,'tz','{"type":"REPLY_START","session_id":"sz","reply_id":"rz"}');""")
    from app.adapter.durable_interactions import interaction_projector
    actual=interaction_projector()
    async def first_broken(db,event,state,boundary):
        if event.conversation_id=='c':
            raise ValueError('bad source')
        return await actual(db,event,state,boundary)
    service=ProductProjectionService(sessions,project=first_broken,batch_size=1)
    assert await service.tick('projection')==0
    assert await service.tick('projection')==1
    assert await admin.fetchval("SELECT last_runtime_seq FROM runtime_projection_cursors WHERE conversation_id='z'")==1
    assert await admin.fetchval("SELECT count(*) FROM runtime_projection_cursors WHERE conversation_id='c'")==0


async def test_projection_batch_and_agent_authority_follow_same_lock_order(projection_database):
    """Second product_seq UPDATE rechecks Agent FK, despite unchanged agent_id."""
    from workmate_product.projections.store import consume_next,ProjectionResult,ProjectedEvent
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    entered,release=asyncio.Event(),asyncio.Event()
    async def project(db,event,state,boundary):
        entered.set()
        await release.wait()
        return ProjectionResult(state,[ProjectedEvent(key=str(i),event_type='CUSTOM',
            payload={'type':'CUSTOM','name':'TEST','value':i}) for i in range(2)])
    async def consume():
        async with sessions.begin() as db:
            return await consume_next(db,'c',project=project)
    async def authority():
        async with sessions.begin() as db:
            await db.execute(text("SELECT id FROM agents WHERE id='a' FOR UPDATE"))
            await db.execute(text("SELECT id FROM conversations WHERE id='c' FOR UPDATE"))
    consumer=asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(),3)
    reader=asyncio.create_task(authority())
    try:
        await asyncio.sleep(.05)  # Allow authority to contend with the held projection locks.
        release.set()
        await asyncio.wait_for(asyncio.gather(consumer,reader),3)
        assert await admin.fetchval('SELECT product_seq FROM conversations')==2
        assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1
    finally:
        release.set()
        for task in (consumer,reader):
            if not task.done():
                task.cancel()
        await asyncio.gather(consumer,reader,return_exceptions=True)


async def test_notification_contending_with_sse_read_lock_retries_after_release(projection_database):
    """No further runtime event is needed after an SSE page releases FOR SHARE."""
    from workmate_product.coordination import WakeupBus
    sessions, admin = projection_database
    bus = WakeupBus('unused')
    service = compose_product_runtime(sessions, wakeup_bus=bus)
    idle, skipped, consumed, shutdown = (asyncio.Event() for _ in range(4))
    original_tick, original_step = service.tick, service._step

    async def tick(phase):
        result = await original_tick(phase)
        idle.set()
        return result

    async def step(phase, cid):
        result = await original_step(phase, cid)
        (consumed if result else skipped).set()
        return result

    service.tick, service._step = tick, step
    running = asyncio.create_task(service._loop('projection', shutdown))
    try:
        await asyncio.wait_for(idle.wait(), 3)
        async with sessions.begin() as reader:
            # Hold a Conversation read lock while the projection is notified.
            await reader.execute(text("SELECT id FROM conversations WHERE id='c' FOR SHARE"))
            await emit(admin, 1, {'type': 'REPLY_START', 'session_id': 's'})
            bus.signal_local('runtime')
            await asyncio.wait_for(skipped.wait(), 3)
            assert not consumed.is_set()
        # No new hint. The 1-second idle fallback must not penalize a skipped lock.
        await asyncio.wait_for(consumed.wait(), .45)
        assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 1
        assert not service.failures
    finally:
        shutdown.set()
        await asyncio.wait_for(running, 3)


@pytest.mark.parametrize('phase', ['projection', 'admission', 'delivery'])
@pytest.mark.parametrize('outcome', ['contended', 'failed', 'empty'])
async def test_notification_retry_is_bounded_and_failures_keep_idle_backoff(monkeypatch, phase, outcome):
    from workmate_product.coordination import WakeupBus
    bus, shutdown, intervals = WakeupBus('unused'), asyncio.Event(), []
    service = ProductProjectionService(None, project=lambda: None,
        admit_next=lambda: None, wakeup_bus=bus)

    async def candidates(_):
        return [] if outcome == 'empty' else ['c']

    async def step(*_):
        if outcome == 'failed':
            raise ValueError('fixture failure')
        return False

    async def wait(pulse, version, interval, stop):
        intervals.append(interval)
        if len(intervals) == 5:
            shutdown.set()

    service._candidates, service._step = candidates, step
    monkeypatch.setattr('workmate_product.projections.service.wait_for_wakeup', wait)
    await service._loop(phase, shutdown)
    assert intervals == ([.1, .1, .1, 1, 1] if outcome == 'contended' else [1] * 5)
    assert bool(service.failures) == (outcome == 'failed')
