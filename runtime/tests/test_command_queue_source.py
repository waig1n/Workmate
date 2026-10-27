import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from workmate_product.coordination.command_queue import CommandEnvelope, QueueMessage, QueueRoute
from workmate_runtime.execution.command_queue import CommandQueueSource


class Queue:
    def __init__(self):
        self.messages = {}
        self.acks = []
        self.reads = []
    async def ensure_group(self, route): pass
    async def read(self, route, consumer, *, count):
        self.reads.append((route,count))
        data=self.messages.get(route,[])[:count]
        self.messages[route]=self.messages.get(route,[])[count:]
        return data
    async def pending(self, route, **kwargs): return []
    async def ack(self, route, ids): self.acks.extend(ids)
    def stream_key(self, route): return str(route)


class Repo:
    def __init__(self):
        self.action='execute'
        self.quarantines=[]
    async def classify(self, db, envelope, **kwargs):
        return SimpleNamespace(action=self.action,reason='test',command=dict(id=envelope.command_id,
            session_id='session',task_id='task',kind='StartTask',session_owner_id=None,
            session_active_task_id=None,session_status='accepted',delivery_channel='execute'))
    async def quarantine_message(self, db, **kwargs): self.quarantines.append(kwargs)
    async def quarantine_abandoned(self, *args): return False


@asynccontextmanager
async def sessions():
    class DB:
        async def execute(self, statement): pass
        @asynccontextmanager
        async def begin(self): yield
    yield DB()


async def test_message_remains_pending_until_durable_settlement():
    queue=Queue(); repo=Repo()
    source=CommandQueueSource(queue,sessions)
    source.repository=repo
    route=QueueRoute('node')
    queue.messages[route]=[QueueMessage('1-0',CommandEnvelope(command_id='c',delivery_generation=1,storage_node_id='node'))]
    commands=await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=1)
    assert [c['id'] for c in commands]==['c']
    await source.settle()
    assert queue.acks==[]
    repo.action='wait'
    await source.settle()
    assert queue.acks==[]
    repo.action='ack'
    await source.settle()
    assert queue.acks==['1-0']


async def test_invalid_envelope_is_durably_quarantined_before_ack():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    queue.messages[QueueRoute('node',control=True)]=[QueueMessage('2-0',None,'invalid_envelope')]
    assert await source.poll(worker_id='w',node_id='node',capacity=0,control_capacity=1)==[]
    assert repo.quarantines and queue.acks==['2-0']


async def test_owned_control_only_enters_owner_inbox():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    async def classify(*args,**kwargs):
        return SimpleNamespace(action='control',reason='test',command=dict(id='stop',session_id='s',
            task_id='t',kind='StopTask',session_owner_id='w',session_active_task_id='t',session_status='running',delivery_channel='control'))
    repo.classify=classify
    queue.messages[QueueRoute('node','w',True)]=[QueueMessage('3-0',CommandEnvelope(command_id='stop',delivery_generation=1,storage_node_id='node',target_incarnation='w'))]
    assert await source.poll(worker_id='w',node_id='node',capacity=0,control_capacity=1)==[]
    assert source.control_ids('s')==('stop',)
    assert queue.acks==[]


from test_postgres_execution_store import database


async def test_real_durable_start_executes_once_and_acks_only_boundary(database):
    import os
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from workmate_product.persistence.repositories import command_deliveries
    from workmate_runtime.execution.service import WorkerService
    from workmate_runtime.execution.controller import ExecutionController
    from test_execution_controller import prepare_reply
    from test_worker_service import bindings, wait_until
    from agentscope.message import TextBlock
    pool,db=database
    schema=await db.fetchval('SELECT current_schema()')
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':schema}})
    factory=async_sessionmaker(engine)
    queue=Queue()
    async with factory() as session,session.begin():
        await command_deliveries.stage(session,'command')
    message=QueueMessage('1-0',CommandEnvelope(command_id='command',delivery_generation=1,storage_node_id='node'))
    queue.messages[QueueRoute('node')]=[message,message]
    source=CommandQueueSource(queue,factory)
    start,calls=prepare_reply([[TextBlock(text='streamed once')]])
    worker=WorkerService(pool,'node',bindings(start),controller=ExecutionController(pool),command_source=source)
    shutdown=asyncio.Event()
    task=asyncio.create_task(worker.serve(shutdown))
    try:
        async def acked(): return bool(queue.acks)
        await wait_until(acked)
        assert len(calls)==1
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='command'")=='completed'
        assert await db.fetchval("SELECT status FROM command_deliveries WHERE command_id='command'")=='settled'
    finally:
        shutdown.set()
        await asyncio.wait_for(task,5)
        await engine.dispose()


