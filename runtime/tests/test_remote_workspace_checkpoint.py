"""Real control-script subprocesses, bounded file transport and fail-closed restore."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
from workmate_runtime.delivery.remote_workspace_checkpoint import RemoteWorkspaceCheckpoint
from workmate_runtime.delivery.workspace_checkpoint import CheckpointError

class Control(TrustedSandboxControlBackend):
    def __init__(self,directory):self.directory=str(directory);directory.mkdir(exist_ok=True)
    async def exec_shell(self,argv,**kwargs):
        # Unit-process harness emulates the fixed dedicated UID using this test user.
        # Production helper is unchanged and always fchowns to 62000.
        argv=list(argv);argv[3]=argv[3].replace('os.fchown(fd,62000,62000)',f'os.fchown(fd,{os.getuid()},{os.getgid()})')
        child=await asyncio.create_subprocess_exec(*argv,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await child.communicate()
        return SimpleNamespace(exit_code=child.returncode,stdout=out,stderr=err)

class Store:
    def __init__(self,root):self.root=root;root.mkdir()
    def publish_file(self,key,source,digest,size):
        p=self.root/digest;p.write_bytes(source.read());return digest
    def snapshot(self,key,digest,size):
        return SimpleNamespace(path=self.root/digest,close=lambda:None)

def primitive(root,store,**kw):
    return RemoteWorkspaceCheckpoint(Control(root.parent/('control-'+root.name)),str(root),store,provider='docker',max_files=10,max_bytes=1000000,**kw)

async def test_roundtrip_empty_dirs_chunks_and_modes(tmp_path):
    root=tmp_path/'old';(root/'working'/'empty').mkdir(parents=True);(root/'working'/'large').write_bytes(b'a'*150000)
    (root/'working'/'large').chmod(0o700)
    store=Store(tmp_path/'objects');captured=await primitive(root,store).capture()
    fresh=tmp_path/'new';fresh.mkdir()
    await primitive(fresh,store).restore(captured)
    assert (fresh/'working'/'large').read_bytes()==b'a'*150000
    assert (fresh/'working'/'empty').is_dir()
    with pytest.raises(CheckpointError):await primitive(fresh,store).restore(captured)

async def test_symlink_and_limits_rejected(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'x').symlink_to('/etc/passwd')
    store=Store(tmp_path/'objects')
    with pytest.raises(CheckpointError):await primitive(root,store).capture()
    (root/'working'/'x').unlink();(root/'working'/'x').write_bytes(b'123')
    with pytest.raises(CheckpointError):await primitive(root,store,max_file_bytes=2).capture()

async def test_restore_corrupt_object_leaves_target_untouched(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'x').write_bytes(b'bytes')
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    (store.root/manifest['files'][0]['sha256']).write_bytes(b'bad')
    fresh=tmp_path/'new';fresh.mkdir()
    with pytest.raises(CheckpointError):await primitive(fresh,store).restore(manifest)
    assert list(fresh.iterdir())==[]

@pytest.mark.parametrize('provider',['bubblewrap','e2b','local'])
def test_allowlist(provider,tmp_path):
    with pytest.raises(ValueError):RemoteWorkspaceCheckpoint(Control(tmp_path/'control'),str(tmp_path),object(),provider=provider,max_files=1,max_bytes=1)

def test_task_backend_rejected(tmp_path):
    with pytest.raises(TypeError):RemoteWorkspaceCheckpoint(object(),str(tmp_path),object(),provider='docker',max_files=1,max_bytes=1)

async def test_changed_source_after_first_chunk_rejected(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);path=root/'working'/'x';path.write_bytes(b'a'*150000)
    store=Store(tmp_path/'objects');remote=primitive(root,store);request=remote._request
    async def changed(action,**payload):
        result=await request(action,**payload)
        if action=='read':path.write_bytes(b'b'*150000)
        return result
    remote._request=changed
    with pytest.raises(CheckpointError):await remote.capture()

async def test_partial_install_retains_marker_and_refuses_retry(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'x').write_bytes(b'a'*150000)
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    fresh=tmp_path/'new';fresh.mkdir();remote=primitive(fresh,store);request=remote._request
    async def fail(action,**payload):
        if action=='write' and payload['offset']>0:raise CheckpointError('controlled transport failure')
        return await request(action,**payload)
    remote._request=fail
    with pytest.raises(CheckpointError):await remote.restore(manifest)
    assert (Path(remote.control)/'.workmate-checkpoint-restore').exists()
    remote._request=request
    with pytest.raises(CheckpointError):await remote.restore(manifest)

async def test_empty_file_and_existing_empty_roots(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'empty').touch()
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    fresh=tmp_path/'new';(fresh/'working').mkdir(parents=True)
    await primitive(fresh,store).restore(manifest)
    assert (fresh/'working'/'empty').read_bytes()==b''

async def test_cancellation_joins_remote_operation(tmp_path):
    started=asyncio.Event();proceed=asyncio.Event();finished=asyncio.Event()
    class Delayed(Control):
        async def exec_shell(self,*args,**kwargs):
            started.set();await proceed.wait();finished.set()
            return SimpleNamespace(exit_code=0,stdout='{}')
    remote=RemoteWorkspaceCheckpoint(Delayed(tmp_path/'control'),str(tmp_path/'work'),object(),provider='docker',max_files=1,max_bytes=1)
    task=asyncio.create_task(remote._request('inventory'))
    await started.wait();task.cancel();await asyncio.sleep(.01)
    assert not task.done()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert finished.is_set()

async def test_finish_argument_is_bounded_digest(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True)
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    fresh=tmp_path/'new';fresh.mkdir();remote=primitive(fresh,store);request=remote._request;seen=[]
    async def observe(action,**payload):
        if action=='finish':seen.append(payload)
        return await request(action,**payload)
    remote._request=observe
    await remote.restore(manifest)
    assert len(seen[0]['manifest_digest'])==64 and 'manifest' not in seen[0]

async def test_special_file_rejected_without_blocking(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);os.mkfifo(root/'working'/'pipe')
    with pytest.raises(CheckpointError):await primitive(root,Store(tmp_path/'objects')).capture()

async def test_restore_refuses_nonempty_target_before_marker(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True)
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    fresh=tmp_path/'new';(fresh/'working').mkdir(parents=True);(fresh/'working'/'keep').write_text('do not replace')
    remote=primitive(fresh,store)
    with pytest.raises(CheckpointError):await remote.restore(manifest)
    assert (fresh/'working'/'keep').read_text()=='do not replace'
    assert not (Path(remote.control)/'.workmate-checkpoint-restore').exists()

async def test_invalid_control_response_rejected(tmp_path):
    class Invalid(Control):
        async def exec_shell(self,*args,**kwargs):return SimpleNamespace(exit_code=0,stdout='[]')
    remote=RemoteWorkspaceCheckpoint(Invalid(tmp_path/'control'),str(tmp_path/'work'),object(),provider='kubernetes',max_files=1,max_bytes=1)
    with pytest.raises(CheckpointError):await remote.capture()

async def test_corrupt_remote_chunk_rejected(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'x').write_bytes(b'hello')
    remote=primitive(root,Store(tmp_path/'objects'));request=remote._request
    async def corrupt(action,**payload):
        if action=='read':return {'data':'!!!!!'}
        return await request(action,**payload)
    remote._request=corrupt
    with pytest.raises(CheckpointError):await remote.capture()

async def test_restore_root_replacement_rejected_even_empty_manifest(tmp_path):
    root=tmp_path/'old';root.mkdir();store=Store(tmp_path/'objects')
    manifest=await primitive(root,store).capture()
    fresh=tmp_path/'new';fresh.mkdir();remote=primitive(fresh,store);request=remote._request
    async def replace(action,**payload):
        result=await request(action,**payload)
        if action=='begin':
            fresh.rename(tmp_path/'original-root');fresh.mkdir()
        return result
    remote._request=replace
    with pytest.raises(CheckpointError):await remote.restore(manifest)

async def test_capture_external_hardlink_rejected(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True)
    external=tmp_path/'outside';external.write_bytes(b'outside')
    os.link(external,root/'working'/'alias')
    with pytest.raises(CheckpointError):await primitive(root,Store(tmp_path/'objects')).capture()

async def test_restore_hardlink_created_before_finish_rejected(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'x').write_bytes(b'bytes')
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    fresh=tmp_path/'new';fresh.mkdir();remote=primitive(fresh,store);request=remote._request
    async def link(action,**payload):
        if action=='finish-file':os.link(fresh/'working'/'x',tmp_path/'outside')
        return await request(action,**payload)
    remote._request=link
    with pytest.raises(CheckpointError):await remote.restore(manifest)

async def test_read_rejects_new_external_hardlink(tmp_path):
    root=tmp_path/'old';(root/'working').mkdir(parents=True);(root/'working'/'x').write_bytes(b'bytes')
    remote=primitive(root,Store(tmp_path/'objects'));request=remote._request
    async def link(action,**payload):
        if action=='read':os.link(root/'working'/'x',tmp_path/'outside')
        return await request(action,**payload)
    remote._request=link
    with pytest.raises(CheckpointError):await remote.capture()

async def test_cancel_waits_for_late_snapshot_and_closes_it_once(tmp_path):
    import threading
    root=tmp_path/'old';(root/'working').mkdir(parents=True)
    (root/'working'/'x').write_bytes(b'verified bytes')
    store=Store(tmp_path/'objects');manifest=await primitive(root,store).capture()
    started=threading.Event();release=threading.Event();closed=[]
    def delayed_snapshot(key,digest,size):
        started.set()
        if not release.wait(timeout=5):raise AssertionError('Test did not release snapshot thread')
        return SimpleNamespace(path=store.root/digest,close=lambda:closed.append(digest))
    store.snapshot=delayed_snapshot
    fresh=tmp_path/'new';fresh.mkdir();remote=primitive(fresh,store)
    operations=[];request=remote._request
    async def observe(action,**payload):
        operations.append(action)
        return await request(action,**payload)
    remote._request=observe
    task=asyncio.create_task(remote.restore(manifest))
    try:
        async with asyncio.timeout(3):
            while not started.is_set():await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and closed==[] and operations==[]
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert closed==[manifest['files'][0]['sha256']]
        assert operations==[] and list(fresh.iterdir())==[]
        assert not (Path(remote.control)/'.workmate-checkpoint-restore').exists()
    finally:
        release.set()
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)
