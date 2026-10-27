"""Supervisor never resumes admission after a resource stop."""
import asyncio
import json
from pathlib import Path
import signal
from types import SimpleNamespace
import time

import pytest
from scripts.reliability import load_supervisor

pytestmark=pytest.mark.asyncio


@pytest.fixture
def environment(tmp_path,monkeypatch):
    monitor=tmp_path/'monitor';monitor.mkdir()
    (monitor/'host-samples.jsonl').write_text('{}\n')
    args=SimpleNamespace(output=tmp_path/'run',monitor=monitor,timeout=60,
                         base_url='http://127.0.0.1:8080',dsn='postgresql://127.0.0.1/test',
                         redis_url='redis://127.0.0.1:6379',namespace='isolated')
    actions=[]
    class Process:
        pid=123456
        returncode=None
        async def wait(self):
            while self.returncode is None:await asyncio.sleep(0)
            return self.returncode
        def terminate(self):actions.append('terminate');self.returncode=-15
        def kill(self):actions.append('kill');self.returncode=-9
    process=Process()
    def killpg(pid,sig):
        assert pid==process.pid
        actions.append(sig)
        if sig==signal.SIGKILL:process.returncode=-9
    async def stop(*args):actions.append('http-stop');return [{'task_id':'task','status':200}]
    monkeypatch.setattr(load_supervisor.os,'killpg',killpg)
    monkeypatch.setattr(load_supervisor,'stop_fixture_tasks',stop)
    return args,actions,process


@pytest.mark.parametrize('trigger',['resource_stop','monitor_stale','monitor_ended','run_deadline'])
async def test_abort_freezes_before_http_and_never_resumes(environment,monkeypatch,trigger):
    args,actions,process=environment
    async def spawn(*cmd,**kwargs):
        if trigger=='resource_stop':(args.monitor/'STOP.json').write_text('{}')
        if trigger=='monitor_ended':(args.monitor/'monitor-summary.json').write_text('{}')
        if trigger=='monitor_stale':
            load_supervisor.os.utime(args.monitor/'host-samples.jsonl',(time.time()-60,time.time()-60))
        if trigger=='run_deadline':args.timeout=-1
        return process
    monkeypatch.setattr(load_supervisor.asyncio,'create_subprocess_exec',spawn)
    result=await load_supervisor.supervise(args,[])
    assert result['abort_reason']==trigger and not result['passed']
    assert actions.index(signal.SIGSTOP)<actions.index('http-stop')<actions.index(signal.SIGKILL)
    assert signal.SIGCONT not in actions
    assert json.loads((args.output/'supervisor.json').read_text())['exit_code']==-9


async def test_monitor_disappearance_still_writes_failure_receipt(environment,monkeypatch):
    args,actions,process=environment
    async def spawn(*cmd,**kwargs):
        (args.monitor/'host-samples.jsonl').unlink();return process
    monkeypatch.setattr(load_supervisor.asyncio,'create_subprocess_exec',spawn)
    result=await load_supervisor.supervise(args,[])
    assert not result['passed']
    assert (args.output/'supervisor.json').exists()
    assert process.returncode is not None


async def test_exit_zero_without_client_receipt_is_not_pass(environment,monkeypatch):
    args,actions,process=environment;process.returncode=0
    async def spawn(*cmd,**kwargs):return process
    monkeypatch.setattr(load_supervisor.asyncio,'create_subprocess_exec',spawn)
    result=await load_supervisor.supervise(args,[])
    assert not result['passed']


@pytest.mark.parametrize('override',['--dsn=postgresql://127.0.0.1/other','--base=http://127.0.0.1:9999'])
async def test_forwarded_connection_override_is_rejected(environment,monkeypatch,override):
    args,_,_=environment
    async def spawn(*cmd,**kwargs):raise AssertionError('must not launch')
    monkeypatch.setattr(load_supervisor.asyncio,'create_subprocess_exec',spawn)
    with pytest.raises(ValueError):await load_supervisor.supervise(args,[override])


@pytest.mark.parametrize('pending',[True,False])
async def test_exit_receipt_retains_remote_effects_boundary(environment,monkeypatch,pending):
    args,actions,process=environment;process.returncode=0
    async def spawn(*cmd,**kwargs):
        child=args.output/'client';child.mkdir()
        (child/'summary.json').write_text(json.dumps(dict(client_checks_passed=True,passed=not pending,remote_effects_pending=pending)))
        return process
    monkeypatch.setattr(load_supervisor.asyncio,'create_subprocess_exec',spawn)
    result=await load_supervisor.supervise(args,[])
    assert result['client_checks_passed']
    assert result['passed']==(not pending)
    assert result['remote_effects_pending']==pending


async def test_stop_request_failure_still_kills_frozen_sender_and_records(environment,monkeypatch):
    args,actions,process=environment
    async def spawn(*cmd,**kwargs):
        (args.monitor/'STOP.json').write_text('{}');return process
    async def stop(*args):raise TimeoutError('fixture timeout')
    monkeypatch.setattr(load_supervisor.asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(load_supervisor,'stop_fixture_tasks',stop)
    result=await load_supervisor.supervise(args,[])
    assert result['stopped_tasks']==[{'error':'TimeoutError'}]
    assert process.returncode==-9 and signal.SIGCONT not in actions
