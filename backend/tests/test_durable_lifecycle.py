"""Safe terminal settlement is atomic with durable ingestion and browser replay."""
import asyncio
from decimal import Decimal
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.adapter.durable_lifecycle import finalize_terminal_boundary
from app.adapter.durable_projection import live_projector, ProjectionDeferred
from workmate_product.persistence.models import Task, TaskMessage, TaskEvent
from workmate_contracts.realtime import message_id, MessageSnapshotReference
from workmate_product.projections.store import consume_next, ProjectionConflict
from test_durable_projection import projection_database, emit  # noqa: F401


async def consume(sessions):
    async with sessions.begin() as db:
        return await consume_next(db, 'c', project=live_projector(
            finalize_boundary=finalize_terminal_boundary))


async def stream(sessions, admin, events):
    for raw in events:
        seq=(await admin.fetchval('SELECT runtime_seq FROM runtime_sessions'))+1
        await emit(admin,seq,raw)
        await consume(sessions)


async def boundary(admin, *, status='completed', pending=None, display=None, extra=None, missing_display=False):
    seq=(await admin.fetchval('SELECT runtime_seq FROM runtime_sessions'))+1
    raw={'type':'EXECUTION_BOUNDARY','status':status,'pending_tool_call_ids':pending or []}
    if display is not None:
        raw['display_snapshot']=display
    elif not missing_display and extra is None:
        raw['display_empty']=True
    if extra is not None:
        raw.update(extra)
    await emit(admin,seq,raw)
    await admin.execute('''INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,
        runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('safe','s','t',1,$1,1,$2,'reply','{}')''',seq,status)
    return seq


START={'type':'REPLY_START','session_id':'s'}
END={'type':'REPLY_END','finished_reason':'completed'}


async def test_restart_and_competing_consumers_settle_once_from_persisted_facts(projection_database):
    sessions,admin=projection_database
    async with sessions.begin() as db:
        task=await db.get(Task,'t')
        task.usage_model_id='model'
        task.usage_pricing={'input_per_k':'1','output_per_k':'2'}
        task.usage_currency='USD'
    await emit(admin,1,START)
    await admin.execute("UPDATE runtime_events SET created_at='2026-09-15 00:00:00+00' WHERE id='1'")
    await consume(sessions)
    await stream(sessions,admin,[
        {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'read'},
        {'type':'TOOL_RESULT_END','tool_call_id':'call','state':'success'},
        {'type':'MODEL_CALL_START','model_name':'model'},
        {'type':'MODEL_CALL_END','input_tokens':1000,'output_tokens':500},
    ])
    end_seq=(await admin.fetchval('SELECT runtime_seq FROM runtime_sessions'))+1
    await emit(admin,end_seq,END)
    await admin.execute("UPDATE runtime_events SET created_at='2026-09-15 00:00:03+00' WHERE id=$1",str(end_seq))
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==0
    reference=await saved_display(sessions,admin,body='  Plan. Answer.  ',process_end=8)
    seq=await boundary(admin,display=reference)
    await asyncio.gather(consume(sessions),consume(sessions))
    await consume(sessions)
    async with sessions() as db:
        task=await db.get(Task,'t')
        assert (task.status,task.duration_ms,task.steps,task.model_name)==('completed',3000,1,'model')
        assert task.token_usage=={'input_tokens':1000,'output_tokens':500}
        assert task.usage_cost==Decimal('2')
        message=(await db.scalars(select(TaskMessage))).one()
        assert message.id==message_id('t','reply')
        assert message.content['text']=='Plan. Answer.'
        assert message.content['presentation']=={'version':1,'process_end':6}
        assert message.content['blocks'][0]['text']=='  Plan. Answer.  '
        audits=(await db.scalars(select(TaskEvent).order_by(TaskEvent.seq))).all()
        assert [a.event_type for a in audits]==['run_started','tool_call','tool_result','run_finished','run_metrics']
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_FINISHED'")==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert state['terminal_boundary_id']=='safe'
    assert 'model_calls' not in state


