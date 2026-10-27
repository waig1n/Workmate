"""Worker hints and typed release handling preserve authoritative PG gates."""
import asyncio
import os
from types import SimpleNamespace
import uuid
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from test_execution_context import database,claim
from test_workspace_residencies import idle
from test_workspace_residency_coordinator import IDENTITY,OWNER
from workmate_runtime.execution.workspace_residency import WorkspaceResidencyCoordinator
from workmate_runtime.execution.workspace_lifecycle import WorkerWorkspaceLifecycle
from workmate_product.coordination.worker_registry import RedisWorkerRegistry
from workmate_product.coordination.workspace_release_queue import RedisWorkspaceReleaseQueue,WorkspaceReleaseEnvelope
from workmate_product.coordination.command_queue import QueueRoute


@pytest_asyncio.fixture
async def lifecycle(database):
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url: pytest.skip('requires isolated local Redis')
    sessions,admin=database
    client=Redis.from_url(url)
    namespace='lifecycle-'+uuid.uuid4().hex
    hints=RedisWorkerRegistry(client,namespace=namespace)
    queue=RedisWorkspaceReleaseQueue(client,namespace=namespace)
    await queue.ensure_group(QueueRoute('node','worker',True))
    coordinator=WorkspaceResidencyCoordinator(sessions,'node',registry=hints,release_queue=queue)
    await claim(admin)
    hold=await coordinator.record_held(IDENTITY,OWNER,'generation')
    closed=[]
    class Spaces:
        def affinity_holds(self): return (hold,) if not closed else ()
        async def release_idle_session(self,sid,version,generation):
            assert (sid,version,generation)==('s',1,'generation')
            release=await coordinator.begin_idle_release(IDENTITY,hold)
            if not release: return False
            closed.append(release)
            await coordinator.finish_idle_release(release)
            return True
    service=SimpleNamespace(worker_id='worker',node_id='node',available_capacity=0,
        lease_seconds=30,failures={})
    controller=WorkerWorkspaceLifecycle(sessions,service,Spaces(),hints,queue,capacity=4)
    try:
        yield controller,coordinator,hold,closed,hints,queue,sessions,admin
    finally:
        keys=[key async for key in client.scan_iter(match=f'workmate:{namespace}:*')]
        if keys: await client.delete(*keys)
        await client.aclose()


async def test_busy_holder_publishes_affinity_but_draining_withdraws(lifecycle):
    loop,_,_,_,hints,_,_,_=lifecycle
    await loop.heartbeat(draining=False)
    assert (await hints.worker('worker')).available_capacity==0
    assert await hints.affinity('s','node') is not None
    await loop.heartbeat(draining=True)
    assert await hints.worker('worker') is None
    assert await hints.worker('worker',allow_draining=True) is not None
    assert await hints.affinity('s','node') is None


async def test_busy_release_remains_pending_then_idle_closes_and_acks(lifecycle):
    loop,coordinator,hold,closed,_,queue,_,admin=lifecycle
    await coordinator.request_idle_release(hold)
    await loop.release_tick()
    route=QueueRoute('node','worker',True)
    assert await queue.pending(route)
    assert not closed
    await idle(admin)
    await loop.release_tick()
    assert len(closed)==1
    assert not await queue.pending(route)
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='released'


async def test_missing_notice_is_repaired_from_exact_durable_request(lifecycle):
    loop,coordinator,hold,closed,_,queue,_,admin=lifecycle
    coordinator.release_queue=None
    await coordinator.request_idle_release(hold)
    await idle(admin)
    await loop.repair_releases()
    assert len(closed)==1
    assert await admin.fetchval('SELECT status FROM workspace_release_requests')=='released'
    assert not await queue.pending(QueueRoute('node','worker',True))


