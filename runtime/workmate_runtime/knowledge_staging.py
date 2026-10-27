"""S3-only document staging. Native pending records exist only after start.

Postgres advisory locks serialize admission/deletion across Runtime processes.
The process lock fallback supports SQLite tests only; it is not a distributed
production lock. Manifest-first immutable uploads survive a lost PUT response.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import hashlib
import io
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from agentscope.app.storage import KnowledgeDocumentData, KnowledgeDocumentRecord
from agentscope.app._bus_ops import enqueue_index_task


class StagedDeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_ids: list[str] = Field(default_factory=list)

    @field_validator('document_ids')
    @classmethod
    def valid_ids(cls, values):
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) for value in values):
            raise ValueError('Invalid staged document identity')
        return values


@asynccontextmanager
async def knowledge_mutation(runtime, kb_id):
    factory = getattr(runtime.storage, '_session_factory', None)
    if factory is not None:
        key = int.from_bytes(hashlib.sha256(('workmate-staging:' + kb_id).encode()).digest()[:8], 'big', signed=True)
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            async with factory() as session:
                if session.get_bind().dialect.name != 'postgresql':
                    break
                async with session.begin():
                    result = await session.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key':key})
                    if result.scalar():
                        yield
                        return
            # Release transaction AND pooled connection before waiting. Blocking
            # advisory acquisition here could exhaust the same pool needed by
            # the owner while it calls the native storage API.
            if asyncio.get_running_loop().time() >= deadline:
                raise HTTPException(409,'Knowledge mutation is busy; retry later')
            await asyncio.sleep(0.05)
    async with runtime.mutation_lock:
        yield


class KnowledgeStaging:
    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def blobs(self):
        return self.runtime.blob_store

    @property
    def storage(self):
        return self.runtime.storage

    def uri(self, kb_id, doc_id, *, manifest=False):
        if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) for value in (kb_id,doc_id)):
            raise HTTPException(422,'Invalid knowledge document identity')
        key = f'kb/{kb_id}/{doc_id}'
        if manifest:
            key = f'knowledge-staged/{kb_id}/{doc_id}.json'
        return f's3://{self.runtime.settings.knowledge_s3_bucket}/{key}'

    async def manifest(self, owner, kb_id, doc_id):
        uri = self.uri(kb_id,doc_id,manifest=True)
        if not await self.blobs.exists(uri):
            return None
        async with self.blobs.open(uri) as stream:
            value = json.loads(await stream.read())
        if (value.get('owner_id'),value.get('knowledge_base_id'),value.get('id')) != (owner,kb_id,doc_id):
            raise HTTPException(404,'Document not found')
        # Never trust a stored arbitrary URI to address another object.
        if value.get('blob_uri') != self.uri(kb_id,doc_id):
            raise HTTPException(409,'Staged document metadata is invalid')
        return value

    async def upload(self, owner, kb_id, doc_id, filename, content_type, content):
        async with knowledge_mutation(self.runtime,kb_id):
            await self.runtime.service._require_edit(owner,kb_id)
            existing = await self.storage.get_knowledge_document(owner,kb_id,doc_id)
            if existing is not None:
                raise HTTPException(409,'Document has already been submitted')
            digest = hashlib.sha256(content).hexdigest()
            meta = await self.manifest(owner,kb_id,doc_id)
            if meta is not None and (meta['sha256'],meta['filename'],meta['content_type']) != (digest,filename,content_type):
                raise HTTPException(409,'Staged document cannot be overwritten')
            if meta is None:
                now = datetime.now(timezone.utc).isoformat()
                meta = dict(id=doc_id,owner_id=owner,knowledge_base_id=kb_id,
                            filename=filename,content_type=content_type,size=len(content),
                            blob_uri=self.uri(kb_id,doc_id),sha256=digest,created_at=now,updated_at=now)
                await self.blobs.write_stream(f'knowledge-staged/{kb_id}/{doc_id}.json',io.BytesIO(json.dumps(meta).encode()))
            if not await self.blobs.exists(meta['blob_uri']):
                await self.blobs.write_stream(f'kb/{kb_id}/{doc_id}',io.BytesIO(content))
            return await self._view(meta)

    async def _view(self, meta):
        complete = await self.blobs.exists(meta['blob_uri']) and await self.blobs.size(meta['blob_uri']) == meta['size']
        return {**{key:meta[key] for key in ('id','filename','size','content_type','created_at','updated_at')},
                'status':'uploaded' if complete else 'upload_failed','chunk_count':0,
                'error':None if complete else 'Original upload is incomplete; delete it and upload again'}

    async def view(self, owner,kb_id,doc_id):
        await self.runtime.service._access.resolve_knowledge_base(owner,kb_id)
        record = await self.storage.get_knowledge_document(owner,kb_id,doc_id)
        if record is not None:
            from .rag.document_projection import document_projection
            return document_projection(record)
        meta = await self.manifest(owner,kb_id,doc_id)
        if meta is None:
            raise HTTPException(404,'Document not found')
        return await self._view(meta)

    async def start(self, owner,kb_id,doc_id):
        async with knowledge_mutation(self.runtime,kb_id):
            await self.runtime.service._require_edit(owner,kb_id)
            record = await self.storage.get_knowledge_document(owner,kb_id,doc_id)
            if record is not None:
                return record
            meta = await self.manifest(owner,kb_id,doc_id)
            if meta is None:
                raise HTTPException(404,'Document not found')
            if (await self._view(meta))['status'] != 'uploaded':
                raise HTTPException(409,'Original upload is incomplete')
            record = KnowledgeDocumentRecord(id=doc_id,user_id=owner,knowledge_base_id=kb_id,
                data=KnowledgeDocumentData(filename=meta['filename'],content_type=meta['content_type'],
                    size=meta['size'],blob_uri=meta['blob_uri']))
            record = await self.storage.upsert_knowledge_document(owner,record)
            # If enqueue fails, the native pending sweeper recovers this record.
            await enqueue_index_task(self.runtime.message_bus,user_id=owner,knowledge_base_id=kb_id,document_id=doc_id)
            return record

    async def delete(self, owner,kb_id,doc_id):
        async with knowledge_mutation(self.runtime,kb_id):
            await self.runtime.service._require_edit(owner,kb_id)
            record = await self.storage.get_knowledge_document(owner,kb_id,doc_id)
            if record is not None:
                if record.status not in {'ready','error'}:
                    raise HTTPException(409,'Document is still being processed')
                await self.runtime.service.delete_document(owner,kb_id,doc_id)
            await self.blobs.delete(self.uri(kb_id,doc_id))
            await self.blobs.delete(self.uri(kb_id,doc_id,manifest=True))

    async def content(self, owner,kb_id,doc_id):
        await self.runtime.service._access.resolve_knowledge_base(owner,kb_id)
        record = await self.storage.get_knowledge_document(owner,kb_id,doc_id)
        if record is not None:
            return await self.runtime.service.stream_document_content(owner,kb_id,doc_id)
        meta = await self.manifest(owner,kb_id,doc_id)
        if meta is None or (await self._view(meta))['status'] != 'uploaded':
            raise HTTPException(404,'Original document not available')
        record = KnowledgeDocumentRecord(id=doc_id,user_id=owner,knowledge_base_id=kb_id,
            data=KnowledgeDocumentData(filename=meta['filename'],content_type=meta['content_type'],size=meta['size'],blob_uri=meta['blob_uri']))
        async def stream():
            async with self.blobs.open(meta['blob_uri']) as source:
                while chunk := await source.read(1024*1024):
                    yield chunk
        return record,meta['size'],stream()
