"""Durable consumer integration on migrated isolated PostgreSQL schemas."""
import asyncio
import json

import pytest
import pytest_asyncio
from sqlalchemy import text

from test_command_admission import database as admission_database, accept_start, command, allow_agent, create_task
from workmate_product.projections.store import (
    consume_next, append_business_events, read_product_events,
    ProjectionResult, ProjectedEvent, ProjectionConflict,
)
from workmate_contracts import ProductCursor


@pytest_asyncio.fixture
async def database(admission_database):
    sessions, admin = admission_database
    async with sessions.begin() as db:
        await accept_start(db,command(),actor_user_id='u',storage_node_id='node',authorize_agent=allow_agent,prepare_task=create_task)
    await admin.execute("UPDATE runtime_sessions SET execution_epoch=1,active_task_id='t',reply_id='reply',status='running'")
    await admin.execute("UPDATE runtime_task_executions SET execution_epoch=1,reply_id='reply',status='running'")
    yield sessions, admin


async def source(admin, seq, raw, *, task='t'):
    await admin.execute('''INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
        VALUES ($1,'s',$2,1,$3,$4::jsonb)''',f'e{seq}',seq,task,json.dumps(raw))
    await admin.execute('UPDATE runtime_sessions SET runtime_seq=$1',seq)


async def project(db, event, state, boundary):
    state['text'] = state.get('text','') + event.payload.get('delta','')
    await db.execute(text("UPDATE task_messages SET content=:content WHERE task_id='t'"),{'content':state['text']})
    return ProjectionResult(state, [ProjectedEvent(key='text',event_type='TEXT_MESSAGE_CONTENT',
        payload={'type':'TEXT_MESSAGE_CONTENT','delta':event.payload.get('delta','')})])


async def test_two_consumers_apply_text_once_and_restart_from_durable_state(database):
    sessions, admin = database
    await source(admin,1,{'type':'TEXT_BLOCK_DELTA','reply_id':'reply','delta':'a'})
    async def consume():
        async with sessions.begin() as db:
            return await consume_next(db,'c',project=project)
    results = await asyncio.gather(consume(),consume())
    assert sum(item is not None for item in results) == 1
    assert await admin.fetchval('SELECT content FROM task_messages') == 'a'
    await source(admin,2,{'type':'TEXT_BLOCK_DELTA','reply_id':'reply','delta':'b'})
    await consume()
    assert await admin.fetchval('SELECT content FROM task_messages') == 'ab'
    assert await admin.fetchval('SELECT product_seq FROM conversations') == 2
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 2


async def test_projection_failure_rolls_back_effects_state_events_and_cursor(database):
    sessions, admin = database
    await source(admin,1,{'type':'TEXT_BLOCK_DELTA','reply_id':'reply','delta':'a'})
    async def failing(db,event,state,boundary):
        await project(db,event,state,boundary)
        raise RuntimeError('injected')
    async with sessions.begin() as db:
        with pytest.raises(RuntimeError,match='injected'):
            await consume_next(db,'c',project=failing)
    assert await admin.fetchval('SELECT content FROM task_messages') == 'hello'
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    assert await admin.fetchval('SELECT count(*) FROM task_projection_states') == 0
    assert await admin.fetchval('SELECT product_seq FROM conversations') == 0


async def test_business_events_and_runtime_events_use_one_product_sequence(database):
    sessions, admin = database
    await source(admin,1,{'type':'TEXT_BLOCK_DELTA','reply_id':'reply','delta':'a'})
    item=ProjectedEvent(key='decision',event_type='CUSTOM',payload={'type':'CUSTOM','name':'HITL_DECISION_RECORDED'})
    async with sessions.begin() as db:
        await append_business_events(db,'c',operation_key='decision-1',items=[item],task_id='t',reply_id='reply')
    async with sessions.begin() as db:
        await consume_next(db,'c',project=project)
    async with sessions.begin() as db:
        retry=await append_business_events(db,'c',operation_key='decision-1',items=[item],task_id='t',reply_id='reply')
        assert retry == (1,)
        page=await read_product_events(db,ProductCursor(conversation_id='c',product_seq=0),limit=1)
        assert [event.product_seq for event in page.events] == [1]
        assert page.high_watermark == 2
        next_page=await read_product_events(db,page.cursor,limit=5)
        assert [event.product_seq for event in next_page.events] == [2]
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 1


async def test_same_business_identity_with_different_content_is_rejected(database):
    sessions, admin = database
    async with sessions.begin() as db:
        await append_business_events(db,'c',operation_key='op',items=[ProjectedEvent(key='a',event_type='CUSTOM',payload={'type':'CUSTOM'})])
    async with sessions.begin() as db:
        with pytest.raises(ProjectionConflict):
            await append_business_events(db,'c',operation_key='op',items=[ProjectedEvent(key='a',event_type='CUSTOM',payload={'type':'CUSTOM','changed':True})])
    assert await admin.fetchval('SELECT product_seq FROM conversations') == 1


