import pytest
from agentscope.state import AgentState
from agentscope.tool import LocalBackend
from workmate_runtime.workspaces import WorkmateSandboxWrite


async def test_remote_evidence_reads_backend_and_invalidates_changed_bytes():
    backend=LocalBackend()
    async def read(path):
        assert path=='/workspace/working/a.txt'
        return b'original'
    backend.read_file=read
    tool=WorkmateSandboxWrite(backend=backend,sandbox_workdir='/workspace',host_workdir=None)
    state=AgentState()
    path=tool._cache_path('/workspace/working/a.txt')
    assert path=='/workspace/working/a.txt'
    tool._remember_read(state,path,b'original')
    assert await tool._has_current_read(state,path)
    async def changed(path): return b'changed'
    backend.read_file=changed
    assert not await tool._has_current_read(state,path)
    assert not state.middle_context['workmate_sandbox_read_evidence']


@pytest.mark.parametrize('path',['/etc/passwd','/workspace/../etc/passwd','relative.txt','/workspace'])
def test_remote_cache_rejects_outside_workspace(path):
    tool=WorkmateSandboxWrite(backend=LocalBackend(),sandbox_workdir='/workspace',host_workdir=None)
    assert tool._cache_path(path) is None
