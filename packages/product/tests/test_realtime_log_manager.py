"""PG grants the current log identity; all Redis work is outside transactions."""
import asyncio
import uuid

import pytest

from workmate_product.realtime.log_manager import EventLogManager, LogUnavailable
from workmate_product.realtime.redis_log import LogRef, LogLost
from test_execution_context import database


class MemoryLog:
    def __init__(self,sessions,*,assert_idle=True):
        self.logs={}
        self.engine=sessions.kw['bind']
        self.calls=[]
        self.assert_idle=assert_idle

    def idle(self):
        # Other coordinators may own connections; serial cases independently
        # verify that this coordinator returns its own connection before I/O.
        if self.assert_idle:
            assert self.engine.pool.checkedout()==0

    async def initialize(self,conversation,generation):
        self.idle()
        await asyncio.sleep(0)
        self.calls.append((conversation,generation))
        return self.logs.setdefault((conversation,generation),LogRef(conversation,generation,str(uuid.uuid4())))

    async def window(self,ref):
        self.idle()
        if self.logs.get((ref.conversation_id,ref.generation))!=ref:
            raise LogLost('lost')


@pytest.mark.asyncio
async def test_competing_managers_activate_one_current_pair(database):
    sessions,admin=database
    log=MemoryLog(sessions,assert_idle=False)
    first,second=EventLogManager(sessions,log),EventLogManager(sessions,log)
    refs=await asyncio.gather(first.ensure('c'),second.ensure('c'))
    assert refs[0]==refs[1]
    row=await admin.fetchrow('SELECT * FROM conversation_event_logs')
    assert row['status']=='active' and row['version']==1
    assert row['generation']==refs[0].generation and row['incarnation']==refs[0].incarnation
    assert await first.is_current(refs[0])


@pytest.mark.asyncio
async def test_missing_or_recreated_log_rotates_without_reusing_identity(database):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    old=await manager.ensure('c')
    log.logs[(old.conversation_id,old.generation)]=LogRef('c',old.generation,str(uuid.uuid4()))
    new=await manager.ensure('c')
    assert new.generation!=old.generation
    assert not await manager.is_current(old)
    assert await manager.is_current(new)
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==2


@pytest.mark.asyncio
async def test_initialization_response_lost_reuses_existing_nonce(database,monkeypatch):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    initialize=log.initialize
    async def lost(*args):
        await initialize(*args)
        raise TimeoutError('response lost')
    monkeypatch.setattr(log,'initialize',lost)
    with pytest.raises(TimeoutError):
        await manager.ensure('c')
    assert await admin.fetchval('SELECT status FROM conversation_event_logs')=='initializing'
    created=next(iter(log.logs.values()))
    monkeypatch.setattr(log,'initialize',initialize)
    assert await manager.ensure('c')==created
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==1


@pytest.mark.asyncio
async def test_activation_commit_response_lost_is_confirmed_by_current_state(database,monkeypatch):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    activate=manager._activate
    async def lost(*args):
        await activate(*args)
        raise TimeoutError('commit response lost')
    monkeypatch.setattr(manager,'_activate',lost)
    with pytest.raises(TimeoutError):
        await manager.ensure('c')
    assert await admin.fetchval('SELECT status FROM conversation_event_logs')=='active'
    monkeypatch.setattr(manager,'_activate',activate)
    current=await manager.ensure('c')
    assert await manager.is_current(current)
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==1


@pytest.mark.asyncio
async def test_expired_initializer_cannot_activate_after_new_claim(database):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    old=await manager._prepare('c')
    await admin.execute("UPDATE conversation_event_logs SET lease_until=clock_timestamp()-interval '1 second'")
    new=await manager._prepare('c')
    assert new.version==old.version+1 and new.token!=old.token
    pair=await log.initialize('c',new.generation)
    assert not await manager._activate(old,pair)
    assert await manager._activate(new,pair)
    assert await manager.ensure('c')==pair


