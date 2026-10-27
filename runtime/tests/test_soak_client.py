"""SOAK scheduling and admission fail closed without retrying unknown effects."""
import importlib.util
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[2]/'scripts/reliability/soak_support/client.py'
spec=importlib.util.spec_from_file_location('soak_client',PATH)
client=importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def test_burst_starts_each_six_hours_for_ten_minutes():
    assert client.arrival_rate(0,.028,.0428)==.028
    assert client.arrival_rate(21599,.028,.0428)==.028
    assert client.arrival_rate(21600,.028,.0428)==.0428
    assert client.arrival_rate(22199,.028,.0428)==.0428
    assert client.arrival_rate(22200,.028,.0428)==.028
    assert client.arrival_rate(43200,.028,.0428)==.0428


def test_reuse_never_crosses_case_or_exceeds_two_turns():
    reuse=client.ConversationReuse(.5)
    assert reuse.take('text') is None
    reuse.complete('text','text-cid',1)
    assert reuse.take('file') is None
    assert reuse.take('text')==('text-cid',2)
    reuse.complete('text','text-cid',2)
    assert reuse.take('text') is None
    reuse.complete('file','file-cid',1)
    assert reuse.take('file')==('file-cid',2)


def test_only_definitely_rejected_technical_failure_is_explained():
    assert client.failure_class({'correct':True,'workload':'stop'}) is None
    assert client.failure_class({'correct':False,'http_status':429,'accepted':False})=='explained_rejection'
    assert client.failure_class({'correct':False,'error_type':'TimeoutError'})=='unknown_effect'
    assert client.failure_class({'correct':False,'id':'task','accepted':True,'http_status':429})=='unknown_effect'


def test_monitor_freshness_and_stop(tmp_path):
    (tmp_path/'host-samples.jsonl').write_text('{}\n')
    assert client.monitor_failure(tmp_path,45) is None
    (tmp_path/'STOP.json').write_text('{}')
    assert client.monitor_failure(tmp_path,45)=='resource_stop'


def test_task_artifact_trace_retention_is_deterministic(tmp_path):
    task=tmp_path/'task';task.mkdir()
    (task/'sse-x-0.jsonl').write_text('frame\n')
    record={'correct':True}
    client.retain_traces(task,record,1)
    assert not list(task.glob('sse-*')) and record['trace_receipts'][0]['sha256']
    (task/'sse-x-0.jsonl').write_text('frame\n')
    client.retain_traces(task,record,20)
    assert list(task.glob('sse-*'))
    client.retain_traces(task,{'correct':False},1)
    assert list(task.glob('sse-*'))


async def test_open_arrivals_do_not_wait_for_completion_or_retry_drops():
    import asyncio
    calls=[];rows=[];aborts=[]
    async def operation(index,planned):
        calls.append(index);await asyncio.sleep(.04)
        return dict(index=index,correct=True)
    async def completed(row):rows.append(row)
    async def abort(reason):aborts.append(reason)
    result=await client.drive_arrivals(.1,100,100,1,operation,completed,lambda:None,abort)
    assert calls==[0]
    assert aborts==['outstanding_limit'] and result['abort_reason']=='outstanding_limit'
    assert any(row.get('invalid') for row in rows)


async def test_unknown_effect_stops_before_next_arrival_and_planned_stop_is_success():
    calls=[];rows=[];aborts=[]
    async def operation(index,planned):
        calls.append(index)
        return dict(index=index,correct=False,error_type='TimeoutError')
    async def completed(row):rows.append(row)
    async def abort(reason):aborts.append(reason)
    await client.drive_arrivals(.15,10,10,4,operation,completed,lambda:None,abort)
    assert calls==[0] and aborts==['unknown_effect']


def test_invalid_success_retains_full_trace(tmp_path):
    (tmp_path/'sse-cid-0.jsonl').write_text('evidence')
    client.retain_traces(tmp_path,dict(correct=True,invalid=True),1)
    assert (tmp_path/'sse-cid-0.jsonl').exists()