@pytest.mark.parametrize('reason,status,tool,task_status,expected,public',[
    ('completed','completed','error','running','failed','RUN_ERROR'),
    ('completed','completed','denied','running','failed','RUN_ERROR'),
    ('completed','completed','interrupted','running','failed','RUN_ERROR'),
    ('completed','completed','success','running','completed','RUN_FINISHED'),
    ('error','failed',None,'running','failed','RUN_ERROR'),
    ('exceed_max_iters','interrupted',None,'running','interrupted','RUN_CANCELLED'),
    ('interrupted','interrupted',None,'running','interrupted','RUN_CANCELLED'),
    ('completed','completed',None,'cancelling','cancelled','RUN_CANCELLED'),
])
async def test_existing_terminal_rules_and_public_status_agree(projection_database,reason,status,tool,task_status,expected,public):
    sessions,admin=projection_database
    await stream(sessions,admin,[START])
    if tool:
        await stream(sessions,admin,[{'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'read'},
            {'type':'TOOL_RESULT_END','tool_call_id':'call','state':tool}])
    await admin.execute('UPDATE tasks SET status=$1',task_status)
    await stream(sessions,admin,[{'type':'REPLY_END','finished_reason':reason}])
    await boundary(admin,status=status)
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')==expected
    assert await admin.fetchval('SELECT count(*) FROM product_events WHERE event_type=$1',public)==1
    completed=json.loads(await admin.fetchval("SELECT payload FROM product_events WHERE payload->>'name'='TASK_COMPLETED'"))
    assert completed['value']['taskStatus']==expected
    assert completed['value']['reason']==reason
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==0
    if expected=='cancelled':
        assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='task_cancelled'")==1


@pytest.mark.parametrize('extra',[
    [{'type':'MODEL_CALL_START','model_name':'model'}],
    [{'type':'MODEL_CALL_START','model_name':'model'},{'type':'MODEL_CALL_END','input_tokens':0,'output_tokens':0}],
    [{'type':'MODEL_CALL_START','model_name':'model'},{'type':'MODEL_CALL_END','input_tokens':None,'output_tokens':1}],
    [{'type':'MODEL_CALL_START','model_name':'other'},{'type':'MODEL_CALL_END','input_tokens':1,'output_tokens':1}],
])
async def test_incomplete_or_mixed_model_usage_is_not_priced(projection_database,extra):
    sessions,admin=projection_database
    await admin.execute('''UPDATE tasks SET usage_model_id='model',
        usage_pricing='{"input_per_k":"1","output_per_k":"2"}',usage_currency='USD' ''')
    await stream(sessions,admin,[START,{'type':'MODEL_CALL_START','model_name':'model'},
        {'type':'MODEL_CALL_END','input_tokens':10,'output_tokens':5},*extra,END])
    await boundary(admin)
    await consume(sessions)
    usage=json.loads(await admin.fetchval('SELECT token_usage FROM tasks'))
    assert usage['input_tokens']>=10 and usage['output_tokens']>=5
    assert await admin.fetchval('SELECT usage_cost FROM tasks') is None


async def test_replay_failure_rolls_back_all_settlement_and_retry_succeeds(projection_database):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,
        END])
    reference=await saved_display(sessions,admin,body='Answer')
    seq=await boundary(admin,display=reference)
    await admin.execute("ALTER TABLE product_events ADD CONSTRAINT reject_terminal CHECK (event_type <> 'RUN_FINISHED')")
    async with sessions.begin() as db:
        with pytest.raises(IntegrityError):
            await consume_next(db,'c',project=live_projector(finalize_boundary=finalize_terminal_boundary))
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_finished'")==0
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1
    await admin.execute('ALTER TABLE product_events DROP CONSTRAINT reject_terminal')
    await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==1


@pytest.mark.parametrize('case',['missing_end','mismatched_end','pending','parked','interaction_audit','missing_start_state'])
async def test_missing_or_incompatible_evidence_never_finalizes(projection_database,case):
    sessions,admin=projection_database
    await stream(sessions,admin,[START])
    if case!='missing_end':
        await stream(sessions,admin,[END if case!='mismatched_end' else {'type':'REPLY_END','finished_reason':'error'}])
    if case=='interaction_audit':
        await admin.execute("INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload) VALUES ('t','c',2,'hitl_request','{}')")
    if case=='missing_start_state':
        await admin.execute("UPDATE task_projection_states SET state=state-'metrics'")
    seq=await boundary(admin,status='awaiting_interaction' if case=='parked' else 'completed',pending=['call'] if case=='pending' else [])
    with pytest.raises((ProjectionDeferred,ProjectionConflict)):
        await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==0


