# -*- coding: utf-8 -*-
"""Task-bound input attachment storage.

Uploads stay in Product storage (local volume or private S3) until their Task
reaches the head of the queue. The Worker then copies them into that session's
``input/<task_id>/`` directory, so an active earlier Task cannot observe files
belonging to a queued Task.
"""

import os
from pathlib import Path

from .config import get_settings

MAX_ATTACHMENT_FILES = 10
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ATTACHMENT_CHUNK_BYTES = 64 * 1024


def safe_attachment_name(raw_name: str, used: set[str]) -> str:
    """Return a display-friendly, path-safe and collision-free basename."""
    basename = Path((raw_name or "attachment").replace("\\", "/")).name
    basename = "".join(ch for ch in basename if ch >= " " and ch not in "/\\")
    basename = basename.strip(" .") or "attachment"
    stem, suffix = os.path.splitext(basename)
    suffix = suffix[:24]
    stem = stem[:180] or "attachment"
    candidate = f"{stem}{suffix}"
    index = 2
    while candidate.casefold() in used:
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def attachment_workspace_path(task_id: str, filename: str) -> str:
    return f"input/{task_id}/{filename}"


def attachment_task_dir(conversation_id: str, task_id: str) -> str:
    return os.path.join(
        get_settings().artifact_storage_dir,
        ".inputs",
        conversation_id,
        task_id,
    )


def attachment_storage_path(
    conversation_id: str,
    task_id: str,
    workspace_path: str,
) -> str:
    return os.path.join(
        attachment_task_dir(conversation_id, task_id),
        Path(workspace_path).name,
    )


def attachment_conversation_dir(conversation_id: str) -> str:
    return os.path.join(
        get_settings().artifact_storage_dir,
        ".inputs",
        conversation_id,
    )


async def publish_attachment(store, key, content):
    """Join bounded object I/O on request cancellation; never abandon a publisher."""
    import asyncio
    operation = asyncio.create_task(asyncio.to_thread(store.publish,key,content))
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
        if not operation.cancelled():
            operation.exception()
        raise


async def cleanup_failed_upload(db, task_id, task_dir, store, keys):
    """After publisher join, keep committed inputs; remove only this request's keys."""
    import asyncio
    import shutil
    from workmate_product.persistence.models import Task
    async def cleanup():
        await db.rollback()
        committed = await db.get(Task,task_id) is not None
        await db.rollback()
        if not committed:
            if store is not None:
                for key in keys:
                    await asyncio.to_thread(store.delete,key)
            shutil.rmtree(task_dir,ignore_errors=True)
        elif store is not None:
            # S3 owns committed inputs; Gateway staging bytes are disposable.
            shutil.rmtree(task_dir,ignore_errors=True)
    operation = asyncio.create_task(cleanup())
    cancelled = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    try:
        operation.result()
    except Exception as error:
        if cancelled:
            raise asyncio.CancelledError from error
        raise
    if cancelled:
        raise asyncio.CancelledError