async def test_warmup_run_writes_every_receipt_and_cleans_sources_before_stopping(tmp_path,monkeypatch):
    import asyncio
    import sys
    import types
    import time
    # Stub network modules only; exercise actual run/persistence/scheduling integration.
    calls=[];seen=[]
    fake_capacity=types.ModuleType('scripts.reliability.capacity_support.client')
    fake_capacity.loopback_url=lambda value:value
    fake_capacity.percentile=lambda values,p:sorted(values)[-1] if values else None
    fake_capacity.case_for=lambda i,w:'text' if i%2==0 else 'file'
    fake_capacity.budgets=lambda rows,**kw:{'passed':bool(rows)}
    async def drain(*a,**k):return {'passed':True}
    fake_capacity.drain=drain
    async def execute(args,http,headers,output,index,**kwargs):
        seen.append(index)
        if index==2:return dict(index=index,correct=False,error_type='TimeoutError')
        return dict(index=index,id='task-'+str(index),key='key',correct=True,workload=kwargs['case_override'],conversation_id='cid-'+str(index),submitted_monotonic=time.monotonic(),first_body_seconds=1,elapsed_seconds=2)
    fake_capacity.execute_case=execute
    parent=types.ModuleType('scripts.reliability.capacity_support');parent.client=fake_capacity
    monkeypatch.setitem(sys.modules,'scripts.reliability.capacity_support',parent)
    monkeypatch.setitem(sys.modules,'scripts.reliability.capacity_support.client',fake_capacity)
    runner=types.ModuleType('scripts.reliability.load_supervisor')
    async def stop(*a):calls.append('stop');return []
    runner.stop_fixture_tasks=stop;monkeypatch.setitem(sys.modules,runner.__name__,runner)
    schedule=types.ModuleType('scripts.reliability.soak_support.schedule')
    async def scheduled(args,http,headers,output,index,**kwargs):
        return dict(index=index,id='scheduled',correct=True,workload='text',source='schedule',source_submitted_monotonic=time.monotonic(),first_body_seconds=1,elapsed_seconds=2)
    async def cleanup(*a):calls.append('pause');return []
    schedule.execute_scheduled_case=scheduled;schedule.cleanup_scheduled_sources=cleanup
    monkeypatch.setitem(sys.modules,schedule.__name__,schedule)
    common=types.ModuleType('scripts.reliability.load_support.common');common.JWT_SECRET='fixture'
    monkeypatch.setitem(sys.modules,common.__name__,common)
    class Connection:
        async def close(self):pass
    async def connect(*a,**k):return Connection()
    monkeypatch.setitem(sys.modules,'asyncpg',types.SimpleNamespace(connect=connect))
    class Http:
        def __init__(self,*a,**k):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    monkeypatch.setitem(sys.modules,'httpx',types.SimpleNamespace(AsyncClient=Http))
    monkeypatch.setitem(sys.modules,'jwt',types.SimpleNamespace(encode=lambda *a,**k:'fixture'))
    class Redis:
        @classmethod
        def from_url(cls,*a,**k):return cls()
        async def aclose(self):pass
    monkeypatch.setitem(sys.modules,'redis.asyncio',types.SimpleNamespace(Redis=Redis))
    monitor=tmp_path/'monitor';monitor.mkdir();(monitor/'host-samples.jsonl').write_text('{}')
    args=types.SimpleNamespace(base_url='http://127.0.0.1',dsn='postgresql://127.0.0.1/db',redis_url='redis://127.0.0.1',namespace='fixture',output=tmp_path/'output',monitor=monitor,duration=7200,rate=.028,burst_rate=.0428,max_inflight=4,reuse=.5,monitor_stale_seconds=45,warmup_count=20)
    report=await client.run(args)
    assert not report['client_checks_passed'] and len(report['tasks'])==3
    assert seen==[1,2]  # first text was a real Schedule interface, never replayed
    assert calls.index('pause')<calls.index('stop')
    assert len((args.output/'index.jsonl').read_text().splitlines())==3
    assert (args.output/'task-2.json').exists() and (args.output/'progress.json').exists()


async def test_abort_joins_pending_sender_before_source_and_task_stop():
    import asyncio
    actions=[];rows=[];running=asyncio.Event()
    async def operation(index,planned):
        running.set()
        try:await asyncio.sleep(.1)
        finally:actions.append('sender-joined')
        return dict(index=index,correct=True)
    async def completed(row):rows.append(row)
    async def abort(reason):actions.append('pause-then-stop')
    result=await client.drive_arrivals(.1,100,100,1,operation,completed,lambda:None,abort)
    assert actions==['sender-joined','pause-then-stop']
    assert any(row.get('error_type')=='CancelledBySupervisor' for row in rows)
    assert 'arrival_start_monotonic' in result


async def test_preparation_launches_before_planned_but_never_outside_duration():
    import asyncio,time
    start=time.monotonic()+.03;calls=[];rows=[]
    async def operation(index,planned):
        launched=time.monotonic()
        await asyncio.sleep(max(0,planned-time.monotonic()))
        calls.append((index,planned,launched,time.monotonic()))
        return dict(index=index,correct=True)
    async def completed(row):rows.append(row)
    async def abort(reason):raise AssertionError(reason)
    result=await client.drive_arrivals(.03,50,50,4,operation,completed,lambda:None,abort,
                                      arrival_start=start,preparation_seconds=.03)
    assert len(calls)==2 and result['admitted_slots']==2
    assert calls[0][2]<start
    assert all(post>=planned and planned<start+.03 for _,planned,_,post in calls)


async def test_monitor_stop_cancels_prepared_wait_before_task_post():
    import asyncio,time
    began=time.monotonic();posted=[];rows=[];aborts=[]
    async def operation(index,planned):
        await asyncio.sleep(max(0,planned-time.monotonic()))
        posted.append(index)
        return dict(index=index,correct=True)
    async def completed(row):rows.append(row)
    async def abort(reason):aborts.append(reason)
    await client.drive_arrivals(.02,5,5,4,operation,completed,
        lambda:'resource_stop' if time.monotonic()-began>.01 else None,abort,
        arrival_start=began+.2,preparation_seconds=.2)
    assert posted==[] and aborts==['resource_stop']
    assert rows[0]['error_type']=='CancelledBySupervisor'