async def test_same_reply_start_preserves_accumulators_and_successful_retry(projection_database):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,
        {'type':'TOOL_CALL_START','tool_call_id':'first','tool_call_name':'read'},
        {'type':'TOOL_RESULT_END','tool_call_id':'first','state':'error'},
        {'type':'MODEL_CALL_START','model_name':'model'},
        {'type':'MODEL_CALL_END','input_tokens':10,'output_tokens':5},START,
        {'type':'TOOL_CALL_START','tool_call_id':'retry','tool_call_name':'read'},
        {'type':'TOOL_RESULT_END','tool_call_id':'retry','state':'success'},
        END])
    reference=await saved_display(sessions,admin,body='Before After',process_end=7)
    await boundary(admin,display=reference)
    await consume(sessions)
    async with sessions() as db:
        task=await db.get(Task,'t')
        assert (task.status,task.steps)==('completed',2)
        assert task.token_usage=={'input_tokens':10,'output_tokens':5}
        message=(await db.scalars(select(TaskMessage))).one()
        assert message.content['text']=='Before After'
        assert message.content['presentation']['process_end']==7
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_started'")==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_resumed'")==1


async def test_later_terminal_boundary_does_not_repeat_settlement(projection_database):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,
        END])
    reference=await saved_display(sessions,admin,body='Answer')
    seq=await boundary(admin,display=reference)
    await consume(sessions)
    await emit(admin,seq+1,{'type':'EXECUTION_BOUNDARY','status':'completed','pending_tool_call_ids':[]})
    await admin.execute('''INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,
        runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('later','s','t',2,$1,1,'completed','reply','{}')''',seq+1)
    await consume(sessions)
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='run_finished'")==1
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_FINISHED'")==1
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'


async def test_partial_cut_cannot_settle_and_final_cut_reuses_the_same_message(projection_database):
    from workmate_contracts.realtime import ContentBlock, MessageSnapshot
    from workmate_product.application.message_snapshots import save_message_snapshot
    sessions,admin=projection_database
    await stream(sessions,admin,[START,
        END])
    await admin.execute("UPDATE runtime_sessions SET owner_id='owner',lease_until=clock_timestamp()+interval '60 seconds'")
    partial=MessageSnapshot(message_id=message_id('t','reply'),conversation_id='c',task_id='t',reply_id='reply',
        execution_epoch=1,snapshot_version=1,blocks=[ContentBlock(block_id='b',kind='text',text='Answer',content_version=1,closed=False)],
        business_seq=0,phase='partial',incomplete=False,process_end=0)
    async with sessions.begin() as db:
        await save_message_snapshot(db,partial,owner_id='owner')
    final=partial.model_copy(update={'snapshot_version':2,'phase':'final',
        'blocks':[partial.blocks[0].model_copy(update={'closed':True})]})
    await boundary(admin,display=MessageSnapshotReference.from_snapshot(final).model_dump(mode='json'))
    with pytest.raises(ProjectionDeferred):
        await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_FINISHED'")==0
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await save_message_snapshot(db,final,owner_id='owner')
            raise RuntimeError('final commit lost')
    with pytest.raises(ProjectionDeferred):
        await consume(sessions)
    async with sessions.begin() as db:
        await save_message_snapshot(db,final,owner_id='owner')
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='completed'
    assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==1
    assert await admin.fetchval("SELECT id FROM task_messages WHERE role='assistant'")==partial.message_id


