"""Privileged bootstrap must not import modules from a persistent Agent directory."""
import subprocess
import sys
from pathlib import Path
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from workmate_runtime.sandbox.remote import RemoteSandboxProvider, establish_control
from workmate_runtime.sandbox.base import WorkspaceBindingError
from workmate_product.config.sandbox import DockerSettings


async def test_bootstrap_uses_trusted_cwd_and_ignores_agent_modules(tmp_path,monkeypatch):
    agent_directory=tmp_path/'agent'
    agent_directory.mkdir()
    marker=agent_directory/'untrusted-module-executed'
    for name in ('venv','pip'):
        (agent_directory/f'{name}.py').write_text(
            f'from pathlib import Path;Path({str(marker)!r}).write_text("executed")\n')
    real_run=subprocess.run
    checked=[]

    def run_without_installing(command,**kwargs):
        # Exercise the real interpreter's module lookup without package
        # installation, environment creation or network access.
        if '-m' in command:
            module=command[command.index('-m')+1]
            assert module in {'venv','pip'}
            checked.append(module)
            harmless=[sys.executable,*command[1:command.index('-m')+2],
                '--help' if module=='venv' else '--version']
            result=real_run(harmless,cwd=agent_directory,capture_output=True,text=True)
            assert not marker.exists(), result.stdout+result.stderr
            return SimpleNamespace(returncode=0)
        # Force missing bootstrap dependencies so both module commands run.
        assert '-c' in command
        return SimpleNamespace(returncode=1)

    class Control:
        directory=str(tmp_path/'trusted-control')
        python=sys.executable
        async def exec_shell(self,command,**kwargs):
            if command[0]==self.python:
                assert kwargs['cwd']=='/'
                with monkeypatch.context() as patch:
                    patch.setattr(subprocess,'run',run_without_installing)
                    patch.setattr('shutil.which',lambda _: '/usr/bin/rg')
                    patch.setattr(sys,'argv',['bootstrap',command[-1]])
                    # Deliberately harsher than the actual trusted cwd: even if
                    # cwd regresses, child interpreters must remain isolated.
                    patch.chdir(agent_directory)
                    exec(compile(command[command.index('-c')+1],'<bootstrap>','exec'),{})
            return SimpleNamespace(exit_code=0)
        async def write_file(self,*args):
            pass

    monkeypatch.setattr('workmate_runtime.sandbox.remote.establish_control',AsyncMock(return_value=Control()))
    monkeypatch.setattr('workmate_runtime.mcp.remote.start_remote_gateway',AsyncMock(return_value=object()))
    workspace=SimpleNamespace(_provision_backend=AsyncMock())
    await RemoteSandboxProvider(DockerSettings()).initialize_workspace(workspace)
    assert checked==['venv','pip']
    assert not marker.exists()
    assert workspace._workmate_initialized is True


@pytest.mark.parametrize('unsafe',['writable_ancestor','unowned_ancestor','inside_workspace'])
async def test_control_rejects_interpreter_replaceable_by_agent(monkeypatch,unsafe):
    executable='/sandbox/python' if unsafe=='inside_workspace' else '/trusted/bin/python'
    calls=[]
    class Raw:
        async def exec_shell(self,command,**kwargs):
            calls.append(command)
            if len(calls)==1:
                return SimpleNamespace(exit_code=0,stdout=('0\n'+executable+'\n').encode())
            assert len(calls)==2, 'Unsafe interpreter must fail before workspace setup'
            def ownership(path):
                return SimpleNamespace(
                    st_uid=1000 if unsafe=='unowned_ancestor' and str(path)=='/trusted' else 0,
                    st_mode=0o777 if unsafe=='writable_ancestor' and str(path)=='/trusted' else 0o755)
            with monkeypatch.context() as patch:
                patch.setattr(sys,'executable',executable)
                patch.setattr(sys,'argv',['check','/sandbox'])
                patch.setattr('os.geteuid',lambda:0)
                patch.setattr(Path,'stat',ownership)
                try:
                    exec(compile(command[command.index('-c')+1],'<interpreter-check>','exec'),{})
                except AssertionError:
                    return SimpleNamespace(exit_code=1,stdout=b'')
            return SimpleNamespace(exit_code=0,stdout=b'')
    raw=Raw()
    with pytest.raises(WorkspaceBindingError):
        await establish_control(SimpleNamespace(workdir='/sandbox',get_backend=lambda:raw))
    assert len(calls)==2
