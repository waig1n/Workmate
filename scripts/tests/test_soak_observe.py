import json
import pytest
from scripts.reliability.soak_support import observe


def test_loopback_only_and_owner_validation():
    assert observe.loopback('postgresql://u:p@127.0.0.1:5432/db',{'postgresql'})
    for value in ('postgresql://u:p@public:5432/db','redis://localhost.evil:6379'):
        with pytest.raises(ValueError):observe.loopback(value,{'postgresql','redis'})


def test_proc_stat_handles_spaces_and_parens():
    fields=['S','1']+['0']*9+['10','11']+['0']*6+['999','0','7']
    row=observe.parse_stat('55 (a (b) c) '+' '.join(fields),4096)
    assert row==dict(pid=55,ppid=1,cpu_ticks=21,start_ticks=999,rss_bytes=28672,state='S')


def test_fixed_role_restart_and_missing_sample_failclosed():
    roles={'gateway':{'pid':2,'start_ticks':5}}
    sample={'processes':{'roles':roles}}
    assert observe.role_errors(sample,roles)==[]
    sample['processes']['roles']['gateway']={'pid':2,'start_ticks':6}
    assert observe.role_errors(sample,{'gateway':{'pid':2,'start_ticks':5}})==['role_identity_changed:gateway']
    assert observe.role_errors({},roles)==['process_sample_missing']


def test_atomic_json(tmp_path):
    path=tmp_path/'progress.json';observe.atomic_json(path,{'phase':'warm_idle'})
    assert json.loads(path.read_text())=={'phase':'warm_idle'}
    assert list(tmp_path.iterdir())==[path]


def snapshot():
    host=dict(available_bytes=8*2**30,disk_free_bytes=50*2**30,disk_total_bytes=100*2**30,health_status=200,vmstat={'oom_kill':0})
    role=dict(pid=2,start_ticks=4,rss_bytes=500,fd_count=4,cpu_ticks=10)
    return dict(hosts={'local':dict(host),'remote':dict(host)},components={'measured_at':1},database={'pg':{},'redis':{}},processes={'roles':{'gateway':role}})


def arguments(tmp_path):
    return ['--owner','capacity-123456abcdef','--dsn','postgresql://u:p@127.0.0.1/db','--redis-url','redis://127.0.0.1','--output',str(tmp_path),'--duration','1']


def test_main_failure_writes_atomic_stop_and_summary(monkeypatch,tmp_path):
    observe.atomic_json(tmp_path/'phase.json',{'phase':'prerun'})
    sample=snapshot();sample['database']={'error':'TimeoutError'}
    monkeypatch.setattr(observe,'collect',lambda *args:sample)
    assert observe.main(arguments(tmp_path))==1
    assert json.loads((tmp_path/'STOP.json').read_text())['reasons']==['database_sample_missing']
    assert not json.loads((tmp_path/'monitor-summary.json').read_text())['passed']
    progress=json.loads((tmp_path/'progress.json').read_text())
    assert progress['sample']['phase']=='prerun'
    assert 'p@' not in (tmp_path/'progress.json').read_text()


def test_missing_phase_never_succeeds(tmp_path):
    assert observe.main(arguments(tmp_path))==1
    summary=json.loads((tmp_path/'monitor-summary.json').read_text())
    assert 'no_samples' in summary['failures']


def test_collect_preserves_public_schema(monkeypatch):
    row=dict(pid=2,ppid=1,start_ticks=4,rss_bytes=500,fd_count=4,cpu_ticks=10)
    monkeypatch.setattr(observe,'probe',lambda *args:{'owned_data_bytes':123})
    monkeypatch.setattr(observe,'components',lambda *args:{'measured_at':1})
    monkeypatch.setattr(observe,'processes',lambda *args:dict(roles={'gateway':row,'worker-0':dict(row,pid=3)},descendants=[dict(row,pid=5,ppid=3)]))
    async def metrics(*args):return {'pg':{'active_tasks':1},'redis':{'pending':0}}
    monkeypatch.setattr(observe,'database_metrics',metrics)
    value=observe.collect('capacity-123456abcdef','dsn','redis')
    assert value['postgres']['active_tasks']==1
    assert value['processes']['workers'][0]['descendants'][0]['pid']==5
    assert value['processes']['gateway']['fds']==4


def test_proc_probe_source_is_kernel_only(monkeypatch):
    from types import SimpleNamespace
    def run(args,**kwargs):
        source=kwargs['input'];compile(source,'observer','exec')
        for forbidden in ('get_coro','cr_await','gi_yieldfrom','ag_await','_frame','sys._current'):
            assert forbidden not in source
        assert args[-2:]==['python','-']
        return SimpleNamespace(stdout='{"roles":{}}')
    monkeypatch.setattr(observe.subprocess,'run',run)
    assert observe.processes('capacity-123456abcdef')=={'roles':{}}