@pytest.mark.parametrize('action,claimed', [('wait',False),('execute',True),('ack',True)])
async def test_pending_recovery_checks_pg_before_reclaim(action,claimed):
    from workmate_product.coordination.command_queue import PendingMessage
    queue=Queue();repo=Repo();repo.action=action
    source=CommandQueueSource(queue,sessions,pending_idle_ms=1);source.repository=repo
    route=QueueRoute('node')
    message=QueueMessage('4-0',CommandEnvelope(command_id='c',delivery_generation=1,storage_node_id='node'))
    recovered=[]
    async def pending(current,**kwargs):
        return [PendingMessage('4-0','old',100,1)] if current==route else []
    async def peek(*args): return message
    async def claim(*args,**kwargs): recovered.append(args);return [message]
    queue.pending=pending;queue.peek=peek;queue.claim_pending=claim
    await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=0)
    assert bool(recovered)==claimed
    assert bool(queue.acks)==(action=='ack')


async def test_quarantine_failure_never_acks_message():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    queue.messages[QueueRoute('node',control=True)]=[QueueMessage('5-0',None,'invalid_envelope')]
    async def failed(*args,**kwargs): raise ConnectionError('database down')
    repo.quarantine_message=failed
    with pytest.raises(ConnectionError):
        await source.poll(worker_id='w',node_id='node',capacity=0,control_capacity=1)
    assert not queue.acks


async def test_channels_share_one_bounded_budget_and_existing_controls_do_not_overprefetch():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    async def classify(*args,**kwargs):
        return SimpleNamespace(action='control',reason='test',command=dict(id='stop',session_id='s',task_id='t',
            kind='StopTask',session_owner_id='w',session_active_task_id='t',session_status='running',delivery_channel='control'))
    repo.classify=classify
    queue.messages[QueueRoute('node','w',True)]=[QueueMessage('6-0',CommandEnvelope(command_id='stop',delivery_generation=1,storage_node_id='node',target_incarnation='w'))]
    await source.poll(worker_id='w',node_id='node',capacity=0,control_capacity=1)
    reads=len(queue.reads)
    await source.poll(worker_id='w',node_id='node',capacity=0,control_capacity=1)
    assert len(queue.reads)==reads


async def test_real_owner_stop_is_consumed_with_full_inference_capacity(database):
    import os
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from workmate_product.persistence.repositories import command_deliveries
    from workmate_runtime.execution.service import WorkerService
    from workmate_runtime.execution.running import RunningExecutionController
    from test_execution_controller import prepare_reply
    from test_worker_service import bindings, wait_until
    from test_running_stop import stop_intent, quiescent
    pool,db=database
    schema=await db.fetchval('SELECT current_schema()')
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':schema}})
    factory=async_sessionmaker(engine);queue=Queue();source=CommandQueueSource(queue,factory)
    async with factory() as session,session.begin(): await command_deliveries.stage(session,'command')
    queue.messages[QueueRoute('node')]=[QueueMessage('1-0',CommandEnvelope(command_id='command',delivery_generation=1,storage_node_id='node'))]
    entered=asyncio.Event();start,calls=prepare_reply([],entered=entered)
    worker=WorkerService(pool,'node',bindings(start),capacity=1,
        controller=RunningExecutionController(pool,verify_cleanup=quiescent),command_source=source)
    shutdown=asyncio.Event();task=asyncio.create_task(worker.serve(shutdown))
    try:
        await asyncio.wait_for(entered.wait(),5)
        assert worker.available_capacity==0
        await stop_intent(db)
        await asyncio.sleep(.2)
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='accepted'
        async with factory() as session,session.begin(): await command_deliveries.stage(session,'stop')
        queue.messages[QueueRoute('node',worker.worker_id,True)]=[QueueMessage('2-0',CommandEnvelope(
            command_id='stop',delivery_generation=1,storage_node_id='node',target_incarnation=worker.worker_id))]
        async def acked(): return '2-0' in queue.acks
        await wait_until(acked)
        assert await db.fetchval("SELECT status FROM runtime_commands WHERE id='stop'")=='completed'
        assert len(calls)==1 and not worker.failures
    finally:
        shutdown.set();await asyncio.wait_for(task,5);await engine.dispose()


