from datetime import datetime,timedelta,timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from workmate_product.application import tool_content_gc as gc

pytestmark=pytest.mark.asyncio
KEY='.tool-content/sha256/aa/'+'a'*64

class Client:
    def __init__(self): self.deleted=[]
    def head_object(self,**kw): return dict(LastModified=datetime.now(timezone.utc)-timedelta(days=40))
    def delete_object(self,**kw): self.deleted.append(kw['Key'])

class Sessions:
    @asynccontextmanager
    async def begin(self): yield object()

class Redis:
    def __init__(self,live=False): self.live=live
    async def scan_iter(self,**kw):
        if self.live: yield 'live'

async def setup(monkeypatch,refs=()):
    async def nop(*args): pass
    async def referenced(*args): return set(refs)
    monkeypatch.setattr(gc.retention,'lock_object_gc',nop)
    monkeypatch.setattr(gc.retention,'assert_no_writers',nop)
    monkeypatch.setattr(gc,'referenced_keys',referenced)

async def test_requires_maintenance_confirmation(monkeypatch):
    await setup(monkeypatch)
    with pytest.raises(ValueError,match='writers'):
        await gc.delete_candidates(Sessions(),SimpleNamespace(client=Client(),bucket='owned'),Redis(),
            namespace='test',candidates=[dict(key=KEY)],writers_stopped=False)

async def test_live_worker_and_reference_preserved(monkeypatch):
    await setup(monkeypatch,refs=[KEY]); client=Client()
    with pytest.raises(ValueError,match='Worker'):
        await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(True),
            namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)
    result=await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
        namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)
    assert result==[] and client.deleted==[]

async def test_strict_namespace_age_and_delete(monkeypatch):
    await setup(monkeypatch); client=Client()
    with pytest.raises(ValueError):
        await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
            namespace='test',candidates=[dict(key='.objects/aa/'+'a'*64)],writers_stopped=True)
    result=await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
        namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)
    assert result==[KEY] and client.deleted==[KEY]

async def test_recent_unknown_and_missing_objects_not_deleted(monkeypatch):
    await setup(monkeypatch); client=Client()
    for head in ({},{'LastModified':datetime.now(timezone.utc)}):
        client.head_object=lambda **kw: head
        assert await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
            namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)==[]
    class Missing(Exception): response={'Error':{'Code':'NoSuchKey'}}
    def missing(**kw): raise Missing()
    client.head_object=missing
    assert await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
        namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)==[]
    assert not client.deleted

async def test_storage_cancellation_waits_for_sdk_completion():
    import asyncio
    import threading
    started=threading.Event(); release=threading.Event(); finished=threading.Event()
    def operation(): started.set(); release.wait(3); finished.set()
    running=asyncio.create_task(gc._storage_io(operation))
    while not started.is_set(): await asyncio.sleep(.01)
    running.cancel()
    await asyncio.sleep(.02)
    assert not running.done()
    release.set()
    with pytest.raises(asyncio.CancelledError): await running
    assert finished.is_set()

async def test_invalid_namespace_and_batch_rejected(monkeypatch):
    await setup(monkeypatch)
    with pytest.raises(ValueError,match='namespace'): await gc._no_worker(Redis(),'bad:*')
    with pytest.raises(ValueError,match='batch'):
        await gc.delete_candidates(Sessions(),SimpleNamespace(client=Client(),bucket='owned'),Redis(),
            namespace='test',candidates=[dict(key=KEY)]*2,writers_stopped=True,limit=1)

async def test_classification_preserves_references_and_recent_objects():
    old=datetime.now(timezone.utc)-timedelta(days=8)
    assert gc.classify([dict(Key=KEY,Size=1,LastModified=old)],{KEY})[0]['status']=='referenced'
    assert gc.classify([dict(Key=KEY,Size=1,LastModified=old)],set())[0]['status']=='orphan_candidate'
    assert gc.classify([dict(Key=KEY,Size=1)],set())[0]['status']=='age_unknown'
    assert gc.classify([dict(Key='.inputs/x',Size=1)],set())==[]
    with pytest.raises(ValueError): gc.classify([],set(),grace_days=0)

async def test_second_reference_check_prevents_delete(monkeypatch):
    await setup(monkeypatch); client=Client(); reads=0
    async def references(*args):
        nonlocal reads
        reads+=1
        return set() if reads==1 else {KEY}
    monkeypatch.setattr(gc,'referenced_keys',references)
    assert await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
        namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)==[]
    assert not client.deleted

async def test_storage_errors_fail_closed(monkeypatch):
    await setup(monkeypatch);client=Client()
    def unavailable(**kw): raise OSError('unavailable')
    client.head_object=unavailable
    with pytest.raises(OSError):
        await gc.delete_candidates(Sessions(),SimpleNamespace(client=client,bucket='owned'),Redis(),
            namespace='test',candidates=[dict(key=KEY)],writers_stopped=True)
    assert not client.deleted
