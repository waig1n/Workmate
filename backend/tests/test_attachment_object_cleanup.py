import asyncio
import threading

import pytest

from app.attachments import publish_attachment,cleanup_failed_upload


async def test_cancellation_joins_publish_then_cleans_only_uncommitted_keys(tmp_path):
    started,finish = threading.Event(),threading.Event()
    objects = {}
    class Store:
        def publish(self,key,content):
            started.set()
            assert finish.wait(5)
            objects[key] = content
        def delete(self,key): objects.pop(key,None)
    class Database:
        async def rollback(self): pass
        async def get(self,*args): return None
    store = Store()
    task = asyncio.create_task(publish_attachment(store,'key',b'bytes'))
    await asyncio.to_thread(started.wait,5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert objects == {'key': b'bytes'}
    await cleanup_failed_upload(Database(),'task',tmp_path/'staging',store,['key'])
    assert objects == {}


async def test_cleanup_keeps_committed_input_objects(tmp_path):
    class Store:
        def delete(self,key): raise AssertionError('Committed input must survive')
    class Database:
        async def rollback(self): pass
        async def get(self,*args): return object()
    staging = tmp_path/'staging'
    staging.mkdir()
    await cleanup_failed_upload(Database(),'task',staging,Store(),['key'])
    assert not staging.exists()


@pytest.mark.parametrize('failure', ['publication', 'cancelled'])
@pytest.mark.parametrize('cleanup_failure', ['delete', 'database'])
async def test_upload_failure_survives_failed_s3_cleanup(tmp_path, monkeypatch, caplog, failure, cleanup_failure):
    import io
    from types import SimpleNamespace
    import pytest
    from fastapi import UploadFile, HTTPException
    from app.api.routers import tasks
    from workmate_product.persistence.models import Conversation, Task
    from workmate_product.application.artifact_objects import ArtifactObjectUnavailable
    from workmate_product import artifact_storage
    original = (ArtifactObjectUnavailable('Product object publication failed')
                if failure == 'publication' else asyncio.CancelledError())
    deleted = []
    class Store:
        def publish(self, key, content): raise original
        def delete(self, key):
            deleted.append(key)
            raise TimeoutError('private endpoint detail')
    class Database:
        async def get(self, model, identifier):
            if model is Conversation:
                return SimpleNamespace(id='conv',user_id='owner',agent_id='agent',deleted_at=None)
            assert model is Task
            if cleanup_failure == 'database': raise TimeoutError('private endpoint detail')
            return None
        async def rollback(self): pass
    class Access:
        async def can_use_agent(self,*args): return True
        async def unavailable_capability_ids(self,*args): return []
    async def security(*args): pass
    monkeypatch.setattr(artifact_storage,'artifact_store_from_settings',lambda _:Store())
    monkeypatch.setattr(tasks,'attachment_task_dir',lambda cid,tid:str(tmp_path/tid))
    with pytest.raises(HTTPException if failure == 'publication' else asyncio.CancelledError) as caught:
        await tasks.create_task_with_files('conv',SimpleNamespace(),message='upload',title=None,
            approval_profile='standard',files=[UploadFile(io.BytesIO(b'bytes'),filename='report.txt')],
            user=SimpleNamespace(id='owner'),db=Database(),access=Access(),security_check=security)
    if failure == 'publication':
        assert caught.value.status_code==502
        assert caught.value.__cause__ is original
    else:
        assert isinstance(caught.value, asyncio.CancelledError)
    if cleanup_failure == 'delete':
        assert len(deleted)==1 and deleted[0].startswith('.inputs/conv/')
    else:
        assert not deleted, 'Uncertain commit status must never authorize object deletion'
    assert 'cleanup' in caplog.text.lower() and 'TimeoutError' in caplog.text
    assert 'private endpoint detail' not in caplog.text
    assert list(tmp_path.iterdir())==[]


async def test_cleanup_cancellation_is_propagated_after_join(tmp_path):
    import pytest
    started, finish=threading.Event(),threading.Event()
    class Store:
        def delete(self,key):
            started.set()
            assert finish.wait(5)
    class Database:
        async def rollback(self): pass
        async def get(self,*args): return None
    task=asyncio.create_task(cleanup_failed_upload(Database(),'task',tmp_path/'staging',Store(),['key']))
    await asyncio.to_thread(started.wait,5)
    task.cancel(); await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError): await task


async def test_cleanup_cancellation_survives_later_delete_failure(tmp_path):
    started, finish=threading.Event(),threading.Event()
    failure=TimeoutError('delete timed out after cancellation')
    class Store:
        def delete(self,key):
            started.set()
            assert finish.wait(5)
            raise failure
    class Database:
        async def rollback(self): pass
        async def get(self,*args): return None
    task=asyncio.create_task(cleanup_failed_upload(Database(),'task',tmp_path/'staging',Store(),['key']))
    await asyncio.to_thread(started.wait,5)
    task.cancel(); await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError) as caught: await task
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert caught.value.__cause__.args == failure.args
