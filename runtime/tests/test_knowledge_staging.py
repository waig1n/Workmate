"""Uploads are invisible to upstream workers until explicit admission."""
import asyncio
import io
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from workmate_runtime.knowledge_staging import KnowledgeStaging


class Blobs:
    def __init__(self):
        self.items = {}
    async def write_stream(self, key, stream):
        uri = 's3://bucket/' + key
        self.items[uri] = stream.read()
        return uri
    async def exists(self, uri):
        return uri in self.items
    async def size(self, uri):
        return len(self.items[uri]) if uri in self.items else None
    async def delete(self, uri):
        self.items.pop(uri, None)
    @asynccontextmanager
    async def open(self, uri):
        data = io.BytesIO(self.items[uri])
        yield SimpleNamespace(read=AsyncMock(side_effect=data.read))


def setup():
    records = {}
    async def save(owner, record):
        await asyncio.sleep(0)
        records[record.id] = record
        return record
    storage = SimpleNamespace(get_knowledge_document=AsyncMock(side_effect=lambda owner,kb,doc: records.get(doc)),
                              upsert_knowledge_document=AsyncMock(side_effect=save))
    service = SimpleNamespace(_require_edit=AsyncMock(return_value='owner'),
                              _access=SimpleNamespace(resolve_knowledge_base=AsyncMock()),
                              delete_document=AsyncMock())
    runtime = SimpleNamespace(storage=storage,blob_store=Blobs(),message_bus=object(),
                              service=service, mutation_lock=asyncio.Lock(),
                              settings=SimpleNamespace(knowledge_s3_bucket='bucket'))
    return KnowledgeStaging(runtime), records, storage


async def test_stage_never_registers_and_explicit_start_is_idempotent(monkeypatch):
    staging, records, storage = setup()
    enqueue = AsyncMock()
    monkeypatch.setattr('workmate_runtime.knowledge_staging.enqueue_index_task', enqueue)
    result = await staging.upload('owner','kb','doc','a.pdf','application/pdf',b'original')
    assert result['status']=='uploaded'
    assert not records
    storage.upsert_knowledge_document.assert_not_called()
    enqueue.assert_not_called()
    first, second = await asyncio.gather(staging.start('owner','kb','doc'),staging.start('owner','kb','doc'))
    assert first.id == second.id == 'doc'
    assert first.status == 'pending'
    assert first.data.blob_uri == 's3://bucket/kb/kb/doc'
    assert storage.upsert_knowledge_document.await_count == 1
    assert enqueue.await_count == 1
    records['doc'].status = 'ready'
    assert (await staging.start('owner','kb','doc')).status == 'ready'
    assert storage.upsert_knowledge_document.await_count == 1


async def test_crash_after_blob_put_is_recoverable_and_content_immutable(monkeypatch):
    staging, _, _ = setup()
    original = staging.blobs.write_stream
    async def lost_response(key, stream):
        result = await original(key, stream)
        if not key.endswith('.json'):
            raise TimeoutError('response lost')
        return result
    staging.blobs.write_stream = lost_response
    with pytest.raises(TimeoutError):
        await staging.upload('owner','kb','doc','a.txt','text/plain',b'original')
    assert (await staging.view('owner','kb','doc'))['status'] == 'uploaded'
    with pytest.raises(HTTPException) as error:
        await staging.upload('owner','kb','doc','a.txt','text/plain',b'different')
    assert error.value.status_code == 409


async def test_queue_failure_leaves_one_recoverable_pending_record(monkeypatch):
    staging, records, storage = setup()
    monkeypatch.setattr('workmate_runtime.knowledge_staging.enqueue_index_task',AsyncMock(side_effect=TimeoutError()))
    await staging.upload('owner','kb','doc','a.txt','text/plain',b'original')
    with pytest.raises(TimeoutError):
        await staging.start('owner','kb','doc')
    assert records['doc'].status == 'pending'
    assert (await staging.start('owner','kb','doc')).status == 'pending'
    assert storage.upsert_knowledge_document.await_count == 1


async def test_partial_upload_cannot_start_and_staged_delete_is_idempotent():
    staging, _, storage = setup()
    await staging.upload('owner','kb','doc','a.txt','text/plain',b'original')
    await staging.blobs.delete('s3://bucket/kb/kb/doc')
    with pytest.raises(HTTPException) as error:
        await staging.start('owner','kb','doc')
    assert error.value.status_code == 409
    storage.upsert_knowledge_document.assert_not_called()
    await staging.delete('owner','kb','doc')
    await staging.delete('owner','kb','doc')
    assert not staging.blobs.items