async def test_real_claimed_dead_owner_is_quarantined_not_replayed(database):
    import os
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from workmate_product.persistence.repositories import command_deliveries
    from test_postgres_execution_store import claim
    pool,db=database
    schema=await db.fetchval('SELECT current_schema()')
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':schema}})
    factory=async_sessionmaker(engine);queue=Queue();source=CommandQueueSource(queue,factory)
    try:
        async with factory() as session,session.begin(): await command_deliveries.stage(session,'command')
        await claim(pool)
        await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        queue.messages[QueueRoute('node')]=[QueueMessage('7-0',CommandEnvelope(command_id='command',delivery_generation=1,storage_node_id='node'))]
        assert await source.poll(worker_id='new',node_id='node',capacity=1,control_capacity=0)==[]
        assert queue.acks==['7-0']
        assert await db.fetchval('SELECT status FROM command_deliveries')=='quarantined'
        assert await db.fetchval('SELECT status FROM runtime_commands')=='claimed'
        assert await db.fetchval('SELECT active_task_id FROM runtime_sessions')=='task'
    finally: await engine.dispose()


async def test_claimed_duplicate_flood_cannot_grow_local_pending_without_bound():
    queue=Queue();repo=Repo();repo.action='wait'
    source=CommandQueueSource(queue,sessions);source.repository=repo
    queue.messages[QueueRoute('node')]=[QueueMessage(f'{i}-0',CommandEnvelope(
        command_id='c',delivery_generation=1,storage_node_id='node')) for i in range(1,21)]
    for _ in range(5):
        await source.poll(worker_id='w',node_id='node',capacity=2,control_capacity=0)
    assert len(source._held)==2


async def test_real_redis_pel_keeps_live_owner_and_quarantines_expired_execution(database):
    import os
    import uuid
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from workmate_product.coordination.command_queue import RedisCommandQueue
    from workmate_product.persistence.repositories import command_deliveries
    from test_postgres_execution_store import claim
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url: pytest.skip('requires isolated local Redis')
    pool,db=database
    schema=await db.fetchval('SELECT current_schema()')
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':schema}})
    factory=async_sessionmaker(engine);client=Redis.from_url(url)
    queue=RedisCommandQueue(client,namespace='test-'+uuid.uuid4().hex)
    route=QueueRoute('node')
    try:
        async with factory() as session,session.begin(): await command_deliveries.stage(session,'command')
        await queue.ensure_group(route)
        mid=await queue.publish(route,CommandEnvelope(command_id='command',delivery_generation=1,storage_node_id='node'))
        assert len(await queue.read(route,'old',count=1))==1
        await claim(pool)
        await asyncio.sleep(.01)
        source=CommandQueueSource(queue,factory,pending_idle_ms=1)
        assert await source.poll(worker_id='new',node_id='node',capacity=1,control_capacity=0)==[]
        pending=await queue.pending(route)
        assert len(pending)==1 and pending[0].consumer=='old' and pending[0].message_id==mid
        await db.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
        source=CommandQueueSource(queue,factory,pending_idle_ms=1)
        assert await source.poll(worker_id='new',node_id='node',capacity=1,control_capacity=0)==[]
        assert await queue.pending(route)==[]
        assert await db.fetchval('SELECT status FROM command_deliveries')=='quarantined'
        assert await db.fetchval('SELECT status FROM runtime_commands')=='claimed'
    finally:
        keys=[key async for key in client.scan_iter(match=f'workmate:{queue.namespace}:*')]
        if keys: await client.delete(*keys)
        await client.aclose();await engine.dispose()