@pytest.mark.parametrize('raw',[
    {'type':'EXECUTION_BOUNDARY','reply_id':'reply','status':'completed'},
    {'type':'TEXT_BLOCK_DELTA','reply_id':'foreign','delta':'bad'},
])
async def test_unproven_boundary_or_foreign_reply_does_not_advance_cursor(database,raw):
    sessions, admin = database
    await source(admin,1,raw)
    async with sessions.begin() as db:
        with pytest.raises(ProjectionConflict):
            await consume_next(db,'c',project=project)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0


async def test_gap_in_runtime_sequence_cannot_be_silently_skipped(database):
    sessions, admin = database
    await source(admin,2,{'type':'TEXT_BLOCK_DELTA','reply_id':'reply','delta':'bad'})
    async with sessions.begin() as db:
        with pytest.raises(ProjectionConflict):
            await consume_next(db,'c',project=project)


async def test_business_identity_distinguishes_json_boolean_and_number(database):
    sessions,admin=database
    async with sessions.begin() as db:
        await append_business_events(db,'c',operation_key='typed',items=[ProjectedEvent(
            key='x',event_type='CUSTOM',payload={'type':'CUSTOM','flag':True})])
    async with sessions.begin() as db:
        with pytest.raises(ProjectionConflict):
            await append_business_events(db,'c',operation_key='typed',items=[ProjectedEvent(
                key='x',event_type='CUSTOM',payload={'type':'CUSTOM','flag':1})])


async def test_exact_historical_boundary_is_passed_instead_of_latest_snapshot(database):
    sessions,admin=database
    await source(admin,1,{'type':'EXECUTION_BOUNDARY','reply_id':'reply','status':'completed'})
    await admin.execute("""INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('original','s','t',1,1,1,'completed','reply','{"history":"original"}'),
            ('later','s','t',2,5,1,'completed','reply','{"history":"later"}')""")
    async def boundary_projector(db,event,state,boundary):
        assert boundary['id']=='original'
        assert boundary['payload']=={'history':'original'}
        return ProjectionResult({'boundary_id':boundary['id']},[])
    async with sessions.begin() as db:
        await consume_next(db,'c',project=boundary_projector)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1
    assert await admin.fetchval('SELECT product_seq FROM conversations')==0


@pytest.mark.parametrize('kind',['future','gap','empty_gap','invalid_limit'])
async def test_invalid_or_missing_replay_is_explicit(database,kind):
    sessions,admin=database
    async with sessions.begin() as db:
        if kind in {'gap','empty_gap'}:
            await db.execute(text('UPDATE conversations SET product_seq=2'))
            if kind=='gap':
                await db.execute(text("""INSERT INTO product_events(id,conversation_id,product_seq,projection_item_key,event_type,occurred_at,payload)
                    VALUES ('gap','c',2,'a','CUSTOM',now(),'{"type":"CUSTOM"}')"""))
        with pytest.raises((ProjectionConflict,ValueError)):
            await read_product_events(db,ProductCursor(conversation_id='c',product_seq=1 if kind=='future' else 0),
                limit=0 if kind=='invalid_limit' else 5)


async def test_missing_committed_runtime_event_is_not_reported_idle(database):
    sessions,admin=database
    await admin.execute('UPDATE runtime_sessions SET runtime_seq=1')
    async with sessions.begin() as db:
        with pytest.raises(ProjectionConflict,match='missing'):
            await consume_next(db,'c',project=project)


async def enable_log(sessions, status):
    import uuid
    from workmate_product.persistence.repositories import event_logs
    async with sessions.begin() as db:
        state = await event_logs.prepare(db, 'c', lease_seconds=30)
        if status == 'active':
            assert await event_logs.activate(db, state, ('c', state.generation, str(uuid.uuid4())))


def mixed_events():
    return [
        ProjectedEvent(key='decision', event_type='CUSTOM',
                       payload={'type': 'CUSTOM', 'name': 'HITL_DECISION_RECORDED'}),
        ProjectedEvent(key='body', event_type='TEXT_MESSAGE_CONTENT',
                       payload={'type': 'TEXT_MESSAGE_CONTENT', 'delta': 'body'}),
    ]


