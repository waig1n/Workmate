import asyncio
from copy import deepcopy
import pytest
from scripts.reliability import soak


def task(case='text',**kw):
    return dict(workload=case,correct=True,first_body_seconds=1,elapsed_seconds=12,**kw)


def summary():
    return {'client_checks_passed':True,'tasks':[task(source='schedule'),task('file')], 'closures':[{'passed':True}]}


def test_phase_gate_stop_without_body_is_valid_but_nonzero_sender_blocks():
    value=summary();value['tasks'].append({**task('stop'),'first_body_seconds':None})
    assert soak.phase_gate(value,sender_exit=0)['passed']
    assert not soak.phase_gate(value,sender_exit=1)['passed']


def test_growth_requires_real_two_hour_sample_coverage():
    def sample(t):return {'time_unix':t,'hosts':{'remote':{'owned_data_bytes':100,'disk_total_bytes':100*2**30,'disk_free_bytes':50*2**30}}}
    assert not soak.growth_gate([sample(0),sample(10)],sample(10))['passed']
    rows=[sample(i) for i in range(0,7201,10)]
    assert soak.growth_gate(rows,rows[-1])['passed']
    assert not soak.growth_gate(rows[:20]+rows[30:],rows[-1])['passed']


def test_recovery_empty_role_evidence_must_fail():
    value={'processes':{'roles':{},'descendants':[]},'postgres':{'active_tasks':0,'connections':1}}
    assert not soak.recovery_gate(value,value)['passed']


def test_observer_final_receipt_required(tmp_path):
    import json
    assert not soak.observer_gate(tmp_path,0)['passed']
    (tmp_path/'monitor-summary.json').write_text(json.dumps({'passed':True,'failures':[]}))
    assert soak.observer_gate(tmp_path,0)['passed']
    assert not soak.observer_gate(tmp_path,1)['passed']
    (tmp_path/'STOP.json').write_text('{}')
    assert not soak.observer_gate(tmp_path,0)['passed']


def test_soak_status_accepts_cleanup_keyword(monkeypatch):
    seen=[]
    def status(self,allow_stopped=False):
        seen.append(allow_stopped);return {'stop_reasons':['4GiB data budget'],'resources':{'data_bytes':5*2**30}}
    monkeypatch.setattr(soak.Remote,'status',status)
    assert soak.SOAKRemote({'owner':'capacity-0123456789ab'},None).status(allow_stopped=True)['passed']
    assert seen==[True]


def test_automatic_formal_transition_requires_every_prerun_gate():
    passed={'sender_exit_code':0,**{key:{'passed':True} for key in ('gate','effects','recovery','growth_projection','latency_trend','task_evidence')}}
    assert soak.formal_transition_gate(passed)['passed']
    for key in passed:
        missing=deepcopy(passed);del missing[key]
        assert not soak.formal_transition_gate(missing)['passed'],key


def test_latency_trend_excludes_burst_and_checks_planned_clock():
    values=[{**task(),'planned_monotonic':x} for x in (0,100,1000,25000)]
    values.append({**task(),'planned_monotonic':21600,'elapsed_seconds':300})
    report={'schedule':{'arrival_start_monotonic':0},'tasks':values}
    assert soak.latency_trend(report)['passed']
    del report['schedule']['arrival_start_monotonic']
    assert not soak.latency_trend(report)['passed']


def test_child_shutdown_escalates_and_reaps_after_grace():
    class Child:
        returncode=None
        def __init__(self):self.signals=[];self.killed=False
        def send_signal(self,value):self.signals.append(value)
        def kill(self):self.killed=True;self.returncode=-9
        async def wait(self):
            if self.returncode is None:await asyncio.sleep(99)
            return self.returncode
    child=Child();result=asyncio.run(soak.terminate_child(child,grace=.001))
    assert result=={'exit_code':-9,'forced_kill':True} and child.killed


def test_startup_failure_and_source_cleanup_failure_do_not_skip_task_stop_or_remote_cleanup(tmp_path,monkeypatch):
    import argparse,json
    from scripts.reliability import load_supervisor
    from scripts.reliability.soak_support import schedule
    calls=[];output=tmp_path/'campaign';state=tmp_path/'state.json'
    state.write_text(json.dumps({'owner':'capacity-0123456789ab'}))
    class Remote:
        def __init__(self,*args):pass
        def save(self):pass
        def sample(self):
            (output/'warmup').mkdir();raise RuntimeError('preflight failed')
        def cleanup(self):calls.append('remote_cleanup');return {'passed':True}
    async def sources(*args):calls.append('source_cleanup');raise RuntimeError('source unavailable')
    async def stop(*args):calls.append('task_stop');return []
    monkeypatch.setattr(soak,'SOAKRemote',Remote)
    monkeypatch.setattr(schedule,'cleanup_scheduled_sources',sources)
    monkeypatch.setattr(load_supervisor,'stop_fixture_tasks',stop)
    args=argparse.Namespace(output=output,state=state,base_url='http://127.0.0.1:18080',dsn='postgresql://127.0.0.1:15432/fixture',redis_url='redis://127.0.0.1:16379')
    result=asyncio.run(soak.execute(args))
    assert not result['passed'] and result['cleanup']['passed']
    assert calls==['source_cleanup','task_stop','remote_cleanup']
    assert any('source_cleanup' in reason for reason in result['cleanup_errors'])


