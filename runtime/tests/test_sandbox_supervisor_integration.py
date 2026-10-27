"""Supervisor gates plus root-only real UID/process integration.

Root integration additionally requires its own PID namespace: never kill host
processes belonging to the supervisor's reserved UID during a developer test.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from workmate_runtime.sandbox import control_script, launch


@pytest.fixture
def controller(tmp_path,monkeypatch):
    directory=tmp_path/'control'
    directory.mkdir(mode=0o700)
    state=directory/'state.json'
    state.write_text(json.dumps({'active':False,'epoch':'','workdir':str(tmp_path)}))
    monkeypatch.setattr(control_script,'__file__',str(directory/'control_script.py'))
    pids=[]
    monkeypatch.setattr(control_script,'processes',lambda: list(pids))
    def operation(name,epoch='one'):
        monkeypatch.setattr(sys,'argv',['control_script.py',name,json.dumps({'epoch':epoch})])
        control_script.main()
        return json.loads(state.read_text())
    return operation,pids,directory


def test_epoch_gate_requires_cleanup_before_next_task(controller):
    operation,pids,_=controller
    assert operation('begin')['active']
    with pytest.raises(RuntimeError,match='Another Task'):
        operation('begin','two')
    with pytest.raises(RuntimeError,match='epoch differs'):
        operation('seal','two')
    assert operation('seal')['active'] is False
    with pytest.raises(RuntimeError,match='permanently sealed'):
        operation('begin')
    pids.append(1234)
    with pytest.raises(RuntimeError,match='not quiescent'):
        operation('begin','two')
    pids.clear()
    operation('kill')
    assert operation('begin','two')['epoch']=='two'


def test_cleanup_cannot_kill_active_or_different_epoch(controller,monkeypatch):
    operation,pids,_=controller
    operation('begin')
    monkeypatch.setattr(control_script.os,'kill',lambda *args: pytest.fail('Unsafe kill invoked'))
    with pytest.raises(RuntimeError,match='Seal exact execution'):
        operation('kill')
    operation('seal')
    with pytest.raises(RuntimeError,match='Seal exact execution'):
        operation('kill','other')


def test_sealed_or_old_epoch_launcher_never_spawns(controller,monkeypatch):
    operation,_,directory=controller
    monkeypatch.setattr(launch,'__file__',str(directory/'launch.py'))
    monkeypatch.setattr(launch.subprocess,'Popen',lambda *args,**kwargs: pytest.fail('Late command spawned'))
    def invoke(epoch):
        monkeypatch.setattr(sys,'argv',['launch.py','62000','--epoch',epoch,'--','true'])
        with pytest.raises(RuntimeError,match='sealed'):
            launch.main()
    invoke('one')
    operation('begin')
    invoke('old')
    operation('seal')
    invoke('one')


def test_demote_rejects_nonroot_and_unapproved_uid(monkeypatch):
    monkeypatch.setattr(launch.os,'geteuid',lambda: 1000)
    with pytest.raises(RuntimeError,match='root control'):
        launch.demote(62000)
    monkeypatch.setattr(launch.os,'geteuid',lambda: 0)
    with pytest.raises(RuntimeError,match='root control'):
        launch.demote(0)


_ROOT_RUNNER = r'''
import ctypes,json,os,pathlib,shutil,subprocess,sys,tempfile,threading,time
source=pathlib.Path(sys.argv[1])
base=pathlib.Path(tempfile.mkdtemp(prefix='workmate-supervisor-root-'))
base.chmod(0o755)
control=base/'control';control.mkdir(mode=0o700)
work=base/'work';work.mkdir(mode=0o770);os.chown(work,62000,62000)
for name in ('launch.py','control_script.py'):
    shutil.copyfile(source/name,control/name)
    (control/name).chmod(0o500)
(control/'state.json').write_text(json.dumps({'active':False,'epoch':'','workdir':str(work)}))
(control/'secret').write_text('root-only-test-fixture')
ctypes.CDLL(None).prctl(36,1,0,0,0) # subreaper for detached grandchildren

def op(name,epoch='one',ok=True):
    p=subprocess.run([sys.executable,str(control/'control_script.py'),name,json.dumps({'epoch':epoch})],capture_output=True,timeout=8)
    assert (p.returncode==0)==ok,(name,p.stderr)
    return p

def command(code,epoch='one',ok=True):
    p=subprocess.run([sys.executable,str(control/'launch.py'),'62000','--epoch',epoch,'--',sys.executable,'-I','-c',code],capture_output=True,timeout=8)
    assert (p.returncode==0)==ok,p.stderr
    return p
try:
    op('begin')
    result=command("import os,json;print(json.dumps({'uid':os.getresuid(),'gid':os.getgid(),'groups':os.getgroups(),'nnp':[l for l in open('/proc/self/status') if l.startswith('NoNewPrivs:')][0].split()[1]}))")
    identity=json.loads(result.stdout)
    assert identity=={'uid':[62000]*3,'gid':62000,'groups':[],'nnp':'1'},identity
    command('open('+repr(str(control/'secret'))+').read()',ok=False)
    op('begin','two',ok=False)
    # Parent returns while the grandchild creates a separate session and lives.
    command("import os,time;pid=os.fork();\nif pid==0:\n os.setsid();null=os.open('/dev/null',os.O_RDWR);os.dup2(null,0);os.dup2(null,1);os.dup2(null,2);open('daemon.pid','w').write(str(os.getpid()));time.sleep(60)\nelse:\n os._exit(0)")
    deadline=time.monotonic()+3
    while not (work/'daemon.pid').exists():
        assert time.monotonic()<deadline
        time.sleep(.01)
    daemon=int((work/'daemon.pid').read_text())
    reaped=[]
    def reap():
        reaped.append(os.waitpid(daemon,0))
    thread=threading.Thread(target=reap);thread.start()
    op('seal')
    op('begin',ok=False)
    command("open('late','w').write('bad')",ok=False)
    assert not (work/'late').exists()
    op('begin','two',ok=False)
    op('kill')
    op('begin',ok=False)
    thread.join(3);assert reaped and not thread.is_alive()
    op('begin','two')
    command("open('next','w').write('ok')",epoch='two')
    assert (work/'next').read_text()=='ok'
    command("open('old','w').write('bad')",epoch='one',ok=False)
    op('seal','two');op('kill','two')
    print(json.dumps({'uid':identity,'daemon_cleanup':True,'root_secret_denied':True,'late_launch_denied':True,'sealed_epoch_denied':True,'next_epoch':True}))
finally:
    shutil.rmtree(base)
'''


@pytest.mark.skipif(os.geteuid()!=0,reason='Requires root and a private PID namespace; UID isolation not simulated')
def test_root_uid_nnp_secret_and_setsid_daemon_lifecycle():
    unshare=shutil.which('unshare')
    if unshare is None:
        pytest.skip('unshare is required to avoid touching host execution UIDs')
    probe=subprocess.run([unshare,'--pid','--fork','--mount-proc','true'],capture_output=True,timeout=5)
    if probe.returncode:
        pytest.skip('Root environment cannot create an isolated PID namespace')
    result=subprocess.run([unshare,'--pid','--fork','--mount-proc',sys.executable,'-c',_ROOT_RUNNER,
        str(Path(launch.__file__).parent)],capture_output=True,timeout=40)
    assert result.returncode==0,result.stderr.decode()


@pytest.mark.skipif(not os.environ.get('WORKMATE_TEST_SUPERVISOR_IMAGE'),reason='Opt in with an existing local Docker image containing Python')
def test_supervisor_in_disposable_docker_container(tmp_path):
    import uuid
    image=os.environ['WORKMATE_TEST_SUPERVISOR_IMAGE']
    runner=tmp_path/'root-runner.py'
    runner.write_text(_ROOT_RUNNER)
    name='workmate-supervisor-test-'+uuid.uuid4().hex
    try:
        result=subprocess.run(['docker','run','--rm','--pull','never','--name',name,
            '--network','none','--init','--user','0:0','--read-only',
            '--tmpfs','/tmp:rw,exec,nosuid,size=64m',
            '--mount',f'type=bind,src={Path(launch.__file__).parent},dst=/supervisor,readonly',
            '--mount',f'type=bind,src={runner},dst=/root-runner.py,readonly',
            '--entrypoint','python3',image,'/root-runner.py','/supervisor'],
            capture_output=True,timeout=40)
        assert result.returncode==0,result.stderr.decode()
        evidence=json.loads(result.stdout)
        assert evidence['uid']['uid']==[62000]*3
        assert all(evidence[key] is True for key in ('daemon_cleanup','root_secret_denied',
            'late_launch_denied','sealed_epoch_denied','next_epoch'))
    finally:
        # Covers an interrupted Docker client; only this randomly named test container.
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=10)
