"""Display cuts use caller transactions and the current execution authority."""
import uuid

import pytest
from workmate_contracts.realtime import ContentBlock, MessageSnapshot, RealtimeCursor, message_id
from workmate_product.application.message_snapshots import save_message_snapshot, SnapshotRejected
from workmate_product.persistence.models import TaskMessage
from test_execution_context import database


def snapshot(*, version=1, epoch=1, value='你好', content_version=1, phase='partial',
             position='100-0', closed=False, generation=None):
    return MessageSnapshot(message_id=message_id('t','reply'), conversation_id='c', task_id='t',
        reply_id='reply', execution_epoch=epoch, snapshot_version=version,
        blocks=[ContentBlock(block_id='block',kind='text',text=value,
                             content_version=content_version,closed=closed)],
        covered_cursor=RealtimeCursor(conversation_id='c',generation=generation or str(uuid.UUID(int=1)),
                                      stream_id=position),
        business_seq=0,phase=phase,incomplete=False,process_end=0)


async def running(admin):
    await admin.execute("""UPDATE conversations SET active_product_task_id='t';
        UPDATE tasks SET status='running',run_id='reply' WHERE id='t';
        UPDATE runtime_sessions SET active_task_id='t',owner_id='owner',execution_epoch=1,reply_id='reply',
          status='running',lease_until=clock_timestamp()+interval '60 seconds';
        UPDATE runtime_task_executions SET reply_id='reply',execution_epoch=1,status='running';""")


async def save(sessions, value, *, owner='owner'):
    async with sessions.begin() as db:
        return await save_message_snapshot(db,value,owner_id=owner)


@pytest.mark.asyncio
async def test_cut_is_stable_idempotent_and_visible_only_after_commit(database):
    sessions,admin=database
    await running(admin)
    captured=snapshot()
    async with sessions.begin() as db:
        saved=await save_message_snapshot(db,captured,owner_id='owner')
        assert saved.message_id==message_id('t','reply')
        assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==0
    assert await save(sessions,captured)==captured
    async with sessions() as db:
        row=await db.get(TaskMessage,captured.message_id)
        assert row.snapshot_version==1 and row.content['text']=='你好'
        assert row.covered_cursor.endswith('=') is False
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            await save_message_snapshot(db,snapshot(version=2,value='你好🌍',content_version=2,position='101-0'),owner_id='owner')
            raise RuntimeError('rollback')
    assert await admin.fetchval("SELECT snapshot_version FROM task_messages WHERE role='assistant'")==1


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[
    {'version':1,'value':'conflict'}, {'version':0}, {'version':2,'value':'no prefix','content_version':2},
    {'version':2,'value':'你好🌍','content_version':1}, {'version':2,'position':'99-0'},
    {'version':2,'epoch':2},
])
async def test_stale_conflicting_or_unowned_cut_cannot_replace_text(database,change):
    sessions,admin=database
    await running(admin)
    await save(sessions,snapshot())
    with pytest.raises(ValueError):
        await save(sessions,snapshot(**change))
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE role='assistant'")=='你好'


@pytest.mark.asyncio
async def test_new_epoch_keeps_original_content_and_cannot_reopen_closed_block(database):
    sessions,admin=database
    await running(admin)
    first=snapshot(closed=True,phase='approval')
    await save(sessions,first)
    await admin.execute("""UPDATE runtime_sessions SET owner_id='new',execution_epoch=2;
        UPDATE runtime_task_executions SET execution_epoch=2""")
    second=snapshot(version=2,epoch=2,closed=True,position='101-0')
    await save(sessions,second,owner='new')
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot(version=3,epoch=2,position='102-0'),owner='new')
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot(version=3,position='102-0'))
    third=second.model_copy(update={'snapshot_version':3,'blocks':[*second.blocks,
        ContentBlock(block_id='next',kind='text',text='续跑',content_version=1,closed=False)]})
    await save(sessions,third,owner='new')
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE role='assistant'")=='你好续跑'


@pytest.mark.asyncio
async def test_competing_cut_versions_cannot_mix_content(database):
    import asyncio
    sessions,admin=database
    await running(admin)
    outcomes=await asyncio.gather(save(sessions,snapshot(value='first')),
                                 save(sessions,snapshot(value='second')),return_exceptions=True)
    assert sum(isinstance(value,MessageSnapshot) for value in outcomes)==1
    assert sum(isinstance(value,SnapshotRejected) for value in outcomes)==1
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE role='assistant'") in {'first','second'}


@pytest.mark.asyncio
async def test_cut_cannot_discard_covered_cursor(database):
    sessions,admin=database
    await running(admin)
    await save(sessions,snapshot())
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot(version=2).model_copy(update={'covered_cursor':None}))