def test_malformed_observer_receipt_fails_without_raising(tmp_path):
    (tmp_path/'monitor-summary.json').write_text('[]')
    assert not soak.observer_gate(tmp_path,0)['passed']


@pytest.mark.asyncio
async def test_task_evidence_is_saved_before_failed_gate(monkeypatch,tmp_path):
    import json
    from datetime import datetime,timezone
    from scripts.reliability import metrics
    value=summary();value['client_checks_passed']=False
    value['tasks'][0]['id']='a';value['tasks'][1]['id']='b'
    async def sample(dsn,ids):
        assert ids==['a','b']
        return {'sampled_at_pg':datetime.now(timezone.utc),'missing_task_ids':[],'tasks':[{'id':x} for x in ids]}
    async def deliveries(dsn,ids):return [{'task_id':taskid,'kind':'StartTask','status':'published','count':1} for taskid in ids]
    monkeypatch.setattr(metrics,'sample',sample)
    monkeypatch.setattr(soak,'command_delivery_metrics',deliveries)
    receipt=await soak.save_task_evidence('dsn',value,tmp_path)
    assert receipt['passed']
    assert json.loads((tmp_path/'task-metrics.json').read_text())['postgres']['tasks']==[{'id':'a'},{'id':'b'}]
    assert not soak.phase_gate(value)['passed']


@pytest.mark.asyncio
async def test_task_evidence_failure_is_persisted_and_blocks(monkeypatch,tmp_path):
    import json
    from scripts.reliability import metrics
    async def sample(*args):raise RuntimeError('postgresql://user:private@host secret')
    monkeypatch.setattr(metrics,'sample',sample)
    receipt=await soak.save_task_evidence('dsn',{'tasks':[{'id':'a'}]},tmp_path)
    assert not receipt['passed']
    text=(tmp_path/'task-metrics.json').read_text()
    assert 'private' not in text and 'RuntimeError' in text
    assert json.loads(text)['passed'] is False


@pytest.mark.asyncio
async def test_required_task_evidence_absence_and_bound_block(tmp_path):
    assert not (await soak.save_task_evidence('dsn',{'tasks':[]},tmp_path/'empty'))['passed']
    assert not (await soak.save_task_evidence('dsn',{'tasks':[{'id':'a'}]*5001},tmp_path/'oversized'))['passed']


@pytest.mark.asyncio
async def test_command_delivery_snapshot_is_readonly_and_scoped(monkeypatch):
    from scripts.reliability import metrics
    seen=[]
    class Transaction:
        async def __aenter__(self):pass
        async def __aexit__(self,*args):pass
    class Connection:
        def transaction(self,**kw):assert kw=={'readonly':True};return Transaction()
        async def fetch(self,sql,ids):
            assert 'ANY($1::varchar[])' in sql and ids==['task']
            assert 'payload' not in sql
            assert 'count(d.command_id)' in sql and 'count(*)' not in sql
            seen.append('query');return [{'task_id':'task','count':1}]
        async def close(self):seen.append('close')
    async def connect(*args,**kw):return Connection()
    monkeypatch.setattr(metrics,'connect',connect)
    result=await soak.command_delivery_metrics('postgresql://u:p@127.0.0.1/db',['task'])
    assert result==[{'task_id':'task','count':1}] and seen==['query','close']


@pytest.mark.asyncio
@pytest.mark.parametrize('deliveries,missing',[
    ([],['a','b']),
    ([{'task_id':'a','kind':'StartTask','status':'published','count':1}],['b']),
    ([{'task_id':'a','kind':'StartTask','status':None,'count':0},{'task_id':'b','kind':'StopTask','status':'published','count':1}],['a','b']),
])
async def test_required_start_delivery_missing_cannot_pass(monkeypatch,tmp_path,deliveries,missing):
    import json
    from scripts.reliability import metrics
    async def sample(*args):return {'missing_task_ids':[],'tasks':[{'id':'a'},{'id':'b'}]}
    async def delivery(*args):return deliveries
    monkeypatch.setattr(metrics,'sample',sample)
    monkeypatch.setattr(soak,'command_delivery_metrics',delivery)
    receipt=await soak.save_task_evidence('dsn',{'tasks':[{'id':'a'},{'id':'b'}]},tmp_path)
    assert not receipt['passed']
    assert json.loads((tmp_path/'task-metrics.json').read_text())['missing_start_delivery_task_ids']==missing


@pytest.fixture(autouse=True)
def explicit_fixture_remote(monkeypatch):
    monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', 'operator@example.test')
