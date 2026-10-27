import asyncio
import pytest
from scripts.reliability import metrics as m


def test_dsn_loopback_only():
    assert m.validate_dsn('postgresql://u:p@127.0.0.1:15432/fixture')
    for value in ['postgresql://u:p@remote/fixture','postgresql://u:p@127.0.0.1/fixture?host=evil','postgresql://u:p@0.0.0.0/fixture']:
        with pytest.raises(ValueError):m.validate_dsn(value)


def test_summary_task_ids_and_delivery_missing():
    summary={'runs':[{'tasks':[{'id':'a'},{'id':'a'},{'id':'b','elapsed_seconds':10}]}]}
    assert m.task_ids(summary)==['a','b']
    assert m.delivery(summary)['p95_seconds'] is None
    assert m.delivery(summary)['missing_tasks']==3


def test_sample_sequential_readonly_and_redacted(monkeypatch):
    calls=[]
    class Conn:
        async def execute(self,query):calls.append(query)
        async def fetch(self,query,*args):
            calls.append(query)
            if query==m.TASK_SQL:return [{'id':'a','status':'completed','queue_seconds':2.,'execution_seconds':12.}]
            if query==m.ACTIVITY_SQL:return [{'state':'active','wait_event_type':None,'wait_event':None,'count':2}]
            return []
        async def fetchrow(self,query,*args):calls.append(query);return {'calls':12,'total_exec_time_ms':50.}
        async def fetchval(self,query):calls.append(query);return True
        async def close(self):calls.append('closed')
    async def connect(*args,**kw):return Conn()
    monkeypatch.setattr(m,'connect',connect)
    result=asyncio.run(m.sample('postgresql://u:p@127.0.0.1/fixture',['a','missing']))
    assert result['queue_p95_seconds']==2 and result['execution_p95_seconds']==12
    assert result['missing_task_ids']==['missing'] and calls[-1]=='closed'
    assert 'READ ONLY' in calls[0]
    assert result['delivery']['p95_seconds'] is None
    assert 'u:p' not in str(result)


def test_percentile_ignores_missing_negative_and_nonfinite():
    assert m.p95([None,-1,float('nan'),1,2,3])==3
    assert m.p95([None,-1]) is None


def test_failed_read_closes_connection(monkeypatch):
    closed=[]
    class Conn:
        async def execute(self,query):pass
        async def fetch(self,*args):raise RuntimeError('read failed')
        async def close(self):closed.append(True)
    async def connect(*a,**kw):return Conn()
    monkeypatch.setattr(m,'connect',connect)
    with pytest.raises(RuntimeError):asyncio.run(m.sample('postgresql://fixture:fixture@127.0.0.1/fixture',[]))
    assert closed==[True]


def test_delivery_uses_per_reader_local_intervals_and_counts_old_missing():
    summary={'runs':[{'tasks':[
        {'reader_timings':[{'reader_index':0,'run_started_monotonic':10,'first_body_monotonic':11,'terminal_monotonic':21},
                           {'reader_index':1,'run_started_monotonic':10.5,'first_body_monotonic':12,'terminal_monotonic':22}]},
        {'first_body_to_terminal_seconds':9,'readers_terminal_spread_seconds':.5},
        {'id':'old','elapsed_seconds':99}]}]}
    value=m.delivery(summary)
    assert value['p95_seconds']==10
    assert value['run_started_to_terminal_p95_seconds']==11.5
    assert value['readers_terminal_spread_p95_seconds']==1
    assert value['missing_tasks']==1 and value['sample_count']==2
    assert 'generation' in value['definition']


def test_delivery_sse_streams_and_invalid_pairs():
    summary={'runs':[{'tasks':[{'streams':{'0':{'started':1,'first':2,'terminal':5}}},
                              {'reader_timings':[{'run_started_monotonic':7,'first_body_monotonic':8,'terminal_monotonic':6}]}]}]}
    result=m.delivery(summary)
    assert result['p95_seconds']==3 and result['missing_tasks']==1
    assert result['invalid_reader_intervals']==1
