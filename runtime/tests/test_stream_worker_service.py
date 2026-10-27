"""Streams drive scheduling; saturated execution never starves control intake."""
import asyncio
from types import SimpleNamespace
import pytest
from workmate_runtime.execution.service import WorkerService


class Source:
    def __init__(self):
        self.polls = []
        self.messages = []
        self.settles = 0

    async def poll(self, *, worker_id, node_id, capacity, control_capacity):
        self.polls.append((capacity, control_capacity))
        result, self.messages = self.messages, []
        return result

    async def settle(self):
        self.settles += 1


async def test_stream_source_is_required_no_implicit_pg_discovery():
    worker = WorkerService(None, 'node', None, controller=None)
    with pytest.raises(ValueError, match='command source'):
        await worker.serve(asyncio.Event())


async def test_control_intake_survives_full_execution_capacity():
    source = Source()
    source.messages = [dict(id='stop', task_id='task', session_id='s', kind='StopTask', stop_phase='unclaimed',delivery_generation=1)]
    stop = asyncio.Event()
    worker = WorkerService(None, 'node', None, controller=None, command_source=source, capacity=1)
    await worker._slots.acquire()
    async def inspect():
        return ()
    async def execute(*args, **kwargs):
        assert args[2] == 'StopTask'
        stop.set()
    worker._inspect = inspect
    worker._session = execute
    await asyncio.wait_for(worker.serve(stop), 2)
    assert source.polls[0] == (0, 100)
    assert source.settles > 0


async def test_failed_queue_does_not_fallback_to_database_execution():
    source = Source()
    stop = asyncio.Event()
    async def unavailable(**kwargs):
        stop.set()
        raise ConnectionError('secret redis URL')
    source.poll = unavailable
    worker = WorkerService(None, 'node', None, controller=None, command_source=source)
    async def inspect():
        return ()
    worker._inspect = inspect
    await asyncio.wait_for(worker.serve(stop), 2)
    assert not worker.active_sessions


async def test_shutdown_cancels_blocked_queue_settlement():
    source=Source();entered=asyncio.Event();exited=asyncio.Event()
    async def blocked():
        entered.set()
        try: await asyncio.Event().wait()
        finally: exited.set()
    source.settle=blocked
    worker=WorkerService(None,'node',None,controller=None,command_source=source)
    async def inspect(): return ()
    worker._inspect=inspect
    shutdown=asyncio.Event();task=asyncio.create_task(worker.serve(shutdown))
    await entered.wait();shutdown.set()
    await asyncio.wait_for(task,1)
    assert exited.is_set() and not worker.ready.is_set()


@pytest.mark.parametrize('failure',['connection','timeout'])
async def test_redis_client_errors_retry_transport_without_database_fallback(failure):
    from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
    source=Source();shutdown=asyncio.Event();calls=[]
    async def poll(**kwargs):
        calls.append(kwargs)
        if len(calls)==1:
            raise (RedisConnectionError if failure=='connection' else RedisTimeoutError)('private redis URL')
        shutdown.set();return []
    source.poll=poll
    worker=WorkerService(None,'node',None,controller=None,command_source=source)
    async def inspect(): return ()
    worker._inspect=inspect
    await asyncio.wait_for(worker.serve(shutdown),2)
    assert len(calls)==2 and not worker.active_sessions


@pytest.mark.parametrize('database_unavailable',[False,True])
async def test_declined_claim_is_durably_disposed_without_quarantining_unowned_session(database_unavailable):
    from workmate_runtime.execution.service import WorkerBindings
    source=Source();rejected=[]
    async def decline(*args): return None
    async def dispose(command_id,generation):
        assert generation == 1
        rejected.append(command_id)
        if database_unavailable: raise ConnectionError('DB failure; source will retry')
    source.rejected=dispose
    worker=WorkerService(None,'node',WorkerBindings(decline,decline,decline,decline,decline),
        controller=SimpleNamespace(start=decline),capacity=1,command_source=source)
    await worker._slots.acquire()
    await worker._session('cmd','session',delivery_generation=1)
    assert rejected==['cmd'] and worker.available_capacity==1
    assert not worker.failures and not worker._unclaimed_commands


async def test_worker_rejection_keeps_generation_captured_before_claim_await():
    from workmate_runtime.execution.service import WorkerBindings
    entered=asyncio.Event();released=asyncio.Event();attempts=[]
    source=Source()
    async def decline(*args):
        entered.set();await released.wait();return None
    async def reject(command,generation): attempts.append((command,generation))
    source.rejected=reject
    worker=WorkerService(None,'node',WorkerBindings(decline,decline,decline,decline,decline),
        controller=SimpleNamespace(start=decline),command_source=source,capacity=1)
    await worker._slots.acquire()
    execution=asyncio.create_task(worker._session('c','s',delivery_generation=1))
    await entered.wait()
    source.control_generation=lambda _: 2
    released.set();await execution
    assert attempts==[('c',1)]


async def test_supervisor_rejection_keeps_generation_captured_before_resume_await():
    from workmate_runtime.execution.supervisor import ParkedSupervisor,ReconciliationRequired
    source=SimpleNamespace(generation=1,attempts=[])
    source.control_generation=lambda _: source.generation
    async def rejected(command,generation): source.attempts.append((command,generation))
    source.rejected=rejected
    async def declined(*args,**kwargs):
        source.generation=2
        return None
    supervisor=ParkedSupervisor(None,execution_controller=SimpleNamespace(resume=declined),command_source=source)
    supervisor.interval=.01
    async def inspect(*args):
        return dict(id='resume',kind='ResumeInteraction') if source.generation==1 else None
    supervisor._inspect=inspect
    owner=SimpleNamespace(worker_id='w')
    with pytest.raises(ReconciliationRequired):
        await supervisor._drive_controls(owner,'node',asyncio.Semaphore(1),None,None,None,None,None)
    assert source.attempts==[('resume',1)]