async def test_lost_ack_is_retried_without_reexecution():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    route=QueueRoute('node');queue.messages[route]=[QueueMessage('8-0',CommandEnvelope(
        command_id='c',delivery_generation=1,storage_node_id='node'))]
    await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=0)
    repo.action='ack';original=queue.ack
    async def unavailable(*args): raise ConnectionError('redis unavailable after durable settlement')
    queue.ack=unavailable
    with pytest.raises(ConnectionError): await source.settle()
    assert len(source._held)==1
    queue.ack=original
    await source.settle()
    assert not source._held and queue.acks==['8-0']


async def test_rejected_claim_retries_durable_isolation_before_ack():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    queue.messages[QueueRoute('node')]=[QueueMessage('9-0',CommandEnvelope(
        command_id='c',delivery_generation=1,storage_node_id='node'))]
    await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=0)
    async def unavailable(*args): raise ConnectionError('PG temporarily down')
    repo.quarantine_unclaimed=unavailable
    with pytest.raises(ConnectionError): await source.rejected('c',1)
    assert not queue.acks and len(source._held)==1
    async def persist(*args): repo.action='ack';return True
    repo.quarantine_unclaimed=persist
    await source.settle()
    assert queue.acks==['9-0'] and not source._held


async def test_rejection_racing_other_worker_claim_does_not_ack_or_replay():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    queue.messages[QueueRoute('node')]=[QueueMessage('10-0',CommandEnvelope(
        command_id='c',delivery_generation=1,storage_node_id='node'))]
    await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=0)
    async def claimed(*args): repo.action='wait';return False
    repo.quarantine_unclaimed=claimed
    await source.rejected('c',1)
    assert not queue.acks and len(source._held)==1 and not source._commands


@pytest.mark.parametrize('action,quarantined',[('execute',True),('wait',True),('ack',False)])
async def test_wrong_channel_cannot_bypass_budget_but_settled_duplicates_can_ack(action,quarantined):
    queue=Queue();repo=Repo();repo.action=action
    source=CommandQueueSource(queue,sessions);source.repository=repo
    queue.messages[QueueRoute('node',control=True)]=[QueueMessage('11-0',CommandEnvelope(
        command_id='c',delivery_generation=1,storage_node_id='node'))]
    assert await source.poll(worker_id='w',node_id='node',capacity=0,control_capacity=1)==[]
    assert bool(repo.quarantines)==quarantined
    assert queue.acks==['11-0']


async def test_delayed_old_rejection_cannot_isolate_a_new_redrive_generation():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    route=QueueRoute('node')
    queue.messages[route]=[QueueMessage('12-0',CommandEnvelope(command_id='c',delivery_generation=1,storage_node_id='node'))]
    candidates=await source.poll(worker_id='w',node_id='node',capacity=2,control_capacity=0)
    observed=candidates[0]['delivery_generation']
    # Controller is awaiting its claim result while another consumer settles old
    # routing and an authorized operator creates a fresh delivery intent.
    repo.action='ack';await source.settle();repo.action='execute'
    queue.messages[route]=[QueueMessage('13-0',CommandEnvelope(command_id='c',delivery_generation=2,storage_node_id='node'))]
    assert (await source.poll(worker_id='w',node_id='node',capacity=2,control_capacity=0))[0]['delivery_generation']==2
    isolations=[]
    async def isolate(db,command_id,generation): isolations.append(generation);return True
    repo.quarantine_unclaimed=isolate
    await source.rejected('c',observed)
    await source.settle()
    assert not isolations and queue.acks==['12-0']
    assert source.control_generation('c')==2
    assert len(source._held)==1 and not source._rejected


async def test_stale_classification_cannot_recreate_an_acked_local_candidate():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    route=QueueRoute('node');message=QueueMessage('14-0',CommandEnvelope(command_id='c',delivery_generation=1,storage_node_id='node'))
    queue.messages[route]=[message]
    await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=0)
    entered=asyncio.Event();released=asyncio.Event();original=repo.classify
    async def delayed(*args,**kwargs):
        result=await original(*args,**kwargs)
        entered.set();await released.wait()
        return result
    repo.classify=delayed
    stale=asyncio.create_task(source._resolve(route,message));await entered.wait()
    repo.classify=original;repo.action='ack'
    await source._resolve(route,message)
    released.set();await stale
    assert not source._held and not source._commands