@pytest.mark.asyncio
async def test_database_metrics_readonly_and_namespace_filtered(monkeypatch):
    import sys
    from types import SimpleNamespace
    queries=[];patterns=[]
    class Transaction:
        async def __aenter__(self):pass
        async def __aexit__(self,*args):pass
    class Connection:
        def transaction(self,**kw):assert kw=={'readonly':True};return Transaction()
        async def fetchrow(self,sql):
            queries.append(sql)
            return {'connections':3,'database_bytes':500,'wal_bytes':123,'lock_waits':0} if 'pg_stat_activity' in sql else {'count':0,'oldest_age_seconds':0.0}
        async def fetch(self,sql):return [{'type':'none','count':3}]
        async def fetchval(self,sql):return 1
        async def close(self):pass
    async def connect(*args,**kw):return Connection()
    class Redis:
        async def info(self,*args):return {'used_memory':456}
        async def scan_iter(self,**kwargs):
            patterns.append(kwargs['match']);yield 'workmate:capacity-123456abcdef:stream';yield 'workmate:capacity-123456abcdef:value'
        async def type(self,key):return 'stream' if key.endswith('stream') else 'string'
        async def xinfo_groups(self,key):return [{'pending':2,'lag':3,'consumers':1}]
        async def xlen(self,key):return 5
        async def aclose(self):pass
    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    import redis.asyncio
    monkeypatch.setattr(redis.asyncio,'from_url',lambda *args,**kw:Redis())
    result=await observe.database_metrics('dsn','redis','capacity-123456abcdef')
    assert result['pg']['active_tasks']==1
    assert result['redis']['pending']==2 and result['redis']['lag']==3
    assert patterns==['workmate:capacity-123456abcdef:*']
    assert any('command_deliveries' in query for query in queries)


def test_components_use_allocated_blocks_without_total_double_count(monkeypatch):
    from types import SimpleNamespace
    def run(args,**kwargs):
        source=kwargs['input'];compile(source,'components','exec')
        assert 'total+=info.st_blocks*512' in source
        assert 'shared/workspaces' in source and 'follow_symlinks=False' in source
        return SimpleNamespace(stdout='{"measured_at":123,"directories":{}}')
    monkeypatch.setattr(observe.subprocess,'run',run)
    assert observe.components('capacity-123456abcdef')['measured_at']==123


def test_cpu_deltas_exclude_guest_double_count():
    before=snapshot();after=snapshot();before['time_unix']=10;after['time_unix']=20
    for host in ('local','remote'):
        before['hosts'][host]['cpu_ticks']='cpu 10 0 0 90 0 0 0 0 10 0'
        after['hosts'][host]['cpu_ticks']='cpu 30 0 0 170 0 0 0 0 30 0'
    before['processes']['roles']['gateway']['cpu_ticks']=10
    after['processes']['roles']['gateway']['cpu_ticks']=210
    after['processes']['clock_ticks_per_second']=100
    observe.add_deltas(after,before)
    assert after['hosts']['local']['cpu_busy_fraction']==.2
    assert after['processes']['roles']['gateway']['cpu_cores']==.2


@pytest.mark.parametrize('size,failed',[(4*2**30+1,False),(8*2**30,False),(8*2**30+1,True)])
def test_soak_data_budget_is_independently_frozen(size,failed):
    sample=snapshot();sample['hosts']['remote']['owned_data_bytes']=size
    assert ('remote:data_cap' in observe.soak_violations(sample)) is failed


def test_swap_requires_continuous_60_seconds_and_falling_memory():
    state={};previous=snapshot();previous['time_unix']=0
    for timestamp in range(10,71,10):
        sample=snapshot();sample['time_unix']=timestamp
        for host in sample['hosts']:
            sample['hosts'][host]['available_bytes']=previous['hosts'][host]['available_bytes']-1
            sample['hosts'][host]['swap_pages_delta']={'pswpin':1,'pswpout':0}
        errors=observe.swap_errors(sample,previous,state)
        assert bool(errors)==(timestamp>=60)
        previous=sample
    sample=snapshot();sample['time_unix']=80
    assert observe.swap_errors(sample,previous,state)==[]
    assert not state


def test_soak_budget_does_not_relax_health_or_memory():
    sample=snapshot();sample['hosts']['remote'].update(owned_data_bytes=6*2**30,health_status=503,available_bytes=1)
    errors=observe.soak_violations(sample)
    assert 'remote:business_health' in errors
    assert 'remote:memory_floor' in errors
    assert 'remote:data_cap' not in errors


def test_guarded_records_stage_exit_and_sanitized_bounded_stderr():
    import subprocess
    def fail():
        raise subprocess.CalledProcessError(7,['private-command'],stderr='password=hunter2 redis://u:secret@host Bearer abc\n'+'x'*1000+'\nProcessLookupError: vanished')
    result=observe.guarded(fail,stage='processes',category='docker_exec_proc')
    assert result['error']=='CalledProcessError'
    detail=result['sample_errors'][0]
    assert detail['stage']=='processes' and detail['command_category']=='docker_exec_proc'
    assert detail['returncode']==7 and len(detail['stderr_summary'])<=800
    assert 'ProcessLookupError' in detail['stderr_summary']
    assert not any(value in json.dumps(result) for value in ('hunter2','secret@','Bearer abc','private-command'))