@pytest.mark.asyncio
async def test_old_rotation_claim_cannot_restore_new_current_generation(database):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    old=await manager.ensure('c')
    candidate=await manager._prepare('c',expected=old)
    await admin.execute("UPDATE conversation_event_logs SET lease_until=clock_timestamp()-interval '1 second'")
    new=await manager.ensure('c')
    late=await log.initialize('c',candidate.generation)
    assert not await manager._activate(candidate,late)
    assert await manager.is_current(new)
    assert not await manager.is_current(old)


@pytest.mark.asyncio
async def test_network_outage_does_not_rotate_log_or_assert_loss(database,monkeypatch):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    await manager.ensure('c')
    async def outage(_):
        raise ConnectionError('offline')
    monkeypatch.setattr(log,'window',outage)
    with pytest.raises(ConnectionError):
        await manager.ensure('c')
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==1


@pytest.mark.asyncio
async def test_deleted_conversation_cannot_initialize_or_retain_log_access(database):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    old=await manager.ensure('c')
    await admin.execute('UPDATE conversations SET deleted_at=clock_timestamp()')
    assert not await manager.is_current(old)
    with pytest.raises(LogUnavailable):
        await manager.ensure('c')


@pytest.mark.asyncio
async def test_corrupt_initializing_log_rotates_live_claim_instead_of_repairing(database,monkeypatch):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    damaged=await manager._prepare('c')
    initialize=log.initialize
    async def corrupt(conversation,generation):
        if generation==damaged.generation:
            log.idle()
            raise LogLost('partial metadata')
        return await initialize(conversation,generation)
    monkeypatch.setattr(log,'initialize',corrupt)
    current=await manager.ensure('c')
    assert current.generation!=damaged.generation
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==2


@pytest.mark.asyncio
async def test_stale_rotation_observation_cannot_displace_already_activated_pair(database):
    sessions,admin=database
    log=MemoryLog(sessions)
    manager=EventLogManager(sessions,log)
    candidate=await manager._prepare('c')
    ref=await log.initialize('c',candidate.generation)
    assert await manager._activate(candidate,ref)
    observed=await manager._prepare('c',expected=candidate)
    assert observed.identity==(ref.conversation_id,ref.generation,ref.incarnation)
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==1


from test_realtime_redis_log import log as redis_log


@pytest.mark.asyncio
async def test_real_pg_redis_competing_initializers_and_delayed_orphan(database,redis_log):
    sessions,admin=database
    a,b=EventLogManager(sessions,redis_log),EventLogManager(sessions,redis_log)
    refs=await asyncio.gather(a.ensure('c'),b.ensure('c'))
    assert refs[0]==refs[1]
    abandoned=await a._prepare('c',expected=refs[0])
    await admin.execute("UPDATE conversation_event_logs SET lease_until=clock_timestamp()-interval '1 second'")
    latest=await b.ensure('c')
    orphan=await redis_log.initialize('c',abandoned.generation)
    assert not await a._activate(abandoned,orphan)
    assert not await a.is_current(orphan)
    assert await a.ensure('c')==latest
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==3


@pytest.mark.asyncio
@pytest.mark.parametrize('missing',[0,1])
async def test_real_pg_redis_partial_initialization_loss_selects_new_generation(database,redis_log,missing):
    sessions,admin=database
    manager=EventLogManager(sessions,redis_log)
    candidate=await manager._prepare('c')
    ref=await redis_log.initialize('c',candidate.generation)
    await redis_log.client.delete(redis_log.keys('c',candidate.generation)[missing])
    replacement=await manager.ensure('c')
    assert replacement.generation!=candidate.generation
    assert not await manager.is_current(ref)
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==2


