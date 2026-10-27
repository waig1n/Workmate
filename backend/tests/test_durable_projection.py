"""Real Product models and isolated PostgreSQL live projection."""
import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from workmate_product.persistence.models import (
    Base,
    User,
    Scope,
    Agent,
    Conversation,
    Task,
    TaskEvent,
    TaskProjectionState,
)
from app.adapter.durable_projection import live_projector, ProjectionDeferred
from workmate_product.projections.store import consume_next


@pytest_asyncio.fixture
async def projection_database():
    dsn=os.getenv('WORKMATE_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('requires isolated PostgreSQL')
    import asyncpg
    admin=await asyncpg.connect(dsn)
    schema='live_projection_'+uuid.uuid4().hex
    engine=None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path TO "{schema}"')
        engine=create_async_engine(dsn.replace('postgresql://','postgresql+asyncpg://'),
            connect_args={'server_settings':{'search_path':schema}})
        async with engine.begin() as db:
            from test_initial_schema import initial_migration
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            def initialize(connection):
                with Operations.context(MigrationContext.configure(connection)):
                    initial_migration().upgrade()
            await db.run_sync(initialize)
            for table in ('role_permissions', 'roles', 'permissions', 'scope_closure', 'scopes'):
                await db.execute(text(f'DELETE FROM {table}'))
        from product_catalog_fakes import MemoryBlobStore
        sessions=async_sessionmaker(engine,expire_on_commit=False,
            info={'skill_blob_store': MemoryBlobStore()})
        async with sessions.begin() as db:
            db.add(User(id='u',email='fixture@example.invalid',password_hash='fixture',display_name='Fixture'))
            db.add(Scope(id='scope',kind='organization',name='Fixture',normalized_name='fixture'))
            await db.flush()
            db.add(Agent(id='a',slug='fixture',display_name='Fixture',scope_id='scope'))
            await db.flush()
            db.add(Conversation(id='c',user_id='u',agent_id='a'))
            await db.flush()
            db.add(Task(id='t',conversation_id='c',title='Fixture'))
        await admin.execute("""
            UPDATE conversations SET active_product_task_id='t';
            INSERT INTO runtime_sessions(id,conversation_id,storage_node_id,active_task_id,execution_epoch,status,reply_id)
            VALUES ('s','c','node','t',1,'running','reply');
            INSERT INTO runtime_task_executions(task_id,session_id,execution_epoch,status,reply_id)
            VALUES ('t','s',1,'running','reply');
        """)
        yield sessions,admin
    finally:
        if engine:
            await engine.dispose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def emit(admin,seq,raw):
    await admin.execute('''INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
        VALUES ($1,'s',$2,1,'t',$3::jsonb)''',str(seq),seq,json.dumps({'reply_id':'reply',**raw}))
    await admin.execute('UPDATE runtime_sessions SET runtime_seq=$1',seq)


async def consume(sessions):
    async with sessions.begin() as db:
        return await consume_next(db,'c',project=live_projector())


async def test_live_audit_and_state_are_once_across_consumers_and_restart(projection_database):
    sessions,admin=projection_database
    events=[{'type':'REPLY_START','session_id':'s'},
        {'type':'TOOL_CALL_START','tool_call_id':'call','tool_call_name':'read','input':'secret'},
        {'type':'TOOL_RESULT_END','tool_call_id':'call','state':'success','content':'secret'}]
    for seq,event in enumerate(events,1):
        await emit(admin,seq,event)
        await asyncio.gather(consume(sessions),consume(sessions))
    async with sessions() as db:
        task=await db.get(Task,'t')
        assert (task.status,task.run_id)==('running','reply')
        state=await db.get(TaskProjectionState,'t')
        assert not {'text','text_open','process_end'} & state.state.keys()
        assert state.state['translation']['active_reply_id']=='reply'
        audits=(await db.scalars(select(TaskEvent).order_by(TaskEvent.seq))).all()
        assert [event.event_type for event in audits]==['run_started','tool_call','tool_result']
        assert audits[1].payload=={'toolCallId':'call','tool':'read','user':'u'}
        assert audits[2].payload=={'toolCallId':'call','tool':'read','user':'u','status':'success'}
        assert 'secret' not in json.dumps([event.payload for event in audits])
    assert await admin.fetchval('SELECT count(*) FROM product_events')>3
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==3


async def test_reply_end_waits_for_safe_boundary_and_lifecycle_finalizer(projection_database):
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await emit(admin,2,{'type':'REPLY_END','session_id':'s','finished_reason':'completed'})
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='running'
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE event_type='RUN_FINISHED'")==0
    await emit(admin,3,{'type':'EXECUTION_BOUNDARY','status':'completed'})
    await admin.execute("""INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('snapshot','s','t',1,3,1,'completed','reply','{}')""")
    with pytest.raises(ProjectionDeferred):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==2
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'


async def test_late_reply_start_does_not_revive_cancelled_task(projection_database):
    sessions,admin=projection_database
    await admin.execute("UPDATE tasks SET status='cancelled'")
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='cancelled'
    assert await admin.fetchval('SELECT count(*) FROM product_events')==0


async def test_unimplemented_interaction_keeps_cursor_before_request(projection_database):
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await emit(admin,2,{'type':'REQUIRE_USER_CONFIRM','tool_calls':[]})
    with pytest.raises(ProjectionDeferred):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1


async def test_cancelling_task_does_not_return_to_running(projection_database):
    sessions,admin=projection_database
    await admin.execute("UPDATE tasks SET status='cancelling'")
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    assert await admin.fetchval('SELECT status FROM tasks')=='cancelling'


async def test_installed_finalizer_receives_persisted_end_and_exact_boundary(projection_database):
    from workmate_product.projections.store import ProjectionResult
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await emit(admin,2,{'type':'REPLY_END','finished_reason':'completed'})
    await consume(sessions)
    await emit(admin,3,{'type':'EXECUTION_BOUNDARY','status':'completed'})
    await admin.execute("""INSERT INTO runtime_snapshots(id,session_id,task_id,state_version,runtime_seq,execution_epoch,status,reply_id,payload)
        VALUES ('exact','s','t',1,3,1,'completed','reply','{}')""")
    async def finalize(db,event,state,boundary):
        assert state['pending_reply_end']['finished_reason']=='completed'
        assert boundary['id']=='exact'
        state['seen_boundary']=boundary['id']
        return ProjectionResult(state,[])
    async with sessions.begin() as db:
        await consume_next(db,'c',project=live_projector(finalize_boundary=finalize))
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==3
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'


async def test_replay_insert_failure_rolls_back_task_and_audit(projection_database):
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await admin.execute("ALTER TABLE product_events ADD CHECK (event_type <> 'RUN_STARTED')")
    from sqlalchemy.exc import IntegrityError
    async with sessions.begin() as db:
        with pytest.raises(IntegrityError):
            await consume_next(db,'c',project=live_projector())
    assert await admin.fetchval('SELECT status FROM tasks')=='created'
    assert await admin.fetchval('SELECT run_id FROM tasks') is None
    assert await admin.fetchval('SELECT count(*) FROM task_events')==0
    assert await admin.fetchval('SELECT count(*) FROM runtime_projection_cursors')==0


async def test_knowledge_sources_audit_and_browser_event_are_atomic_once(projection_database):
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    value={'reply_id':'reply','sources':[{'source_id':'kb-source','knowledge_base_id':'kb',
        'document_id':'doc','filename':'policy.md','excerpt':'Policy','metadata':{},'score':1.0}]}
    await emit(admin,2,{'type':'CUSTOM','name':'knowledge_sources','value':value})
    await asyncio.gather(consume(sessions),consume(sessions))
    async with sessions() as db:
        audits=(await db.scalars(select(TaskEvent).where(TaskEvent.event_type=='knowledge_sources'))).all()
        assert len(audits)==1
        assert {key: audits[0].payload[key] for key in value} == value
        assert audits[0].payload['browser_projection']['details_available'] is True
    assert await admin.fetchval("SELECT count(*) FROM product_events WHERE payload->>'name'='knowledge_status'")==1


async def test_mismatched_knowledge_reply_does_not_advance_or_publish(projection_database):
    from workmate_product.projections.store import ProjectionConflict
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await emit(admin,2,{'type':'CUSTOM','name':'knowledge_sources',
        'value':{'reply_id':'other','sources':[]}})
    with pytest.raises(ProjectionConflict,match='Knowledge sources'):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1
    assert await admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='knowledge_sources'")==0


async def test_native_start_identifies_product_task(projection_database):
    import json
    sessions, admin = projection_database
    await emit(admin, 1, {'type':'REPLY_START','session_id':'s','taskId':'untrusted'})
    await consume(sessions)
    payload = json.loads(await admin.fetchval("SELECT payload FROM product_events WHERE event_type='RUN_STARTED'"))
    assert payload['taskId'] == 't'
    assert payload['runId'] == 'reply'

@pytest.mark.parametrize('kind',[f'{block}_BLOCK_{action}' for block in ('TEXT','THINKING') for action in ('START','DELTA','END')])
async def test_body_never_enters_durable_product_projection(projection_database,kind):
    from workmate_product.projections.store import ProjectionConflict
    sessions,admin=projection_database
    await emit(admin,1,{'type':'REPLY_START','session_id':'s'})
    await consume(sessions)
    await emit(admin,2,{'type':kind,'block_id':'b','delta':'ephemeral'})
    with pytest.raises(ProjectionConflict,match='body'):
        await consume(sessions)
    assert await admin.fetchval('SELECT last_runtime_seq FROM runtime_projection_cursors')==1
    state=json.loads(await admin.fetchval('SELECT state FROM task_projection_states'))
    assert not {'text','text_open','process_end'} & state.keys()


async def test_superseded_session_events_cannot_project_into_current_conversation(projection_database):
    sessions, admin = projection_database
    await emit(admin, 1, {'type': 'REPLY_START'})
    await admin.execute("""
        UPDATE runtime_sessions SET superseded_at=clock_timestamp() WHERE id='s';
        INSERT INTO runtime_sessions(id,conversation_id,storage_node_id) VALUES ('new-session','c','node');
        INSERT INTO runtime_projection_cursors(conversation_id,session_id,last_runtime_seq,version)
          VALUES ('c','new-session',0,1);
    """)
    assert await consume(sessions) is None
    assert await admin.fetchval("SELECT status FROM tasks WHERE id='t'") == 'created'
    assert await admin.fetchval('SELECT count(*) FROM task_events') == 0
    assert await admin.fetchval("SELECT last_runtime_seq FROM runtime_projection_cursors WHERE conversation_id='c'") == 0