async def test_cold_resume_keeps_execution_delivery_after_claim_changes_owner():
    queue=Queue();repo=Repo();source=CommandQueueSource(queue,sessions);source.repository=repo
    route=QueueRoute('node')
    queue.messages[route]=[QueueMessage('15-0',CommandEnvelope(
        command_id='resume',delivery_generation=1,storage_node_id='node'))]
    async def classify(*args,**kwargs):
        return SimpleNamespace(action=repo.action,reason='cold_resume',command=dict(
            id='resume',session_id='s',task_id='t',kind='ResumeInteraction',
            session_owner_id=None if repo.action=='execute' else 'w',
            session_active_task_id='t',session_status='running',delivery_channel='execute'))
    repo.classify=classify
    assert [c['id'] for c in await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=1)]==['resume']
    repo.action='wait'  # Execution claimed: session owner now exists, delivery channel stays execute.
    await source.settle()
    assert not repo.quarantines
    assert not queue.acks and len(source._held)==1
    repo.action='ack'
    await source.settle()
    assert queue.acks==['15-0'] and not source._held


@pytest.mark.parametrize('wrong_channel',[False,True])
async def test_real_cold_resume_retains_delivery_channel_and_rejects_wrong_stream(database,wrong_channel):
    import os,uuid
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from workmate_product.coordination.command_queue import RedisCommandQueue
    from workmate_product.persistence.repositories import command_deliveries
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url:pytest.skip('requires isolated local Redis')
    _,admin=database;schema=await admin.fetchval('SELECT current_schema()')
    engine=create_async_engine(os.environ['WORKMATE_TEST_POSTGRES_DSN'].replace('postgresql://','postgresql+asyncpg://'),
        connect_args={'server_settings':{'search_path':schema}})
    factory=async_sessionmaker(engine);client=Redis.from_url(url)
    queue=RedisCommandQueue(client,namespace='test-cold-resume-'+uuid.uuid4().hex)
    route=QueueRoute('node',control=wrong_channel);source=CommandQueueSource(queue,factory)
    try:
        await admin.execute("UPDATE runtime_commands SET kind='ResumeInteraction'")
        async with factory.begin() as db:await command_deliveries.stage(db,'command')
        await queue.ensure_group(route)
        await queue.publish(route,CommandEnvelope(command_id='command',delivery_generation=1,storage_node_id='node'))
        commands=await source.poll(worker_id='w',node_id='node',capacity=1,control_capacity=1)
        if wrong_channel:
            assert commands==[] and await queue.pending(route)==[]
            assert await admin.fetchval('SELECT error_code FROM command_delivery_quarantines')=='channel_mismatch'
            assert await admin.fetchval('SELECT status FROM runtime_commands')=='accepted'
            assert await admin.fetchval('SELECT owner_id FROM runtime_sessions') is None
            return
        assert [c['id'] for c in commands]==['command']
        assert len(await queue.pending(route))==1
        await admin.execute("""UPDATE runtime_commands SET status='claimed',claimed_by='w',claimed_epoch=1;
            UPDATE runtime_sessions SET owner_id='w',execution_epoch=1,status='running',active_task_id='task',
                lease_until=clock_timestamp()+interval '30 seconds'""")
        await source.settle()
        assert await admin.fetchval('SELECT count(*) FROM command_delivery_quarantines')==0
        assert len(await queue.pending(route))==1
        await admin.execute("UPDATE runtime_commands SET status='completed'")
        await source.settle()
        assert await queue.pending(route)==[]
        assert await admin.fetchval('SELECT status FROM command_deliveries')=='settled'
        assert await admin.fetchval('SELECT count(*) FROM command_delivery_quarantines')==0
    finally:
        keys=[key async for key in client.scan_iter(match=f'workmate:{queue.namespace}:*')]
        if keys:await client.delete(*keys)
        await client.aclose();await engine.dispose()