@pytest.mark.asyncio
async def test_real_pg_redis_lost_initialization_response_and_old_nonce_recreation(database,redis_log,monkeypatch):
    sessions,admin=database
    manager=EventLogManager(sessions,redis_log)
    initialize=redis_log.initialize
    window=redis_log.window
    created=[]
    async def lost(*args):
        assert sessions.kw['bind'].pool.checkedout()==0
        created.append(await initialize(*args))
        raise TimeoutError('initialization response lost')
    monkeypatch.setattr(redis_log,'initialize',lost)
    with pytest.raises(TimeoutError):
        await manager.ensure('c')
    async def checked_initialize(*args):
        assert sessions.kw['bind'].pool.checkedout()==0
        return await initialize(*args)
    async def checked_window(*args):
        assert sessions.kw['bind'].pool.checkedout()==0
        return await window(*args)
    monkeypatch.setattr(redis_log,'initialize',checked_initialize)
    monkeypatch.setattr(redis_log,'window',checked_window)
    confirmed=await manager.ensure('c')
    assert confirmed==created[0]
    await redis_log.client.delete(*redis_log.keys('c',confirmed.generation))
    orphan=await initialize('c',confirmed.generation)
    assert orphan.incarnation!=confirmed.incarnation
    with pytest.raises(LogLost):
        await redis_log.window(confirmed)
    assert not await manager.is_current(orphan)
    replacement=await manager.ensure('c')
    assert replacement.generation!=confirmed.generation
    assert await admin.fetchval('SELECT version FROM conversation_event_logs')==2


@pytest.mark.asyncio
async def test_generation_repository_respects_caller_transaction_and_activation_rollback(database):
    from workmate_product.persistence.repositories import event_logs
    sessions,admin=database
    with pytest.raises(RuntimeError,match='rollback preparation'):
        async with sessions.begin() as db:
            await event_logs.prepare(db,'c',lease_seconds=30)
            assert await admin.fetchval('SELECT count(*) FROM conversation_event_logs')==0
            raise RuntimeError('rollback preparation')
    assert await admin.fetchval('SELECT count(*) FROM conversation_event_logs')==0
    manager=EventLogManager(sessions,MemoryLog(sessions))
    candidate=await manager._prepare('c')
    ref=await manager.log.initialize('c',candidate.generation)
    with pytest.raises(RuntimeError,match='rollback activation'):
        async with sessions.begin() as db:
            assert await event_logs.activate(db,candidate,(ref.conversation_id,ref.generation,ref.incarnation))
            assert await admin.fetchval('SELECT status FROM conversation_event_logs')=='initializing'
            raise RuntimeError('rollback activation')
    assert not await manager.is_current(ref)
    assert await manager._activate(candidate,ref)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', [
    'missing', 'deleted_without_log', 'deleted_with_log', 'uninitialized',
    'initializing_live', 'initializing_expired', 'active',
])
async def test_repository_read_preserves_visibility_and_state_with_one_query(database, case):
    from sqlalchemy import event
    from workmate_product.persistence.repositories import event_logs

    sessions, admin = database
    expected = None
    if case in {'deleted_with_log', 'initializing_live', 'initializing_expired', 'active'}:
        async with sessions.begin() as db:
            prepared = await event_logs.prepare(db, 'c', lease_seconds=300)
        live = case != 'initializing_expired'
        status = 'initializing'
        incarnation = None
        if case == 'initializing_expired':
            await admin.execute(
                "UPDATE conversation_event_logs SET lease_until=clock_timestamp()-interval '1 second'"
            )
        elif case in {'active', 'deleted_with_log'}:
            incarnation = str(uuid.uuid4())
            async with sessions.begin() as db:
                assert await event_logs.activate(db, prepared, ('c', prepared.generation, incarnation))
            status, live = 'active', False
        expected = event_logs.LogState('c', prepared.generation, prepared.version,
                                       status, prepared.token, incarnation, live)
    if case.startswith('deleted_'):
        await admin.execute('UPDATE conversations SET deleted_at=clock_timestamp()')

    statements = []
    engine = sessions.kw['bind'].sync_engine

    def record_statement(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    async with sessions.begin() as db:
        transaction = db.get_transaction()
        event.listen(engine, 'before_cursor_execute', record_statement)
        try:
            if case == 'missing' or case.startswith('deleted_'):
                with pytest.raises(LogUnavailable, match='Conversation is unavailable'):
                    await event_logs.read(db, 'missing' if case == 'missing' else 'c')
            else:
                assert await event_logs.read(db, 'c') == expected
            assert db.get_transaction() is transaction and transaction.is_active
        finally:
            event.remove(engine, 'before_cursor_execute', record_statement)
    assert len(statements) == 1, statements