@pytest.mark.parametrize('error_type',[FileNotFoundError,ProcessLookupError,PermissionError,OSError])
def test_proc_churn_only_missing_process_is_tolerated(monkeypatch,error_type):
    from pathlib import Path
    from types import SimpleNamespace
    role_stat='2 (role) '+' '.join(['S','1']+['0']*9+['10','11']+['0']*6+['999','0','7'])
    class Folder:
        def __init__(self,name):self.name=name
        def __truediv__(self,name):
            if self.name=='9':raise error_type('controlled proc churn')
            return SimpleNamespace(read_text=lambda:role_stat,iterdir=lambda:iter(()))
    def path(value):
        if value=='/evidence/ready.json':return SimpleNamespace(read_text=lambda:json.dumps({'pid':2,'children':{'gateway':2}}))
        if value=='/proc':return SimpleNamespace(iterdir=lambda:iter([Folder('2'),Folder('9')]))
        return Path(value)
    monkeypatch.setattr(observe,'Path',path)
    if error_type in (PermissionError,OSError):
        with pytest.raises(error_type):observe.process_snapshot()
    else:
        sample=observe.process_snapshot()
        assert sample['vanished_pids']==[9]
        assert sample['roles']['gateway']['pid']==2


@pytest.mark.parametrize('fixed_child',[False,True])
def test_real_proc_exit_between_stat_and_fd_read(monkeypatch,fixed_child):
    """Force a real kernel /proc disappearance without scanning unrelated processes."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    class ChurningFolder:
        name=str(child.pid)
        def __truediv__(self,name):
            if name=='fd':
                child.terminate();child.wait(timeout=5)
            return Path('/proc')/str(child.pid)/name
    ready={'pid':os.getpid(),'children':{'gateway':child.pid if fixed_child else os.getpid()}}
    def path(value):
        if value=='/evidence/ready.json':return SimpleNamespace(read_text=lambda:json.dumps(ready))
        if value=='/proc':return SimpleNamespace(iterdir=lambda:iter([Path('/proc')/str(os.getpid()),ChurningFolder()]))
        return Path(value)
    monkeypatch.setattr(observe,'Path',path)
    try:
        if fixed_child:
            with pytest.raises(ValueError,match='Fixed role missing'):observe.process_snapshot()
        else:
            sample=observe.process_snapshot()
            assert sample['vanished_pids']==[child.pid]
            assert sample['roles']['gateway']['pid']==os.getpid()
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)


def test_collect_failed_process_preserves_diagnostic_and_cannot_pass(monkeypatch):
    import subprocess
    monkeypatch.setattr(observe,'components',lambda *args:{'measured_at':1})
    monkeypatch.setattr(observe,'probe',lambda *args:{})
    def fail(*args):raise subprocess.CalledProcessError(1,['omitted'],stderr='PermissionError: denied')
    monkeypatch.setattr(observe,'processes',fail)
    async def metrics(*args):return {'pg':{},'redis':{}}
    monkeypatch.setattr(observe,'database_metrics',metrics)
    result=observe.collect('capacity-123456abcdef','dsn','redis')
    assert observe.role_errors(result,{})==['process_sample_missing']
    assert result['processes']['sample_errors'][0]['stage']=='processes'
    assert result['processes']['sample_errors'][0]['stderr_summary']=='PermissionError: denied'


@pytest.mark.parametrize('changed_field',['pid','start_ticks'])
def test_proc_identity_change_during_fd_read_is_failure(monkeypatch,changed_field):
    from types import SimpleNamespace
    initial=dict(pid=2,ppid=1,state='S',cpu_ticks=10,start_ticks=20,rss_bytes=100)
    values=iter([initial,{**initial,changed_field:99}])
    monkeypatch.setattr(observe,'parse_stat',lambda *args:next(values))
    class Folder:
        name='2'
        def __truediv__(self,name):return SimpleNamespace(read_text=lambda:'stat',iterdir=lambda:iter(()))
    monkeypatch.setattr(observe,'Path',lambda value:SimpleNamespace(read_text=lambda:json.dumps({'pid':2,'children':{'gateway':2}})) if value=='/evidence/ready.json' else SimpleNamespace(iterdir=lambda:iter([Folder()])))
    with pytest.raises(ValueError,match='Process identity changed'):
        observe.process_snapshot()


@pytest.fixture(autouse=True)
def explicit_fixture_remote(monkeypatch):
    monkeypatch.setenv('WORKMATE_TEST_SSH_TARGET', 'operator@example.test')
