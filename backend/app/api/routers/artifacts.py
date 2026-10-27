# -*- coding: utf-8 -*-
"""Artifacts router (design §17)."""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ...auth import get_current_user
from ...db import get_db
from workmate_product.persistence.models import Artifact, Conversation, User, TaskArtifactDelivery
from ..schemas import ArtifactOut
import asyncio
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse
from workmate_product.application.artifact_objects import (
    snapshot_artifact,
    ArtifactObjectUnavailable,
)
from workmate_product.artifact_storage import artifact_store_from_settings
from ...artifacts.policy import may_preview_inline
from ...config import get_settings


router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("", response_model=list[ArtifactOut])
async def list_artifacts(
    conversation_id: str | None = None,
    task_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    query = (
        select(Artifact, TaskArtifactDelivery)
        .join(TaskArtifactDelivery, TaskArtifactDelivery.artifact_id == Artifact.id)
        .join(Conversation, Artifact.conversation_id == Conversation.id)
        .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .order_by(TaskArtifactDelivery.delivered_at.desc(), TaskArtifactDelivery.task_id, Artifact.id)
        .offset(offset)
        .limit(limit)
    )
    if conversation_id:
        query = query.where(Artifact.conversation_id == conversation_id)
    if task_id:
        query = query.where(TaskArtifactDelivery.task_id == task_id)
    rows = (await db.execute(query)).all()
    return [ArtifactOut.model_validate(artifact).model_copy(update={
        'task_id': delivery.task_id, 'change_type': delivery.change_type,
        'metadata': delivery.metadata_, 'created_at': delivery.delivered_at,
    }) for artifact, delivery in rows]


class SnapshotFileResponse(FileResponse):
    def __init__(self, snapshot, **kwargs):
        self.snapshot = snapshot
        super().__init__(snapshot.path, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.snapshot.close()


async def _snapshot(*args, object_store=None):
    # Join the copy even on request cancellation, then remove its private result.
    if object_store is not None:
        import re
        _,key,digest,size = args
        if (not isinstance(digest,str) or re.fullmatch("[0-9a-f]{64}",digest) is None
                or key != f".objects/sha256/{digest[:2]}/{digest}"
                or type(size) is not int or size < 0):
            raise ValueError("Invalid Artifact object descriptor")
    function = snapshot_artifact if object_store is None else object_store.snapshot
    values = args if object_store is None else args[1:]
    operation = asyncio.create_task(run_in_threadpool(function, *values))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if not operation.cancelled() and operation.exception() is None:
            operation.result().close()
        raise


@router.get('/{artifact_id}/download')
async def download_artifact(artifact_id: str, inline: bool = False,
                            user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    artifact = await db.get(Artifact, artifact_id)
    conv = await db.get(Conversation, artifact.conversation_id) if artifact else None
    if conv is None or conv.deleted_at is not None or conv.user_id != user.id:
        raise HTTPException(404, 'Artifact not found')
    settings = get_settings()
    key, digest, size = artifact.storage_key, (artifact.metadata_ or {}).get('sha256'), artifact.size
    name, mime = artifact.name, artifact.mime_type
    # No database transaction is held during filesystem I/O or a slow download.
    await db.rollback()
    try:
        snapshot = await _snapshot(settings.artifact_storage_dir,
            key, digest, size, object_store=artifact_store_from_settings(settings))
    except ValueError as error:
        raise HTTPException(400, 'Invalid Artifact object descriptor') from error
    except ArtifactObjectUnavailable as error:
        raise HTTPException(410, 'Artifact file unavailable') from error
    try:
        allow_inline = inline and may_preview_inline(name, mime, settings)
        return SnapshotFileResponse(snapshot, media_type=mime, filename=name,
            content_disposition_type='inline' if allow_inline else 'attachment',
            headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'})
    except BaseException:
        snapshot.close()
        raise
