import asyncio
import json
import pytest
from scripts.reliability.soak_support import schedule as s


def test_snapshot_recovery_accepts_initial_prefix_and_final():
    assert s.apply_snapshot({'task_id':'a','blocks':[{'kind':'text','text':'abc'}],'phase':'partial'},'a','abcdef')==('abc',False)
    assert s.apply_snapshot({'task_id':'a','blocks':[{'kind':'text','text':'abcdef'}],'phase':'final','incomplete':False},'a','abcdef')==('abcdef',True)
    with pytest.raises(AssertionError):s.apply_snapshot({'task_id':'a','blocks':[],'phase':'approval'},'a','abcdef')


def test_cleanup_uses_current_version_and_keeps_audit(tmp_path):
    class Response:
        def __init__(self,data):self.data=data
        def raise_for_status(self):pass
        def json(self):return self.data
    class Http:
        async def get(self,url,**kwargs):return Response({'status':'active','version':4,'next_fire_at':'future'})
        async def post(self,url,**kwargs):
            assert url=='/api/schedules/s/pause' and kwargs['json']=={'version':4}
            return Response({'status':'paused','next_fire_at':None})
    path=tmp_path/'scheduled-sources.json';s.save_sources(path,[{'id':'s','name':'exact','settled':False}])
    result=asyncio.run(s.cleanup_scheduled_sources(Http(),tmp_path))
    assert result==[{'schedule_id':'s','future_disabled':True}]
    assert json.loads(path.read_text())[0]['settled']


def test_unknown_create_outcome_not_silently_certified(tmp_path):
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'items':[],'total':0}
    class Http:
        async def get(self,*args,**kwargs):return Response()
    s.save_sources(tmp_path/'scheduled-sources.json',[{'name':'exact','settled':False,'creation_started':True}])
    with pytest.raises(AssertionError,match='Unknown Schedule'):asyncio.run(s.cleanup_scheduled_sources(Http(),tmp_path))


def test_initial_reset_final_snapshot_finishes_without_waiting_for_new_event(tmp_path):
    class Response:
        status_code=200
        async def aiter_lines(self):
            yield 'data: '+json.dumps({'name':'REALTIME_RESET','value':{'snapshots':[{'task_id':'t','phase':'final','incomplete':False,'blocks':[{'kind':'text','text':'abc'}]}]}})
    class Context:
        async def __aenter__(self):return Response()
        async def __aexit__(self,*args):pass
    class Http:
        def stream(self,*args):return Context()
    result=asyncio.run(s.consume_scheduled(Http(),'c','t','abc',tmp_path,0))
    assert result['body']=='abc' and result['terminal_source']=='final_snapshot' and result['reset_count']==1


def test_real_schedule_path_records_identity_and_verifies_pg(monkeypatch,tmp_path):
    import argparse,asyncpg,httpx
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from scripts.reliability.load_support.common import chunk
    key='a'*32;expected=''.join(chunk(key,i) for i in range(600));now=datetime.now(timezone.utc)
    monkeypatch.setattr(s.uuid,'uuid4',lambda:SimpleNamespace(hex=key))
    class DB:
        async def fetchval(self,query,*params):
            if 'clock_timestamp' in query:return now
            if 'active_product' in query:return True
            if 'runtime_events' in query:return 0
            return 1
        async def fetchrow(self,query,*params):
            if 'FROM tasks WHERE' in query:return {'status':'completed','origin':'schedule','approval_profile':'unattended','created_at':now,'run_id':'reply'}
            return {'snapshot_phase':'final','content':{'blocks':[{'kind':'text','text':expected}],'text':expected.strip()}}
        async def close(self):pass
    async def connect(*a,**kw):return DB()
    monkeypatch.setattr(asyncpg,'connect',connect)
    class Response:
        status_code=201
        def __init__(self,value):self.value=value
        def json(self):return self.value
        def raise_for_status(self):pass
    class Http:
        async def post(self,path,**kw):
            assert path=='/api/schedules'
            assert json.loads((tmp_path/'scheduled-sources.json').read_text())[0]['creation_started']
            assert kw['json']['notification'] is None and kw['json']['rule']['kind']=='once'
            return Response({'id':'s','version':1})
        async def get(self,path,**kw):
            if path.endswith('/occurrences'):return Response({'total':1,'items':[{'id':'o','task_id':'t','conversation_id':'c','outcome':'task_created'}]})
            return Response({'id':'s','version':2,'status':'completed','next_fire_at':None})
    async def consume(*args):
        tick=s.time.monotonic()
        return {'body':expected,'first':tick,'started':tick,'terminal':tick,'reset_count':1,'terminal_source':'final_snapshot'}
    monkeypatch.setattr(s,'consume_scheduled',consume)
    args=argparse.Namespace(output=tmp_path,dsn='postgresql://127.0.0.1/fixture',base_url='http://127.0.0.1:8080')
    record=asyncio.run(s.execute_scheduled_case(args,Http(),{},tmp_path,0))
    assert record['correct'] and record['source']=='schedule' and record['id']=='t'
    assert record['source_submitted_monotonic'] and record['scheduled_for']!=record['schedule_creation_pg']
    assert len(record['reader_timings'])==2
    assert json.loads((tmp_path/'scheduled-sources.json').read_text())[0]['settled']