@pytest.mark.asyncio
async def test_final_cut_seals_message_and_preserves_legacy_presentation(database):
    sessions,admin=database
    await running(admin)
    final=snapshot(value=' 你好\n',phase='final',closed=True).model_copy(update={'process_end':2})
    await save(sessions,final)
    async with sessions() as db:
        row=await db.get(TaskMessage,final.message_id)
        assert row.content['text']=='你好'
        assert row.content['blocks'][0]['text']==' 你好\n'
        assert row.content['presentation']=={'version':1,'process_end':1}
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot(version=2,closed=True,phase='partial'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    "UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'",
    "UPDATE runtime_sessions SET owner_id='other'",
    "UPDATE conversations SET deleted_at=clock_timestamp()",
    "UPDATE conversations SET active_product_task_id=NULL",
    "UPDATE tasks SET run_id='other'",
])
async def test_current_execution_and_conversation_are_rechecked(database,mutation):
    sessions,admin=database
    await running(admin)
    await admin.execute(mutation)
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot())
    assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==0


@pytest.mark.asyncio
async def test_cut_does_not_adopt_later_business_watermark(database):
    sessions,admin=database
    await running(admin)
    await admin.execute('UPDATE conversations SET business_seq=8')
    await save(sessions,snapshot().model_copy(update={'business_seq':3}))
    assert await admin.fetchval("SELECT snapshot_business_seq FROM task_messages WHERE role='assistant'")==3
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot(version=2).model_copy(update={'business_seq':9}))


@pytest.mark.asyncio
async def test_log_activation_and_rotation_fence_captured_display_cuts(database):
    from workmate_product.realtime.log_manager import EventLogManager
    from test_realtime_log_manager import MemoryLog
    sessions,admin=database
    await running(admin)
    manager=EventLogManager(sessions,MemoryLog(sessions))
    candidate=await manager._prepare('c')
    with pytest.raises(SnapshotRejected,match='unconfirmed'):
        await save(sessions,snapshot(generation=candidate.generation))
    assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==0
    old=await manager.ensure('c')
    await save(sessions,snapshot(generation=old.generation))
    await manager._prepare('c',expected=old)
    with pytest.raises(SnapshotRejected,match='unconfirmed'):
        await save(sessions,snapshot(version=2,generation=old.generation))
    new=await manager.ensure('c')
    with pytest.raises(SnapshotRejected,match='superseded'):
        await save(sessions,snapshot(version=2,generation=old.generation))
    with pytest.raises(SnapshotRejected,match='unconfirmed'):
        await save(sessions,snapshot(version=2,generation=new.generation).model_copy(update={'covered_cursor':None}))
    current=snapshot(version=2,value='你好🌍',content_version=2,
                     generation=new.generation,position='1-0')
    assert await save(sessions,current)==current
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE role='assistant'")=='你好🌍'


@pytest.mark.asyncio
async def test_snapshot_keeps_generation_locked_until_commit(database):
    import asyncpg
    from workmate_product.realtime.log_manager import EventLogManager
    from test_realtime_log_manager import MemoryLog
    sessions,admin=database
    await running(admin)
    current=await EventLogManager(sessions,MemoryLog(sessions)).ensure('c')
    async with sessions.begin() as db:
        await save_message_snapshot(db,snapshot(generation=current.generation),owner_id='owner')
        with pytest.raises(asyncpg.LockNotAvailableError):
            await admin.fetchrow("SELECT * FROM conversation_event_logs WHERE conversation_id='c' FOR UPDATE NOWAIT")
    assert await admin.fetchrow("SELECT * FROM conversation_event_logs WHERE conversation_id='c' FOR UPDATE NOWAIT")


@pytest.mark.asyncio
async def test_reply_binding_does_not_wait_for_product_start_projection(database):
    sessions,admin=database
    await running(admin)
    await admin.execute('UPDATE tasks SET run_id=NULL')
    await save(sessions,snapshot())
    assert await admin.fetchval("SELECT run_id FROM tasks WHERE id='t'") is None
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE role='assistant'")=='你好'


