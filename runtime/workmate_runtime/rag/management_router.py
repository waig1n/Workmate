"""Knowledge management HTTP surface, independent of legacy Session execution."""
import asyncio
import hashlib
import os
from pathlib import PurePosixPath
from urllib.parse import quote
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from agentscope.app.storage import ChunkerConfig, EmbeddingModelConfig
from ..knowledge_staging import KnowledgeStaging, StagedDeleteRequest, knowledge_mutation
from .management import KnowledgeManagement
from .document_projection import document_projection
from .sources import source_projection

MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateKnowledge(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class UpdateKnowledge(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
    product_id: str = Field(min_length=1)



def create_management_router(resolve):
    """The provider must return live, service-authorized management resources."""
    router = APIRouter(prefix="/workmate/knowledge", tags=["workmate-knowledge"])

    @router.post("")
    async def create(body: CreateKnowledge, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        from agentscope.credential import OpenAICredential, DashScopeCredential
        from agentscope.rag import ApproxTokenChunker

        settings = runtime.settings
        service = runtime.service
        cls = (
            OpenAICredential
            if settings.knowledge_embedding_model_type == "openai"
            else DashScopeCredential
        )
        args = {"api_key": os.environ[settings.knowledge_embedding_model_api_key_env]}
        if cls is OpenAICredential:
            args["base_url"] = settings.knowledge_embedding_model_base_url
        credential = cls(**args)
        # A deterministic provider endpoint identity permits credential rotation
        # without changing the pinned model/dimension of existing collections.
        identity = hashlib.sha256(
            f"{x_user_id}:{settings.knowledge_embedding_model_type}:{settings.knowledge_embedding_model_base_url}".encode()
        ).hexdigest()[:24]
        credential.id = f"kb-embedding-{identity}"
        credential.name = "Knowledge embedding"
        credential_id = await runtime.storage.upsert_credential(
            x_user_id, credential
        )
        config = EmbeddingModelConfig(
            type=credential.type,
            credential_id=credential_id,
            model=settings.knowledge_embedding_model_name,
            dimensions=settings.knowledge_embedding_model_dimensions,
            parameters=(
                {"pass_dimensions": settings.knowledge_embedding_model_pass_dimensions}
                if settings.knowledge_embedding_model_type == "openai"
                else {}
            ),
        )
        chunker = ChunkerConfig(
            type=ApproxTokenChunker.chunker_type,
            parameters={
                "chunk_size": settings.knowledge.chunk_size,
                "overlap": settings.knowledge.chunk_overlap,
            },
        )
        return await service.create_knowledge_base(
            x_user_id, body.name, body.description, config, chunker
        )

    @router.get("/{kb_id}")
    async def detail(kb_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        await runtime.service._access.resolve_knowledge_base(x_user_id, kb_id)
        return await runtime.storage.get_knowledge_base(x_user_id, kb_id)

    @router.patch("/{kb_id}")
    async def update(kb_id: str, body: UpdateKnowledge, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        return await runtime.service.update_knowledge_base(
            x_user_id, kb_id, **body.model_dump(exclude_unset=True)
        )

    @router.delete("/{kb_id}", status_code=204)
    async def delete(kb_id: str, body: StagedDeleteRequest | None = None, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        staging = KnowledgeStaging(runtime)
        async with knowledge_mutation(runtime, kb_id):
            service = runtime.service
            await service._require_edit(x_user_id, kb_id)
            docs = await runtime.storage.list_knowledge_documents(
                x_user_id, kb_id
            )
            if any(d.status not in {"ready", "error"} for d in docs):
                raise HTTPException(409, "Documents are still being processed")
            # Preflight every processing state before any deletion while
            # holding the same cross-process lock used by explicit start.
            for doc in docs:
                await service.delete_document(x_user_id, kb_id, doc.id)
            for doc_id in body.document_ids if body else []:
                await staging.blobs.delete(staging.uri(kb_id,doc_id))
                await staging.blobs.delete(staging.uri(kb_id,doc_id,manifest=True))
            await service.delete_knowledge_base(x_user_id, kb_id)

    @router.get("/{kb_id}/documents")
    async def documents(kb_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        await runtime.service._access.resolve_knowledge_base(x_user_id, kb_id)
        docs = await runtime.storage.list_knowledge_documents(
            x_user_id, kb_id
        )
        return {"items": [document_projection(d) for d in docs], "total": len(docs)}

    @router.put("/{kb_id}/staged/{document_id}")
    async def upload(kb_id: str, document_id: str, file: UploadFile, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        staging = KnowledgeStaging(runtime)
        filename = PurePosixPath((file.filename or "").replace("\\", "/")).name
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in MIME:
            raise HTTPException(422, "Unsupported document format")
        await runtime.service._require_edit(x_user_id, kb_id)
        content = await file.read(runtime.settings.knowledge.max_upload_bytes + 1)
        await file.close()
        if len(content) > runtime.settings.knowledge.max_upload_bytes:
            raise HTTPException(413, "Document exceeds upload limit")
        if not content:
            raise HTTPException(422, "Document is empty")
        return await staging.upload(x_user_id,kb_id,document_id,filename,MIME[suffix],content)

    @router.get("/{kb_id}/staged/{document_id}")
    async def staged_view(kb_id: str, document_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        staging = KnowledgeStaging(runtime)
        return await staging.view(x_user_id,kb_id,document_id)

    @router.post("/{kb_id}/documents/{document_id}/start")
    async def start_document(kb_id: str, document_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        staging = KnowledgeStaging(runtime)
        return document_projection(await staging.start(x_user_id,kb_id,document_id))

    @router.delete("/{kb_id}/documents/{document_id}", status_code=204)
    async def delete_document(kb_id: str, document_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        staging = KnowledgeStaging(runtime)
        await staging.delete(x_user_id,kb_id,document_id)

    @router.post("/{kb_id}/documents/{document_id}/retry")
    async def retry(kb_id: str, document_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        from agentscope.app._bus_ops import enqueue_index_task

        async with knowledge_mutation(runtime, kb_id):
            service = runtime.service
            await service._require_edit(x_user_id, kb_id)
            doc = await service.get_document(x_user_id, kb_id, document_id)
            if doc.status != "error":
                raise HTTPException(409, "Only failed documents can be retried")
            knowledge = await service._resolve_knowledge(x_user_id, kb_id)
            await knowledge.delete_document(document_id)
            doc.status, doc.processing_node, doc.lease_expires_at = (
                "pending",
                None,
                None,
            )
            doc.data.error, doc.data.chunk_count = None, 0
            doc = await runtime.storage.upsert_knowledge_document(
                x_user_id, doc
            )
            await enqueue_index_task(
                runtime.message_bus,
                user_id=x_user_id,
                knowledge_base_id=kb_id,
                document_id=document_id,
            )
            return document_projection(doc)

    @router.get("/{kb_id}/documents/{document_id}/content")
    async def content(kb_id: str, document_id: str, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        staging = KnowledgeStaging(runtime)
        doc, size, stream = await staging.content(x_user_id, kb_id, document_id)
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.data.filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if size is not None:
            headers["Content-Length"] = str(size)
        return StreamingResponse(
            stream,
            media_type=doc.data.content_type or "application/octet-stream",
            headers=headers,
        )

    @router.post("/{kb_id}/search")
    async def search(kb_id: str, body: SearchRequest, x_user_id: str = Header(), runtime: KnowledgeManagement = Depends(resolve)):
        async with asyncio.timeout(runtime.settings.knowledge.search_timeout_secs):
            hits = await runtime.service.search(
                x_user_id, kb_id, body.query, body.top_k
            )
        return {"sources": [source_projection(body.product_id, h) for h in hits]}

    return router