async def test_postgres_advisory_transaction_serializes_different_runtime_instances(monkeypatch):
    from workmate_runtime.knowledge_staging import knowledge_mutation
    staging, records, storage = setup()
    lock=asyncio.Lock()
    statements=[]
    class Session:
        acquired = False
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            pass
        @asynccontextmanager
        async def begin(self):
            try:
                yield
            finally:
                if self.acquired:
                    lock.release()
        async def execute(self,sql,params):
            statements.append((str(sql),params))
            self.acquired = not lock.locked()
            if self.acquired:
                await lock.acquire()
            return SimpleNamespace(scalar=lambda: self.acquired)
    storage._session_factory=Session
    second=KnowledgeStaging(SimpleNamespace(**{**vars(staging.runtime),'mutation_lock':asyncio.Lock()}))
    monkeypatch.setattr('workmate_runtime.knowledge_staging.enqueue_index_task',AsyncMock())
    await staging.upload('owner','kb','doc','a.txt','text/plain',b'a')
    await asyncio.gather(staging.start('owner','kb','doc'),second.start('owner','kb','doc'))
    assert storage.upsert_knowledge_document.await_count==1
    assert len(statements)>=3
    assert all('pg_try_advisory_xact_lock' in sql for sql,_ in statements)
    assert len({params['key'] for _,params in statements})==1
    assert not lock.locked()


async def test_delete_refuses_processing_and_keeps_original(monkeypatch):
    staging,records,_=setup()
    monkeypatch.setattr('workmate_runtime.knowledge_staging.enqueue_index_task',AsyncMock())
    await staging.upload('owner','kb','doc','a.txt','text/plain',b'a')
    await staging.start('owner','kb','doc')
    with pytest.raises(HTTPException) as error:
        await staging.delete('owner','kb','doc')
    assert error.value.status_code==409
    assert await staging.blobs.exists('s3://bucket/kb/kb/doc')
    assert await staging.blobs.exists('s3://bucket/knowledge-staged/kb/doc.json')


async def test_staged_original_stream_and_delete_without_upstream_record():
    staging,records,storage=setup()
    await staging.upload('owner','kb','doc','a.txt','text/plain',b'original')
    record,size,stream=await staging.content('owner','kb','doc')
    assert b''.join([chunk async for chunk in stream])==b'original'
    assert size==8 and record.data.filename=='a.txt'
    assert not records
    await staging.delete('owner','kb','doc')
    staging.runtime.service.delete_document.assert_not_called()
    assert not staging.blobs.items


async def test_advisory_contention_releases_connection_before_wait(monkeypatch):
    from workmate_runtime.knowledge_staging import knowledge_mutation
    staging,_,storage=setup()
    events=[]
    answers=iter([False,True])
    class Session:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
        async def __aenter__(self):
            events.append('open')
            return self
        async def __aexit__(self,*args):
            events.append('close')
        @asynccontextmanager
        async def begin(self):
            events.append('begin')
            try:
                yield
            finally:
                events.append('end')
        async def execute(self,*args):
            return SimpleNamespace(scalar=lambda: next(answers))
    storage._session_factory=Session
    async def wait(delay):
        assert events[-2:]==['end','close']
        events.append('wait')
    monkeypatch.setattr('workmate_runtime.knowledge_staging.asyncio.sleep',wait)
    async with knowledge_mutation(staging.runtime,'kb'):
        events.append('work')
    assert events==['open','begin','end','close','wait','open','begin','work','end','close']


async def test_library_delete_preflight_keeps_staged_objects_when_native_busy():
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from workmate_runtime.rag.management_router import create_management_router
    staging,records,storage=setup()
    await staging.upload('owner','kb','doc','a.txt','text/plain',b'original')
    storage.list_knowledge_documents=AsyncMock(return_value=[SimpleNamespace(id='native',status='indexing')])
    app=FastAPI()
    async def resolve():
        return staging.runtime
    app.include_router(create_management_router(resolve))
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        response=await client.request('DELETE','/workmate/knowledge/kb',headers={'X-User-ID':'owner'},json={'document_ids':['doc']})
    assert response.status_code==409,response.text
    assert await staging.blobs.exists('s3://bucket/kb/kb/doc')
    staging.runtime.service.delete_document.assert_not_called()