@pytest.mark.parametrize('log_status', [None, 'initializing', 'active'])
async def test_business_sequence_and_outbox_do_not_depend_on_log_registration(database, log_status):
    sessions, admin = database
    if log_status:
        await enable_log(sessions, log_status)
    async with sessions.begin() as db:
        result = await append_business_events(db, 'c', operation_key='mixed', items=mixed_events())
        # The same transaction sees the staged event before committing.
        assert await db.scalar(text('SELECT count(*) FROM realtime_business_outbox')) == 1
    assert result == (1, 2)
    assert tuple(await admin.fetchrow('SELECT product_seq,business_seq FROM conversations')) == (2, 1)
    events = await admin.fetch('SELECT id,business_seq FROM product_events ORDER BY product_seq')
    assert [r['business_seq'] for r in events] == [1, None]
    outbox = await admin.fetch('SELECT event_id,business_seq,status FROM realtime_business_outbox')
    assert [tuple(r) for r in outbox] == [(events[0]['id'], 1, 'pending')]
    async with sessions.begin() as db:
        assert await append_business_events(db, 'c', operation_key='mixed', items=mixed_events()) == result
    assert await admin.fetch('SELECT id,business_seq FROM product_events ORDER BY product_seq') == events
    assert await admin.fetch('SELECT event_id,business_seq,status FROM realtime_business_outbox') == outbox
    assert tuple(await admin.fetchrow('SELECT product_seq,business_seq FROM conversations')) == (2, 1)


async def test_caller_rollback_removes_business_sequence_and_outbox(database):
    sessions, admin = database
    with pytest.raises(RuntimeError, match='abort caller'):
        async with sessions.begin() as db:
            await append_business_events(db, 'c', operation_key='rollback', items=mixed_events())
            assert await db.scalar(text('SELECT count(*) FROM realtime_business_outbox')) == 1
            raise RuntimeError('abort caller')
    assert tuple(await admin.fetchrow('SELECT product_seq,business_seq FROM conversations')) == (0, 0)
    assert await admin.fetchval('SELECT count(*) FROM product_events') == 0
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == 0


async def test_runtime_projection_stages_business_events_with_cursor(database):
    sessions, admin = database
    await enable_log(sessions, 'initializing')
    await source(admin, 1, {'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'reply', 'delta': 'a'})
    async def important(db, event, state, boundary):
        return ProjectionResult(state, mixed_events())
    async with sessions.begin() as db:
        await consume_next(db, 'c', project=important)
    async with sessions.begin() as db:
        assert await consume_next(db, 'c', project=important) is None
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 1
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == 1
    assert tuple(await admin.fetchrow('SELECT product_seq,business_seq FROM conversations')) == (2, 1)


async def test_all_ephemeral_body_events_never_enter_business_outbox(database):
    sessions, admin = database
    await enable_log(sessions, 'active')
    types = ('TEXT_MESSAGE_START', 'TEXT_MESSAGE_CONTENT', 'TEXT_MESSAGE_END',
             'REASONING_MESSAGE_START', 'REASONING_MESSAGE_CONTENT', 'REASONING_MESSAGE_END')
    items = [ProjectedEvent(key=kind, event_type=kind, payload={'type': kind}) for kind in types]
    items += [ProjectedEvent(key=name, event_type='CUSTOM', payload={'type': 'CUSTOM', 'name': name})
              for name in ('REASONING_DELTA',)]
    async with sessions.begin() as db:
        await append_business_events(db, 'c', operation_key='bodies', items=items)
    assert await admin.fetchval('SELECT product_seq FROM conversations') == len(items)
    assert await admin.fetchval('SELECT business_seq FROM conversations') == 0
    assert await admin.fetchval('SELECT count(*) FROM product_events WHERE business_seq IS NOT NULL') == 0
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == 0


async def test_runtime_projection_rollback_preserves_retry_and_outbox_atomicity(database):
    sessions, admin = database
    await enable_log(sessions, 'active')
    await source(admin, 1, {'type': 'TEXT_BLOCK_DELTA', 'reply_id': 'reply', 'delta': 'a'})
    async def important(db, event, state, boundary):
        await project(db, event, state, boundary)
        return ProjectionResult(state, mixed_events())
    with pytest.raises(RuntimeError, match='abort projection'):
        async with sessions.begin() as db:
            await consume_next(db, 'c', project=important)
            assert await db.scalar(text('SELECT count(*) FROM realtime_business_outbox')) == 1
            raise RuntimeError('abort projection')
    assert await admin.fetchval('SELECT content FROM task_messages') == 'hello'
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors') == 0
    assert await admin.fetchval('SELECT count(*) FROM task_projection_states') == 0
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == 0
    assert tuple(await admin.fetchrow('SELECT product_seq,business_seq FROM conversations')) == (0, 0)
    async with sessions.begin() as db:
        await consume_next(db, 'c', project=important)
    assert await admin.fetchval('SELECT content FROM task_messages') == 'a'
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors') == 1
    assert await admin.fetchval('SELECT count(*) FROM realtime_business_outbox') == 1
    assert tuple(await admin.fetchrow('SELECT product_seq,business_seq FROM conversations')) == (2, 1)
