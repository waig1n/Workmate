"""Content-addressed downloads pin a verified private snapshot."""
import hashlib
import os
import pytest
from workmate_product.application.artifact_objects import snapshot_artifact, ArtifactObjectUnavailable


def publish(root, content=b'version one'):
    digest=hashlib.sha256(content).hexdigest()
    key=f'.objects/sha256/{digest[:2]}/{digest}'
    path=root/key
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return key,digest,path


def test_snapshot_preserves_published_version_and_cleans_up(tmp_path):
    key,digest,path=publish(tmp_path)
    snapshot=snapshot_artifact(tmp_path,key,digest,11)
    try:
        path.write_bytes(b'version two')
        assert snapshot.path.read_bytes()==b'version one'
    finally:
        snapshot.close()
    assert not snapshot.path.exists()


@pytest.mark.parametrize('key,digest,size', [('../secret','a'*64,1),('',None,0),
    ('.objects/sha256/aa/'+'a'*64,'b'*64,1),('.objects/sha256/aa/'+'a'*64,'a'*64,-1)])
def test_invalid_descriptor(tmp_path,key,digest,size):
    with pytest.raises(ValueError): snapshot_artifact(tmp_path,key,digest,size)


@pytest.mark.parametrize('kind',['missing','corrupt','size','symlink','directory','fifo','parent_link'])
def test_unavailable_objects_fail_closed(tmp_path,kind):
    key,digest,path=publish(tmp_path)
    if kind=='missing': path.unlink()
    elif kind=='corrupt': path.write_bytes(b'wrong bytes')
    elif kind=='size': path.write_bytes(b'longer than original bytes')
    elif kind=='symlink':
        target=tmp_path/'secret'; target.write_bytes(b'version one')
        path.unlink(); path.symlink_to(target)
    elif kind=='directory': path.unlink(); path.mkdir()
    elif kind=='fifo': path.unlink(); os.mkfifo(path)
    elif kind=='parent_link':
        parent=path.parent; moved=parent.with_name('moved'); parent.rename(moved); parent.symlink_to(moved)
    with pytest.raises(ArtifactObjectUnavailable): snapshot_artifact(tmp_path,key,digest,11)


def test_empty_artifact(tmp_path):
    key,digest,_=publish(tmp_path,b'')
    snapshot=snapshot_artifact(tmp_path,key,digest,0)
    try: assert snapshot.path.read_bytes()==b''
    finally: snapshot.close()


async def test_response_send_failure_cleans_snapshot(tmp_path):
    from app.api.routers.artifacts import SnapshotFileResponse
    key,digest,_=publish(tmp_path)
    snapshot=snapshot_artifact(tmp_path,key,digest,11)
    response=SnapshotFileResponse(snapshot)
    async def send(message): raise RuntimeError('disconnected')
    with pytest.raises(RuntimeError,match='disconnected'):
        await response({'type':'http','method':'GET','headers':[]},None,send)
    assert not snapshot.path.exists()


async def test_cancelled_copy_joins_and_cleans_snapshot(tmp_path,monkeypatch):
    import asyncio
    import threading
    import app.api.routers.artifacts as route
    key,digest,_=publish(tmp_path)
    entered=threading.Event(); release=threading.Event(); snapshots=[]
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        result=snapshot_artifact(*args)
        snapshots.append(result)
        return result
    monkeypatch.setattr(route,'snapshot_artifact',blocked)
    request=asyncio.create_task(route._snapshot(tmp_path,key,digest,11))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set(): await asyncio.sleep(.001)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError): await request
    assert len(snapshots)==1 and not snapshots[0].path.exists()
