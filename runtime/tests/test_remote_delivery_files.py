import asyncio
from types import SimpleNamespace
import pytest
from workmate_runtime.delivery.remote_files import RemoteDeliveryFiles
from workmate_runtime.delivery.files import DeliveryFileError
from workmate_runtime.sandbox.base import TrustedSandboxControlBackend

class Control(TrustedSandboxControlBackend):
    async def exec_shell(self,command,**kwargs):
        process=await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await process.communicate()
        return SimpleNamespace(exit_code=process.returncode,stdout=out,stderr=err)

async def test_remote_baseline_and_capture_publish_without_task_scope(tmp_path):
    root=tmp_path/'workspace'
    (root/'output').mkdir(parents=True)
    files=RemoteDeliveryFiles(Control(),str(root),tmp_path/'objects',max_file_bytes=1024,max_files=2)
    baseline=await files.baseline()
    (root/'output'/'result.txt').write_bytes(b'remote artifact')
    payload=await files.capture(baseline)
    assert payload['files'][0]['name']=='result.txt'
    assert (tmp_path/'objects'/payload['files'][0]['storage_key']).read_bytes()==b'remote artifact'

async def test_remote_capture_rejects_symlink(tmp_path):
    root=tmp_path/'workspace'
    (root/'output').mkdir(parents=True)
    (root/'output'/'escape').symlink_to('/etc/passwd')
    files=RemoteDeliveryFiles(Control(),str(root),tmp_path/'objects',max_file_bytes=1024,max_files=2)
    with pytest.raises(DeliveryFileError):
        await files.baseline()

def test_remote_capture_requires_trusted_control(tmp_path):
    with pytest.raises(TypeError):
        RemoteDeliveryFiles(object(),'/workspace',tmp_path,max_file_bytes=1024,max_files=2)


async def test_remote_explicit_selection_preserves_size_and_count_limits(tmp_path):
    import json
    root=tmp_path/'workspace'
    (root/'output').mkdir(parents=True)
    (root/'.workmate').mkdir()
    files=RemoteDeliveryFiles(Control(),str(root),tmp_path/'objects',max_file_bytes=8,max_files=2)
    baseline=await files.baseline()
    for name,content in [('a',b'ok'),('b',b'too large artifact'),('c',b'extra')]:
        (root/'output'/name).write_bytes(content)
    (root/'.workmate'/'presented_files.json').write_text(json.dumps({'version':1,'call_id':'new','files':['a','b','c']}))
    payload=await files.capture(baseline)
    assert payload['source']=='explicit'
    assert [f['name'] for f in payload['files']]==['a']
    assert any('size limit' in warning for warning in payload['warnings'])
    assert any('per-run limit' in warning for warning in payload['warnings'])


async def test_remote_capture_rejects_file_changed_after_inventory(tmp_path):
    root=tmp_path/'workspace'
    (root/'output').mkdir(parents=True)
    files=RemoteDeliveryFiles(Control(),str(root),tmp_path/'objects',max_file_bytes=1024,max_files=2)
    baseline=await files.baseline()
    target=root/'output'/'result'
    target.write_bytes(b'before')
    request=files._request
    async def mutate(action,name=''):
        if action=='read':
            target.write_bytes(b'changed')
        return await request(action,name)
    files._request=mutate
    with pytest.raises(DeliveryFileError,match='changed after inventory'):
        await files.capture(baseline)


@pytest.mark.parametrize('checkpoint_enabled', [True, False])
async def test_coordinator_remote_capture_uses_control_after_seal(tmp_path,monkeypatch,checkpoint_enabled):
    from contextlib import asynccontextmanager
    from workmate_runtime.delivery.coordinator import DeliveryCoordinator
    from workmate_runtime.execution.resources import TaskResourceRegistry
    from workmate_runtime.transport.delivery import DeliveryConflict
    from test_worker_workspaces import OWNER
    root=tmp_path/'workspace'
    (root/'output').mkdir(parents=True)
    persisted={}
    class Store:
        def __init__(self,db): pass
        async def record_baseline(self,owner,node,baseline): persisted['baseline']=baseline
        async def load_baseline(self,owner,node): return persisted['baseline']
        async def record_seal(self,owner,node,**kwargs): return kwargs['payload']
    class Connection:
        async def execute(self,*args): pass
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Connection()
    monkeypatch.setattr('workmate_runtime.delivery.coordinator.DeliveryStore',Store)
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    from unittest.mock import AsyncMock
    checkpoints = SimpleNamespace(capture=AsyncMock())
    coordinator=DeliveryCoordinator(Pool(),'node',tmp_path/'objects',registry,max_file_bytes=1024,max_files=2,checkpoints=checkpoints if checkpoint_enabled else None)
    control=Control()
    binding=SimpleNamespace(owner=OWNER,workspace=SimpleNamespace(host_workdir=None,
        workdir=str(root),get_backend=lambda: control))
    await coordinator.prepare(OWNER,binding)
    with pytest.raises(DeliveryConflict):
        await coordinator.seal_terminal(OWNER,'reply','completed',{})
    (root/'output'/'final').write_bytes(b'finished')
    registry._scope(OWNER).sealed=True
    payload=await coordinator.seal_terminal(OWNER,'reply','completed',{})
    assert payload['files'][0]['name']=='final'
    assert registry._scope(OWNER).sealed
    if checkpoint_enabled:
        checkpoints.capture.assert_awaited_once_with(OWNER, binding.workspace, boundary='terminal')
    else:
        checkpoints.capture.assert_not_awaited()
        assert not coordinator._checkpoint_workspaces
