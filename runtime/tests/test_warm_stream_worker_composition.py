"""Local PG/Redis/Core acceptance for warm compute across Worker incarnations.

WORKMATE_TEST_REAL_SANDBOX=1 uses the real WorkerMCPWorkspace and attestation.
Otherwise only the OS Workspace is a fixture, not evidence of sandbox isolation.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import uuid

import pytest
import pytest_asyncio
from agentscope.message import TextBlock
from agentscope.model import ChatResponse
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from workmate_contracts import Command
from workmate_product.application.commands import accept_start
from workmate_product.application.execution_context import record_execution_spec
from workmate_product.config.command_queue import CommandQueuePolicy
from workmate_product.config.worker import WorkspaceLifecyclePolicy
from workmate_product.coordination.command_publisher import CommandPublisher
from workmate_product.coordination.command_queue import RedisCommandQueue
from workmate_product.persistence.models import Task,TaskMessage
from workmate_product.persistence.repositories.command_deliveries import stage
from workmate_runtime.config import RuntimeSettings
from workmate_runtime.execution.composition import compose_worker
from test_execution_context import database,record,MODEL
from test_core_engine import ScriptModel as BaseScriptModel
from test_worker_workspaces import StubWorkspace


class ScriptModel(BaseScriptModel):
    async def _call_api(self,*args,**kwargs):
        source=await super()._call_api(*args,**kwargs)
        async def chunks():
            async for chunk in source:
                yield ChatResponse(content=chunk.content,is_last=False)
                yield chunk
        return chunks()


async def wait_for(predicate,timeout=30):
    async with asyncio.timeout(timeout):
        while not await predicate(): await asyncio.sleep(.02)


class Harness:
    async def close(self,*,worker_timeout=20,service_timeout=10):
        failures=[]
        async def attempt(awaitable):
            try:
                return await awaitable
            except BaseException as error:
                failures.append(error)
                return []
        async def join(tasks,timeout):
            results=await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),timeout)
            failures.extend(result for result in results if isinstance(result,BaseException))
        for stop in self.stops: stop.set()
        try:
            await attempt(join(self.running.values(),worker_timeout))
        finally:
            self.global_stop.set()
            try:
                await attempt(join(self.services,service_timeout))
                for index,app in enumerate(self.workers):
                    if index not in self.running:
                        await attempt(app.workspaces.close())
                async def remove_keys():
                    keys=[key async for key in self.client.scan_iter(match=f'workmate:{self.queue.namespace}:*')]
                    if keys: await self.client.delete(*keys)
                await attempt(remove_keys())
            finally:
                try:
                    await attempt(self.realtime_client.aclose())
                finally:
                    await attempt(self.client.aclose())
        if failures:
            raise BaseExceptionGroup('Warm fixture cleanup failed',failures)
    async def start(self,index):
        if index not in self.running:
            self.running[index]=asyncio.create_task(self.workers[index].serve(self.stops[index]))
            await asyncio.wait_for(self.workers[index].service.ready.wait(),10)
    async def stop(self,index):
        self.stops[index].set()
        await asyncio.wait_for(self.running[index],15)
    async def finished(self,task_id):
        async def done():
            for app in self.workers: assert not app.service.failures,app.service.failures
            assert not self.product.failures,self.product.failures
            return (await self.admin.fetchval('SELECT status FROM tasks WHERE id=$1',task_id)
                in {'completed','cancelled'} and await self.admin.fetchval(
                    "SELECT c.active_product_task_id FROM conversations c "
                    "JOIN tasks t ON t.conversation_id=c.id WHERE t.id=$1",task_id) is None)
        await wait_for(done)
        async def idle(): return not any(app.service.active_sessions for app in self.workers)
        await wait_for(idle)
        await self.assert_realtime(task_id)
    async def assert_realtime(self,task_id):
        from workmate_contracts.realtime import RealtimeCursor
        async def published():
            for service in self.services:
                assert not service.done(), service.exception() if service.done() else None
            return not await self.admin.fetchval("SELECT count(*) FROM realtime_business_outbox WHERE status<>'published'")
        await wait_for(published)
        status=await self.admin.fetchval('SELECT status FROM tasks WHERE id=$1',task_id)
        if status=='completed':
            message=await self.admin.fetchrow("SELECT * FROM task_messages WHERE task_id=$1 AND role='assistant'",task_id)
            assert message is not None and message['snapshot_version']>0
            assert message['snapshot_phase']=='final' and not message['snapshot_incomplete']
            assert json.loads(message['content'])['text']=='warm reply'
            ref=await self.log_manager.ensure(message['conversation_id'])
            page=await self.log.read(ref,RealtimeCursor(conversation_id=ref.conversation_id,
                generation=ref.generation,stream_id='0-0'),count=512)
            assert any(entry.envelope.task_id==task_id and entry.envelope.text is not None
                and entry.envelope.payload.get('delta')=='warm reply' for entry in page.entries)
        runtime_types=await self.admin.fetch("SELECT payload->>'type' AS type FROM runtime_events")
        assert not any(row['type'].startswith(('TEXT_BLOCK_','THINKING_BLOCK_')) for row in runtime_types)
        product_types=await self.admin.fetch("SELECT payload->>'type' AS type FROM product_events")
        assert not any(row['type'].startswith(('TEXT_MESSAGE_','REASONING_MESSAGE_')) for row in product_types)
    async def next_task(self,number,profile='standard'):
        task_id=f't{number}';command_id=f'cmd{number}'
        epoch=await self.admin.fetchval("SELECT execution_epoch FROM runtime_sessions WHERE id='s'")
        async def allowed(*args): return True
        async with self.sessions.begin() as db:
            db.add(Task(id=task_id,conversation_id='c',title='Next',approval_profile=profile));await db.flush()
            db.add(TaskMessage(id=f'm{number}',task_id=task_id,conversation_id='c',role='user',content={'text':'next'}))
            await accept_start(db,Command(command_id=command_id,kind='StartTask',conversation_id='c',
                runtime_session_id='s',product_task_id=task_id,expected_epoch=epoch),
                actor_user_id='u',storage_node_id='node',authorize_agent=allowed)
            await record_execution_spec(db,task_id=task_id,session_id='s',actor_user_id='u',
                selected_model=MODEL,knowledge_enabled=False)
        return task_id,command_id
    async def hold(self):
        return dict(await self.admin.fetchrow("SELECT * FROM workspace_residencies WHERE session_id='s'"))


@pytest_asyncio.fixture
async def warm(database,tmp_path,monkeypatch):
    from app.services.durable_runtime import compose_product_runtime
    url=os.getenv('WORKMATE_TEST_REDIS_URL')
    if not url: pytest.skip('requires isolated local Redis')
    h=Harness();h.sessions,h.admin=database;h.prepared=[];h.running={}
    h.client=Redis.from_url(url);h.queue=RedisCommandQueue(h.client,namespace='warm-'+uuid.uuid4().hex)
    from workmate_product.config.realtime import RealtimePolicy
    from workmate_product.realtime.composition import create_realtime_log
    from workmate_product.realtime.log_manager import EventLogManager
    from workmate_product.realtime.business_publisher import BusinessPublisher
    h.realtime_client=Redis.from_url(url,decode_responses=True,retry=Retry(NoBackoff(),0),retry_on_timeout=False)
    h.log=create_realtime_log(h.realtime_client,namespace=h.queue.namespace,policy=RealtimePolicy())
    h.log_manager=EventLogManager(h.sessions,h.log)
    h.model=lambda *_:ScriptModel([[TextBlock(text='warm reply')]])
    monkeypatch.setattr('workmate_runtime.assembly.agent.build_selected_model',lambda *args:h.model(*args))
    factory=StubWorkspace
    if os.getenv('WORKMATE_TEST_REAL_SANDBOX')=='1':
        from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
        factory=WorkerMCPWorkspace
    else:
        async def no_attestation(*args): return None
        monkeypatch.setattr('workmate_runtime.assembly.sandbox_proof.attest_task_sandbox',no_attestation)
    settings=RuntimeSettings(reme_enabled=False,workspace_lifecycle=WorkspaceLifecyclePolicy(
        max_warm_per_worker=4,warm_ttl_seconds=3600,sweep_interval_seconds=60))
    h.workers=[compose_worker(h.sessions.kw['bind'],settings,storage_node_id='node',
        workspace_dir=tmp_path/'workspaces',artifact_dir=tmp_path/'artifacts',max_file_bytes=1024,
        max_files=3,workspace_factory=factory,capacity=1,command_queue=h.queue,realtime_log=h.log) for _ in range(2)]
    h.stops=[asyncio.Event(),asyncio.Event()]
    for index,app in enumerate(h.workers):
        original=app.service.bindings.prepare_start
        async def prepare(owner,*args,_original=original,_app=app,_index=index):
            result=await _original(owner,*args)
            bound=_app.assembler._bound[owner]
            h.prepared.append((_index,owner,bound.binding.workspace,bound.binding.backend,bound.policy))
            return result
        app.service.bindings=replace(app.service.bindings,prepare_start=prepare)
    await record(h.sessions)
    async with h.sessions.begin() as db: await stage(db,'cmd')
    h.product=compose_product_runtime(h.sessions,poll_interval=.01)
    publisher=CommandPublisher(h.sessions,h.queue,CommandQueuePolicy(poll_interval_seconds=.05))
    global_stop=h.global_stop=asyncio.Event()
    business=BusinessPublisher(h.sessions,h.log_manager,h.log,CommandQueuePolicy(poll_interval_seconds=.05))
    services=h.services=[asyncio.create_task(h.product.serve(global_stop)),
        asyncio.create_task(publisher.serve(global_stop)),asyncio.create_task(business.serve(global_stop))]
    original_failure=None
    try:
        yield h
    except BaseException as error:
        original_failure=error
        raise
    finally:
        try:
            await h.close()
        except BaseException as cleanup_error:
            if original_failure is not None:
                raise BaseExceptionGroup('Warm fixture and cleanup failed',
                    [original_failure,cleanup_error]) from None
            raise


async def test_warm_reuse_ignores_lost_or_wrong_affinity_then_peer_rebuilds(warm):
    h=warm;await h.start(0);await h.finished('t');first=h.prepared[0]
    hold=await h.hold();assert hold['status']=='held'
    keep=Path(first[2].host_workdir)/'working'/'keep.txt';keep.write_text('kept across holders')
    await h.start(1)
    hints=h.workers[0].workspace_lifecycle.registry
    await h.client.delete(hints.affinity_key('s'))
    await h.admin.execute("UPDATE agents SET tool_policy=$1::jsonb WHERE id='a'",
        json.dumps({"denied_tools":["fresh_tool"]}))
    task,_=await h.next_task(2,'strict');await h.finished(task)
    second=h.prepared[1]
    assert first[0]==second[0]==0 and first[2] is second[2] and first[3] is not second[3]
    assert 'fresh_tool' in second[4].denied_tools and second[4].approval_profile=='strict'
    assert (await h.hold())['hold_version']==hold['hold_version']
    from workmate_product.coordination.worker_registry import AffinityRecord
    await hints.publish_affinity(AffinityRecord(session_id='s',node_id='node',
        incarnation=h.workers[1].service.worker_id,workspace_generation='wrong',hold_version=hold['hold_version']+99))
    task,_=await h.next_task(3);await h.finished(task)
    assert h.prepared[2][0]==0
    assert await h.admin.fetchval("SELECT count(*) FROM command_deliveries WHERE status='quarantined'")==0
    # Capacity eviction closes the actual hold; retiring Worker 0 makes the peer
    # the only available new consumer, without forcing/rewriting command routes.
    await h.workers[0].workspaces.sweep_idle(ttl=0,capacity=0)
    assert (await h.hold())['status']=='released'
    await h.stop(0)
    task,_=await h.next_task(4);await h.finished(task)
    fourth=h.prepared[3];renewed=await h.hold()
    assert fourth[0]==1 and fourth[2] is not first[2]
    assert fourth[2].host_workdir==first[2].host_workdir and keep.read_text()=='kept across holders'
    assert renewed['hold_version']>hold['hold_version']
    assert renewed['workspace_generation']==hold['workspace_generation']


async def test_busy_warm_holder_queues_start_but_processes_stop_without_inference_slot(warm):
    from workmate_product.persistence.models import Conversation
    from workmate_product.application.control_commands import accept_stop
    h=warm;await h.start(0);await h.finished('t')
    entered=asyncio.Event();h.model=lambda *_:ScriptModel([],entered=entered)
    async def allowed(*args): return True
    async with h.sessions.begin() as db:
        db.add(Conversation(id='busy',user_id='u',agent_id='a',model_profile='chat'));await db.flush()
        db.add(Task(id='busy-task',conversation_id='busy',title='Busy',approval_profile='standard'));await db.flush()
        db.add(TaskMessage(id='busy-input',task_id='busy-task',conversation_id='busy',role='user',content={'text':'wait'}))
        await accept_start(db,Command(command_id='busy-start',kind='StartTask',conversation_id='busy',
            runtime_session_id='busy-session',product_task_id='busy-task',expected_epoch=0),
            actor_user_id='u',storage_node_id='node',authorize_agent=allowed)
        await record_execution_spec(db,task_id='busy-task',session_id='busy-session',actor_user_id='u',
            selected_model=MODEL,knowledge_enabled=False)
    await asyncio.wait_for(entered.wait(),30)
    assert h.workers[0].service.available_capacity==0
    await h.start(1)
    h.model=lambda *_:ScriptModel([[TextBlock(text='should remain unstarted')]])
    task,command=await h.next_task(2)
    async def published():
        return await h.admin.fetchval("SELECT status FROM command_deliveries WHERE command_id=$1",command)=='published'
    await wait_for(published)
    assert await h.admin.fetchval('SELECT status FROM runtime_commands WHERE id=$1',command)=='accepted'
    assert await h.admin.fetchval('SELECT target_incarnation FROM command_deliveries WHERE command_id=$1',command)==h.workers[0].service.worker_id
    async with h.sessions.begin() as db:
        await accept_stop(db,Command(command_id='queued-stop',kind='StopTask',conversation_id='c',
            runtime_session_id='s',product_task_id=task,expected_epoch=1),actor_user_id='u')
    async def cancelled(): return await h.admin.fetchval('SELECT status FROM tasks WHERE id=$1',task)=='cancelled'
    await wait_for(cancelled)
    assert await h.admin.fetchval("SELECT status FROM tasks WHERE id='busy-task'")=='running'
    assert h.workers[0].service.available_capacity==0
    assert not any(item[1].task_id==task for item in h.prepared)
    busy=await h.admin.fetchrow("SELECT * FROM runtime_sessions WHERE id='busy-session'")
    async with h.sessions.begin() as db:
        await accept_stop(db,Command(command_id='busy-stop',kind='StopTask',conversation_id='busy',
            runtime_session_id='busy-session',product_task_id='busy-task',expected_epoch=busy['execution_epoch'],
            reply_id=busy['reply_id']),actor_user_id='u')
    await h.finished('busy-task')
    assert await h.admin.fetchval("SELECT count(*) FROM command_deliveries WHERE status='quarantined'")==0


async def test_drain_releases_warm_residence_withdraws_hint_and_allows_peer(warm):
    h=warm;await h.start(0);await h.finished('t')
    first=await h.hold();lifecycle=h.workers[0].workspace_lifecycle
    await lifecycle.heartbeat(draining=False)
    assert await lifecycle.registry.affinity('s','node') is not None
    await h.stop(0)
    assert (await h.hold())['status']=='released'
    assert await lifecycle.registry.affinity('s','node') is None
    await h.start(1)
    task,_=await h.next_task(2);await h.finished(task)
    assert h.prepared[-1][0]==1
    assert (await h.hold())['hold_version']>first['hold_version']


@pytest.mark.parametrize('failure',['worker_timeout','service_timeout','scan','redis_close'])
async def test_warm_cleanup_joins_services_and_closes_clients_after_failure(failure):
    from types import SimpleNamespace
    h=Harness()
    h.stops=[asyncio.Event()]
    h.global_stop=asyncio.Event()
    h.workers=[]
    h.queue=SimpleNamespace(namespace='warm-cleanup-only')
    closed=[]
    async def worker():
        await (asyncio.Event() if failure=='worker_timeout' else h.stops[0]).wait()
    async def publisher():
        await (asyncio.Event() if failure=='service_timeout' else h.global_stop).wait()
    class Client:
        def __init__(self,name): self.name=name
        async def scan_iter(self,**kwargs):
            if failure=='scan': raise RuntimeError('scan failure')
            for key in (): yield key
        async def aclose(self):
            closed.append(self.name)
            if failure=='redis_close' and self.name=='realtime':
                raise RuntimeError('close failure')
    h.client,h.realtime_client=Client('command'),Client('realtime')
    h.running={0:asyncio.create_task(worker())}
    h.services=[asyncio.create_task(publisher())]
    try:
        with pytest.raises(BaseExceptionGroup) as error:
            await h.close(worker_timeout=.01,service_timeout=.01)
        assert error.value.exceptions
        assert h.stops[0].is_set() and h.global_stop.is_set()
        assert all(task.done() for task in [*h.running.values(),*h.services])
        assert closed==['realtime','command']
    finally:
        for task in [*h.running.values(),*h.services]: task.cancel()
        await asyncio.gather(*h.running.values(),*h.services,return_exceptions=True)
