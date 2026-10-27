"""Knowledge product API. Every runtime lookup follows parent-library authorization."""

from pathlib import PurePath
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...adapter.knowledge_client import segment
from ...adapter.errors import RuntimeError as RuntimeFailure
from ...auth import get_current_user
from ...db import get_db
from workmate_product.persistence.models import User
from ...services import resource_sharing, knowledge_documents
from ...services.knowledge import KnowledgeService, knowledge_settings, runtime_call
from ..pagination import ResourcePage
from ..knowledge_schemas import KnowledgeCreate, KnowledgeSearch, KnowledgeUpdate

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])
FORMATS = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def client(request):
    return request.app.state.runtime_client


@router.get("", response_model=ResourcePage[dict])
async def list_knowledge(
    agent_scope_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 12,
    q: Annotated[str, Query(max_length=200)] = "",
    view: Literal["all", "personal", "shared", "managed"] = "all",
):
    return await KnowledgeService(db, user.id).list(
        agent_scope_id, page=page, page_size=page_size, q=q, view=view,
    )

@router.post("", status_code=201)
async def create_knowledge(
    body: KnowledgeCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await KnowledgeService(db, user.id).create(body, client(request))


@router.get("/{kb_id}")
async def get_knowledge(
    kb_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = KnowledgeService(db, user.id)
    return await service.view(await service.get(kb_id))


@router.patch("/{kb_id}")
async def update_knowledge(
    kb_id: str,
    body: KnowledgeUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await resource_sharing.lock_sharing(db)
    service = KnowledgeService(db, user.id)
    kb = await service.get(kb_id, manage=True)
    changes = body.model_dump(exclude_none=True)
    runtime_changes = {
        key: value for key, value in changes.items() if key in ("name", "description")
    }
    if runtime_changes:
        await runtime_call(
            client(request).knowledge_request(
                "PATCH", kb.runtime_owner_id, kb.runtime_id, body=runtime_changes
            )
        )
    for key, value in changes.items():
        setattr(kb, key, value)
    service.audit("update", kb, fields=sorted(changes))
    await db.commit()
    return await service.view(kb)


@router.delete("/{kb_id}", status_code=204)
async def delete_knowledge(
    kb_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await resource_sharing.lock_sharing(db)
    service = KnowledgeService(db, user.id)
    kb = await service.get(kb_id, manage=True)
    if await service.has_bindings(kb.id):
        raise HTTPException(
            409, "Remove Agent bindings before deleting this knowledge base"
        )
    uploads = await knowledge_documents.rows_for(db,kb.id)
    # Runtime checks all native states and deletes under the SAME KB advisory
    # lock as start/upload. No staged data is removed before that preflight.
    try:
        await client(request).knowledge_request(
            "DELETE",kb.runtime_owner_id,kb.runtime_id,
            body={"document_ids":[row.id for row in uploads]},
        )
    except RuntimeFailure as exc:
        if exc.status_code != 404:
            raise HTTPException(exc.status_code if exc.status_code==409 else 502,"Knowledge deletion failed; retry later") from exc
    for row in uploads:
        await db.delete(row)
    await db.flush()
    service.audit("delete", kb)
    await db.delete(kb)
    await db.commit()
    return Response(status_code=204)


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await KnowledgeService(db, user.id).get(kb_id)
    return await knowledge_documents.list_documents(db,client(request),kb)


@router.post("/{kb_id}/documents", status_code=202)
async def upload_document(
    kb_id: str,
    request: Request,
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = KnowledgeService(db, user.id)
    kb = await service.get(kb_id, manage=True)
    filename = PurePath((file.filename or "").replace("\\", "/")).name
    extension = PurePath(filename).suffix.lower()
    if extension not in FORMATS:
        raise HTTPException(
            422, "Supported formats: TXT, Markdown, PDF, DOCX, PPTX, XLSX"
        )
    limit = knowledge_settings().max_upload_bytes
    content = await file.read(limit + 1)
    await file.close()
    if len(content) > limit:
        raise HTTPException(413, "Document exceeds configured upload size")
    if not content:
        raise HTTPException(422, "Document is empty")
    if len(filename) > 255:
        raise HTTPException(422,"Filename is too long")
    return await knowledge_documents.upload(db,client(request),service,kb,filename,FORMATS[extension],content)



@router.delete("/{kb_id}/documents/{document_id}", status_code=204)
async def delete_document(
    kb_id: str,
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await resource_sharing.lock_sharing(db)
    service = KnowledgeService(db,user.id)
    kb = await service.get(kb_id,manage=True)
    await knowledge_documents.delete(db,client(request),service,kb,document_id)
    await db.commit()
    return Response(status_code=204)


@router.post("/{kb_id}/documents/{document_id}/start", status_code=202)
async def start_document(kb_id: str, document_id: str, request: Request,
                         user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await resource_sharing.lock_sharing(db)
    service = KnowledgeService(db,user.id)
    kb = await service.get(kb_id,manage=True)
    return await knowledge_documents.start(db,client(request),service,kb,document_id)



@router.post("/{kb_id}/documents/{document_id}/retry", status_code=202)
async def retry_document(
    kb_id: str,
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await resource_sharing.lock_sharing(db)
    service = KnowledgeService(db, user.id)
    kb = await service.get(kb_id, manage=True)
    result = await runtime_call(
        client(request).knowledge_request(
            "POST",
            kb.runtime_owner_id,
            kb.runtime_id,
            suffix=f"/documents/{segment(document_id)}/retry",
        )
    )
    service.audit("document_retry", kb, document_id=document_id)
    await db.commit()
    return result


@router.get("/{kb_id}/documents/{document_id}/content")
async def document_content(
    kb_id: str,
    document_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await KnowledgeService(db, user.id).get(kb_id, use=True, download=True)
    content, content_type, disposition = await runtime_call(
        client(request).knowledge_content(
            kb.runtime_owner_id,
            kb.runtime_id,
            document_id,
            knowledge_settings().max_upload_bytes,
        )
    )
    # Never expose upstream URLs, cookies, or arbitrary active content.
    safe_types = {"application/pdf", "text/plain", "text/markdown"}
    media_type = content_type.split(";")[0]
    if media_type not in safe_types:
        media_type = "application/octet-stream"
    safe_name = quote(document_id, safe="")
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{kb_id}/search")
async def search(
    kb_id: str,
    body: KnowledgeSearch,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    service = KnowledgeService(db, user.id)
    kb = await service.get(kb_id, use=True)
    result = await runtime_call(
        client(request).knowledge_request(
            "POST",
            kb.runtime_owner_id,
            kb.runtime_id,
            suffix="/search",
            body={**body.model_dump(), "product_id": kb.id},
        )
    )
    service.audit("search", kb, source_count=len(result.get("sources", [])))
    await db.commit()
    return result