@pytest.mark.parametrize('completed',[False,True])
async def test_incomplete_interrupted_message_never_proves_success(projection_database,completed):
    from workmate_contracts.realtime import ContentBlock, MessageSnapshot
    from workmate_product.application.message_snapshots import save_message_snapshot
    sessions,admin=projection_database
    reason='completed' if completed else 'interrupted'
    await stream(sessions,admin,[START,
        {'type':'REPLY_END','finished_reason':reason}])
    await admin.execute("UPDATE runtime_sessions SET owner_id='owner',lease_until=clock_timestamp()+interval '60 seconds'")
    cut=MessageSnapshot(message_id=message_id('t','reply'),conversation_id='c',task_id='t',reply_id='reply',
        execution_epoch=1,snapshot_version=1,blocks=[ContentBlock(block_id='b',kind='text',text='Fragment',content_version=1,closed=True)],
        business_seq=0,phase='interrupted',incomplete=True,process_end=0)
    async with sessions.begin() as db:
        await save_message_snapshot(db,cut,owner_id='owner')
    await boundary(admin,status=reason,display=MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json'))
    if completed:
        with pytest.raises(ProjectionDeferred,match='display phase'):
            await consume(sessions)
        assert await admin.fetchval('SELECT status FROM tasks')=='running'
    else:
        await consume(sessions)
        assert await admin.fetchval('SELECT status FROM tasks')=='interrupted'
        assert await admin.fetchval("SELECT snapshot_incomplete FROM task_messages WHERE role='assistant'") is True
        assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_CANCELLED'")==1
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_FINISHED'")==0


@pytest.mark.parametrize('change',['none','version','digest','identity','epoch','missing'])
async def test_terminal_reference_requires_exact_committed_display_cut(projection_database,change):
    from workmate_contracts.realtime import ContentBlock,MessageSnapshot,MessageSnapshotReference
    from workmate_product.application.message_snapshots import save_message_snapshot
    sessions,admin=projection_database
    await stream(sessions,admin,[START,
        END])
    await admin.execute("UPDATE runtime_sessions SET owner_id='owner',lease_until=clock_timestamp()+interval '60 seconds'")
    cut=MessageSnapshot(message_id=message_id('t','reply'),conversation_id='c',task_id='t',reply_id='reply',
        execution_epoch=1,snapshot_version=1,blocks=[ContentBlock(block_id='b',kind='text',
            text='Saved body',content_version=1,closed=True)],business_seq=0,phase='final',incomplete=False)
    if change!='missing':
        async with sessions.begin() as db:
            await save_message_snapshot(db,cut,owner_id='owner')
    ref=MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json')
    if change=='version': ref['snapshot_version']+=1
    if change=='digest': ref['sha256']='a'*64
    if change=='identity': ref['message_id']=message_id('other','reply')
    if change=='epoch': ref['execution_epoch']=2
    seq=await boundary(admin,display=ref)
    if change=='none':
        await consume(sessions)
        assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'")=='completed'
    else:
        error=ProjectionDeferred if change=='missing' else ProjectionConflict
        with pytest.raises(error,match='display'):
            await consume(sessions)
        assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'")=='running'
        assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1
        assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_FINISHED'")==0


@pytest.mark.parametrize('mutation',[
    "state=jsonb_set(state,'{metrics,started_at}','\"invalid\"')",
    "state=jsonb_set(state,'{metrics,started_at}','\"2026-09-15T00:00:00\"')",
    "state=jsonb_set(state,'{translation,last_tool_result_state}','\"error\"')",
])
async def test_inconsistent_durable_projection_state_fails_closed(projection_database,mutation):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,END])
    await admin.execute('UPDATE task_projection_states SET '+mutation)
    seq=await boundary(admin)
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1


async def test_unmatched_model_end_preserves_known_usage_without_price(projection_database):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,{'type':'MODEL_CALL_END','input_tokens':10,'output_tokens':1},END])
    await boundary(admin)
    await consume(sessions)
    usage=json.loads(await admin.fetchval('SELECT token_usage FROM tasks'))
    assert usage=={'input_tokens':10,'output_tokens':1,'complete':False}


@pytest.mark.parametrize('invalid',[-1,True,1.5])
async def test_invalid_token_counters_do_not_advance_cursor(projection_database,invalid):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,{'type':'MODEL_CALL_START','model_name':'model'}])
    await emit(admin,3,{'type':'MODEL_CALL_END','input_tokens':invalid,'output_tokens':1})
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==2