async def test_malformed_or_inexact_notice_is_quarantined_before_ack(lifecycle):
    loop,coordinator,hold,closed,_,queue,_,admin=lifecycle
    request=await coordinator.request_idle_release(hold)
    route=QueueRoute('node','worker',True)
    await queue.publish(route,WorkspaceReleaseEnvelope(request_id=request['id'],session_id='s',
        storage_node_id='node',target_incarnation='worker',workspace_generation='generation',expected_hold_version=2))
    await queue.client.xadd(queue.stream_key(route),{'payload':'secret malformed content'})
    await loop.release_tick()
    assert not closed
    assert await admin.fetchval('SELECT count(*) FROM command_delivery_quarantines')==2
    assert len(await queue.pending(route))==1


async def test_failed_lifecycle_supervision_stops_worker_admission(monkeypatch):
    from workmate_runtime.execution.composition import WorkerApplication
    joined=asyncio.Event()
    closed=asyncio.Event()
    async def serving(stop):
        try: await asyncio.Event().wait()
        finally: joined.set()
    async def failed(*_): raise ValueError('lifecycle unavailable')
    async def background(stop): await stop.wait()
    async def recovery(): return {}
    async def close(): closed.set()
    monkeypatch.setattr(WorkerApplication,'_sweep',lambda self,stop:background(stop))
    app=WorkerApplication(SimpleNamespace(serve=serving,failures={}),None,None,
        SimpleNamespace(resource_registry=SimpleNamespace(_scopes={}),close=close),None,
        SimpleNamespace(recover_pending=recovery),SimpleNamespace(serve=background),
        workspace_lifecycle=SimpleNamespace(serve=failed))
    running=asyncio.create_task(app.serve(asyncio.Event()))
    try:
        done,_=await asyncio.wait({running},timeout=.5)
        assert running in done,'Worker admission must observe a failed lifecycle loop'
        with pytest.raises(ValueError,match='lifecycle unavailable'):
            await running
    finally:
        running.cancel()
        await asyncio.gather(running,return_exceptions=True)
    assert joined.is_set() and closed.is_set()


async def test_slow_release_does_not_block_health_or_stop(lifecycle,monkeypatch):
    loop,_,_,_,_,_,_,_=lifecycle
    heartbeats=[]
    entered=asyncio.Event()
    async def heartbeat(**kwargs): heartbeats.append(kwargs)
    async def slow():
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(loop,'heartbeat',heartbeat)
    monkeypatch.setattr(loop,'release_tick',slow)
    loop.ttl=3
    stop=asyncio.Event()
    draining=asyncio.Event()
    running=asyncio.create_task(loop.serve(stop,draining))
    try:
        await asyncio.wait_for(entered.wait(),1)
        draining.set()
        async with asyncio.timeout(2):
            while not any(row['draining'] for row in heartbeats):
                await asyncio.sleep(.01)
        stop.set()
        done,_=await asyncio.wait({running},timeout=1)
        assert running in done
        await running
    finally:
        running.cancel()
        await asyncio.gather(running,return_exceptions=True)


async def test_confirmed_blocked_close_only_isolates_its_session(lifecycle,monkeypatch):
    loop,coordinator,hold,_,hints,queue,_,admin=lifecycle
    async def failed(sid,version,generation):
        release=await coordinator.begin_idle_release(IDENTITY,hold)
        await coordinator.block_idle_release(release,RuntimeError())
        raise RuntimeError('controlled close failure')
    monkeypatch.setattr(loop.workspaces,'release_idle_session',failed)
    await idle(admin)
    await coordinator.request_idle_release(hold)
    await loop.release_tick()
    assert loop.service.failures=={'s':'RuntimeError'}
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='blocked'
    assert not await queue.pending(QueueRoute('node','worker',True))
    await loop.heartbeat(draining=False)
    assert await hints.worker('worker') is not None


async def test_registry_loss_rebuilds_the_same_persisted_holding(lifecycle):
    loop,_,hold,_,hints,_,_,admin=lifecycle
    await loop.heartbeat(draining=False)
    await hints.client.delete(hints.worker_key('worker'),hints.affinity_key('s'))
    assert await hints.worker('worker') is None
    await loop.heartbeat(draining=False)
    hint=await hints.affinity('s','node')
    assert hint.hold_version==hold['hold_version']
    assert await admin.fetchval('SELECT hold_version FROM workspace_residencies')==hold['hold_version']
