"""Durable upload ledger and explicit hand-off; Runtime owns index state."""
import uuid
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select

from ..adapter.errors import RuntimeError as RuntimeFailure
from ..adapter.knowledge_client import segment
from workmate_product.persistence.models import KnowledgeDocumentUpload
from . import resource_sharing
from .knowledge import runtime_call


def upload_view(row):
    age = (datetime.now(timezone.utc) - row.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
    waiting = row.state == 'uploading' and age < 120
    return dict(id=row.id,filename=row.filename,content_type=row.content_type,size=row.size,
                status='uploading' if waiting else 'upload_failed',chunk_count=0,created_at=row.created_at,updated_at=row.updated_at,
                error=None if waiting else 'Original upload did not complete; delete it and upload again')


async def rows_for(db,kb_id):
    return list((await db.scalars(select(KnowledgeDocumentUpload).where(
        KnowledgeDocumentUpload.knowledge_base_id==kb_id).order_by(KnowledgeDocumentUpload.created_at.desc()))).all())


async def list_documents(db,client,kb):
    native = await runtime_call(client.knowledge_request('GET',kb.runtime_owner_id,kb.runtime_id,suffix='/documents'))
    items = {item['id']:item for item in native['items']}
    for row in await rows_for(db,kb.id):
        if row.id in items:
            continue
        try:
            item = await client.knowledge_request('GET',kb.runtime_owner_id,kb.runtime_id,suffix=f'/staged/{segment(row.id)}')
        except RuntimeFailure as exc:
            if exc.status_code != 404:
                raise HTTPException(502,'Document upload state could not be checked; retry later') from exc
            item = upload_view(row)
        if item.get('status') == 'upload_failed' and upload_view(row)['status'] == 'uploading':
            item = upload_view(row)
        items[row.id] = item
    return {'items':list(items.values()),'total':len(items)}


async def upload(db,client,service,kb,filename,content_type,content):
    # Persist identity BEFORE network I/O: a lost response can be reconciled
    # from the Runtime manifest by the next list request.
    row = KnowledgeDocumentUpload(id=str(uuid.uuid4()),knowledge_base_id=kb.id,
        filename=filename,content_type=content_type,size=len(content),state='uploading',created_by=service.user_id)
    doc_id, kb_id = row.id,kb.id
    db.add(row)
    await db.commit()
    # Serialize with start/document deletion/KB deletion after committing the
    # durable reservation. A deletion may have won this intervening window.
    await resource_sharing.lock_sharing(db)
    row = await db.get(KnowledgeDocumentUpload,doc_id,populate_existing=True)
    if row is None:
        raise HTTPException(409,'Upload was deleted before it completed')
    kb = await service.get(kb_id,manage=True)
    try:
        result = await runtime_call(client.knowledge_upload(kb.runtime_owner_id,kb.runtime_id,filename,content_type,content,doc_id))
    except HTTPException:
        row.state = 'upload_failed'
        await db.commit()
        raise
    if result.get('id') != doc_id or result.get('status') != 'uploaded':
        raise HTTPException(502,'Original upload could not be verified')
    row.state = 'uploaded'
    service.audit('document_upload',kb,document_id=doc_id)
    await db.commit()
    return result


async def start(db,client,service,kb,doc_id):
    # The caller holds the Product mutation lock; Runtime additionally holds
    # a Postgres advisory lock, including after a Product HTTP timeout.
    result = await runtime_call(client.knowledge_request('POST',kb.runtime_owner_id,kb.runtime_id,
        suffix=f'/documents/{segment(doc_id)}/start'))
    row = await db.get(KnowledgeDocumentUpload,doc_id)
    if row is not None and row.knowledge_base_id == kb.id:
        first = row.state != 'submitted'
        row.state = 'submitted'
        if first:
            service.audit('document_start',kb,document_id=doc_id)
    await db.commit()
    return result


async def delete(db,client,service,kb,doc_id):
    try:
        await client.knowledge_request('DELETE',kb.runtime_owner_id,kb.runtime_id,suffix=f'/documents/{segment(doc_id)}')
    except RuntimeFailure as exc:
        if exc.status_code != 404:
            raise HTTPException(exc.status_code if exc.status_code==409 else 502,'Document deletion failed; retry later') from exc
    row = await db.get(KnowledgeDocumentUpload,doc_id)
    if row is not None and row.knowledge_base_id == kb.id:
        await db.delete(row)
    service.audit('document_delete',kb,document_id=doc_id)