async def saved_display(sessions, admin, *, body='Saved body', phase='final', process_end=0):
    from workmate_contracts.realtime import ContentBlock, MessageSnapshot, MessageSnapshotReference
    from workmate_product.application.message_snapshots import save_message_snapshot
    await admin.execute("UPDATE runtime_sessions SET owner_id='owner',lease_until=clock_timestamp()+interval '60 seconds'")
    cut = MessageSnapshot(message_id=message_id('t','reply'), conversation_id='c', task_id='t',
        reply_id='reply', execution_epoch=1, snapshot_version=1,
        blocks=[ContentBlock(block_id='b',kind='text',text=body,content_version=1,closed=True)] if body else [],
        business_seq=0, phase=phase, incomplete=False,process_end=process_end)
    async with sessions.begin() as db:
        await save_message_snapshot(db, cut, owner_id='owner')
    return MessageSnapshotReference.from_snapshot(cut).model_dump(mode='json')


@pytest.mark.parametrize('reason,status,task_status', [
    ('completed','completed','running'), ('error','failed','running'),
    ('interrupted','interrupted','running'), ('completed','completed','cancelling'),
])
async def test_public_terminal_events_preserve_exact_verified_display_reference(projection_database, reason, status, task_status):
    sessions, admin = projection_database
    await stream(sessions, admin, [START,
        {'type':'REPLY_END','finished_reason':reason}])
    await admin.execute('UPDATE tasks SET status=$1', task_status)
    reference = await saved_display(sessions, admin, phase='final' if reason == 'completed' else 'interrupted')
    seq = await boundary(admin, status=status, display=reference)
    await consume(sessions)
    public = [json.loads(row['payload']) for row in await admin.fetch(
        'SELECT payload FROM product_events WHERE source_runtime_event_id=$1 ORDER BY product_seq', str(seq))]
    assert public
    assert all(payload.get('display_snapshot') == reference for payload in public)
    assert all('display_empty' not in payload for payload in public)


async def test_public_terminal_events_preserve_verified_empty_display_evidence(projection_database):
    sessions, admin = projection_database
    await stream(sessions, admin, [START, END])
    seq = await boundary(admin, extra={'display_empty':True})
    await consume(sessions)
    public = [json.loads(row['payload']) for row in await admin.fetch(
        'SELECT payload FROM product_events WHERE source_runtime_event_id=$1 ORDER BY product_seq', str(seq))]
    assert public
    assert all(payload.get('display_empty') is True for payload in public)
    assert all('display_snapshot' not in payload for payload in public)
    assert await admin.fetchval('SELECT count(*) FROM task_messages') == 0


@pytest.mark.parametrize('conflict', ['dual_proof', 'saved_nonempty', 'legacy_nonempty', 'integer', 'string', 'null'])
async def test_invalid_empty_display_evidence_cannot_settle_terminal(projection_database, conflict):
    sessions, admin = projection_database
    await stream(sessions, admin, [START, END])
    if conflict == 'legacy_nonempty':
        async with sessions.begin() as db:
            db.add(TaskMessage(id=message_id('t','reply'),task_id='t',conversation_id='c',role='assistant',content={'text':'Legacy body'}))
    reference = None
    if conflict == 'dual_proof':
        reference = await saved_display(sessions, admin, body='')
    elif conflict == 'saved_nonempty':
        await saved_display(sessions, admin)
    value = {'integer':1, 'string':'true', 'null':None}.get(conflict, True)
    seq = await boundary(admin, display=reference, extra={'display_empty':value})
    with pytest.raises(ProjectionConflict):
        await consume(sessions)
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'running'
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == seq - 1
    assert await admin.fetchval('SELECT count(*) FROM product_events WHERE source_runtime_event_id=$1', str(seq)) == 0

async def test_terminal_without_display_proof_cannot_settle(projection_database):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,END])
    await boundary(admin,missing_display=True)
    with pytest.raises(ProjectionConflict,match='display'):
        await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT count(*) FROM task_messages')==0

@pytest.mark.parametrize('runtime_status,reason,display_phase',[
    ('completed','completed','interrupted'),('failed','error','final'),
])
async def test_terminal_requires_display_phase_matching_runtime_boundary(projection_database,runtime_status,reason,display_phase):
    sessions,admin=projection_database
    await stream(sessions,admin,[START,{'type':'REPLY_END','finished_reason':reason}])
    reference=await saved_display(sessions,admin,phase=display_phase)
    seq=await boundary(admin,status=runtime_status,display=reference)
    with pytest.raises(ProjectionDeferred,match='display phase'):
        await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==seq-1