@pytest.mark.asyncio
@pytest.mark.parametrize('reply',[None,'other'])
async def test_display_cut_requires_runtime_session_reply_binding(database,reply):
    sessions,admin=database
    await running(admin)
    await admin.execute('UPDATE runtime_sessions SET reply_id=$1',reply)
    with pytest.raises(SnapshotRejected):
        await save(sessions,snapshot())
    assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==0


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['partial','approval','final','interrupted'])
async def test_unanchored_cut_preserves_text_during_rotation_then_reanchors(database,phase):
    from workmate_product.realtime.log_manager import EventLogManager
    from test_realtime_log_manager import MemoryLog
    sessions,admin=database
    await running(admin)
    manager=EventLogManager(sessions,MemoryLog(sessions))
    old=await manager.ensure('c')
    await save(sessions,snapshot(generation=old.generation))
    await manager._prepare('c',expected=old)
    unanchored=snapshot(version=2,value='你好🌍',content_version=2,phase=phase,
        closed=phase in {'final','interrupted'}).model_copy(update={'covered_cursor':None})
    async with sessions.begin() as db:
        assert await save_message_snapshot(db,unanchored,owner_id='owner',unanchored=True)==unanchored
    assert await admin.fetchval("SELECT covered_cursor FROM task_messages WHERE role='assistant'") is None
    assert await admin.fetchval("SELECT content->>'text' FROM task_messages WHERE role='assistant'")=='你好🌍'
    assert not await admin.fetchval("SELECT snapshot_incomplete FROM task_messages WHERE role='assistant'")
    if phase not in {'final','interrupted'}:
        new=await manager.ensure('c')
        anchored=unanchored.model_copy(update={'snapshot_version':3,'covered_cursor':
            RealtimeCursor(conversation_id='c',generation=new.generation,stream_id='1-0')})
        assert await save(sessions,anchored)==anchored
    else:
        with pytest.raises(SnapshotRejected,match='terminal'):
            async with sessions.begin() as db:
                await save_message_snapshot(db,unanchored.model_copy(update={
                    'snapshot_version':3,'phase':'partial'}),owner_id='owner',unanchored=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[
    {'value':'not a prefix','content_version':2},
    {'epoch':2},
    {'content_version':0},
])
async def test_unanchored_cut_cannot_bypass_content_or_execution_checks(database,change):
    sessions,admin=database
    await running(admin)
    await save(sessions,snapshot())
    value=snapshot(version=2,**change).model_copy(update={'covered_cursor':None})
    with pytest.raises(SnapshotRejected):
        async with sessions.begin() as db:
            await save_message_snapshot(db,value,owner_id='owner',unanchored=True)
    assert await admin.fetchval("SELECT snapshot_version FROM task_messages WHERE role='assistant'")==1


@pytest.mark.asyncio
async def test_unanchored_cut_never_silently_discards_supplied_cursor(database):
    sessions,admin=database
    await running(admin)
    async with sessions.begin() as db:
        with pytest.raises(SnapshotRejected,match='cursor'):
            await save_message_snapshot(db,snapshot(),owner_id='owner',unanchored=True)


@pytest.mark.asyncio
async def test_expiry_during_message_write_rolls_back_even_when_caller_catches(database,monkeypatch):
    import asyncio
    import workmate_product.application.message_snapshots as application
    sessions,admin=database
    await running(admin)
    write=application.write_message
    async def delayed(*args,**kwargs):
        result=await write(*args,**kwargs)
        await asyncio.sleep(.4)
        return result
    monkeypatch.setattr(application,'write_message',delayed)
    await admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()+interval '300 milliseconds'")
    async with sessions.begin() as db:
        with pytest.raises(SnapshotRejected,match='expired'):
            await save_message_snapshot(db,snapshot(),owner_id='owner')
    assert await admin.fetchval("SELECT count(*) FROM task_messages WHERE role='assistant'")==0


@pytest.mark.asyncio
async def test_appending_text_cannot_clear_known_message_loss(database):
    sessions,admin=database
    await running(admin)
    lost=snapshot().model_copy(update={'incomplete':True})
    await save(sessions,lost)
    recovered=snapshot(version=2,value='你好🌍',content_version=2,position='101-0')
    with pytest.raises(SnapshotRejected,match='incomplete'):
        await save(sessions,recovered)
    kept=recovered.model_copy(update={'incomplete':True})
    assert await save(sessions,kept)==kept
    assert await admin.fetchval("SELECT snapshot_incomplete FROM task_messages WHERE role='assistant'")


@pytest.mark.asyncio
async def test_initial_schema_snapshot_columns_and_persistence(database):
    from sqlalchemy import inspect
    sessions, admin = database
    async with sessions.begin() as db:
        def inspect_schema(connection):
            columns={c['name']:c for c in inspect(connection).get_columns('task_messages')}
            assert set(columns)==set(TaskMessage.__table__.columns.keys())
            for column in TaskMessage.__table__.columns:
                assert columns[column.name]['nullable']==column.nullable
            assert {c['name'] for c in inspect(connection).get_check_constraints('task_messages')}=={
                c.name for c in TaskMessage.__table__.constraints if c.__class__.__name__=='CheckConstraint'}
        await (await db.connection()).run_sync(inspect_schema)
    await running(admin)
    await save(sessions,snapshot())
    assert await admin.fetchval("SELECT snapshot_version FROM task_messages WHERE role='assistant'")==1


@pytest.mark.asyncio
async def test_superseded_owner_cannot_save_display_snapshot(database):
    sessions, admin = database
    await running(admin)
    await admin.execute("UPDATE runtime_sessions SET superseded_at=clock_timestamp()")
    with pytest.raises(SnapshotRejected):
        await save(sessions, snapshot())