def test_cancel_during_create_records_id_then_pauses_source(monkeypatch,tmp_path):
    import argparse,asyncpg
    from datetime import datetime,timezone
    started=asyncio.Event();paused=[]
    class DB:
        async def fetchval(self,*a):return datetime.now(timezone.utc)
        async def close(self):pass
    async def connect(*a,**k):return DB()
    monkeypatch.setattr(asyncpg,'connect',connect)
    class Response:
        status_code=201
        def __init__(self,value):self.value=value
        def json(self):return self.value
        def raise_for_status(self):pass
    class Http:
        async def post(self,path,**kw):
            if path.endswith('/pause'):
                paused.append(path);return Response({'status':'paused','next_fire_at':None})
            started.set();await asyncio.sleep(.03)
            return Response({'id':'s','version':1})
        async def get(self,*a,**kw):return Response({'version':1,'status':'active','next_fire_at':'future'})
    args=argparse.Namespace(output=tmp_path,dsn='postgresql://127.0.0.1/fixture',base_url='http://127.0.0.1:8080')
    async def run():
        operation=asyncio.create_task(s.execute_scheduled_case(args,Http(),{},tmp_path,0))
        await started.wait();operation.cancel()
        with pytest.raises(asyncio.CancelledError):await operation
    asyncio.run(run())
    assert paused==['/api/schedules/s/pause']
    assert json.loads((tmp_path/'scheduled-sources.json').read_text())[0]['id']=='s'


def test_planned_admission_wait_precedes_database_due_clock(monkeypatch,tmp_path):
    import argparse,asyncpg
    from datetime import datetime,timezone
    calls=[];target=s.time.monotonic()+.03
    class DB:
        async def fetchval(self,query):
            assert s.time.monotonic()>=target
            calls.append('due_clock');return datetime.now(timezone.utc)
        async def close(self):pass
    async def connect(*a,**kw):calls.append('connect');return DB()
    monkeypatch.setattr(asyncpg,'connect',connect)
    class Response:
        status_code=201
        def json(self):return {'id':'s','version':1}
    class Http:
        async def post(self,path,**kwargs):
            assert s.time.monotonic()>=target
            assert datetime.fromisoformat(kwargs['json']['rule']['run_at'])>datetime.now(timezone.utc)
            calls.append('post');return Response()
        async def get(self,*a,**kw):raise RuntimeError('End fixture after admission')
    async def cleanup(*args):return []
    monkeypatch.setattr(s,'cleanup_scheduled_sources',cleanup)
    args=argparse.Namespace(output=tmp_path,dsn='postgresql://127.0.0.1/fixture',base_url='http://127.0.0.1:8080')
    record=asyncio.run(s.execute_scheduled_case(args,Http(),{},tmp_path,0,admission_not_before=target))
    assert calls==['connect','due_clock','post']
    assert record['submitted_monotonic']>=target
    assert record['submitted_monotonic']==record['source_submitted_monotonic']


def test_slow_due_clock_refuses_late_schedule_post(monkeypatch,tmp_path):
    import argparse,asyncpg
    from datetime import datetime,timezone
    class DB:
        async def fetchval(self,query):await asyncio.sleep(.51);return datetime.now(timezone.utc)
        async def close(self):pass
    async def connect(*a,**kw):return DB()
    monkeypatch.setattr(asyncpg,'connect',connect)
    class Http:
        async def post(self,*args,**kwargs):pytest.fail('Late source creation must not happen')
    args=argparse.Namespace(output=tmp_path,dsn='postgresql://127.0.0.1/fixture',base_url='http://127.0.0.1:8080')
    record=asyncio.run(s.execute_scheduled_case(args,Http(),{},tmp_path,0,admission_not_before=s.time.monotonic()+.01))
    assert not record['correct'] and record['error_type']=='TimeoutError'
    assert json.loads((tmp_path/'scheduled-sources.json').read_text())[0]['settled']
