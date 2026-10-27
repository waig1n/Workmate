# -*- coding: utf-8 -*-
"""Tasks router - task lifecycle, attachments, AG-UI SSE and HITL."""

import mimetypes
import logging
import os
import shutil
import uuid
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import select, or_, and_, case
from sqlalchemy.ext.asyncio import AsyncSession
from workmate_product.projections.knowledge import compact_event_payload
from ...adapter.skill_projection import compact_skill_payload, historical_skill_event_ids
from ...attachments import (
    ATTACHMENT_CHUNK_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_FILES,
    attachment_task_dir,
    attachment_workspace_path,
    safe_attachment_name,
)
from ...auth import get_access_decision, get_current_user, get_current_user_id
from ...db import get_db
from workmate_product.persistence.models import (
    ApprovalProfile,
    Conversation,
    Task,
    TaskEvent,
    TaskStatus,
    User,
)
from ...services.task_presentation import present_task, present_tasks
from ...services import ModelConfigError
from ...services.access_decision import AccessDecisionService
from ...services.runtime_errors import ApprovalConflict
from ...services.task_approval import pending_confirm
from ..schemas import (
    ConfirmBatchRequest,
    PendingConfirmOut,
    TaskCreatedOut,
    TaskCreate,
    TaskEventOut,
    TaskOut,
)


router = APIRouter(prefix="/tasks", tags=["tasks"])


def _product_runtime(request: Request):
    return request.app.state.product_runtime


async def _get_owned_task(
    task_id: str, user: User, db: AsyncSession,
) -> tuple[Task, Conversation]:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    conv = await db.get(Conversation, task.conversation_id)
    if conv is None or conv.deleted_at is not None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task, conv


async def _defer_initial_security_check(request, user, profile):
    """The owning Worker attests isolation before starting a Full Access Reply."""
    if ApprovalProfile(profile) == ApprovalProfile.UNATTENDED:
        raise HTTPException(422, detail='unattended is reserved for scheduled tasks')


def initial_task_security_check():
    """Task admission validates intent; Worker admission enforces isolation."""
    return _defer_initial_security_check


@router.post(
    "/conversations/{conversation_id}/tasks",
    response_model=TaskCreatedOut,
    status_code=202,
)
async def create_task(
    conversation_id: str,
    body: TaskCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
    security_check = Depends(initial_task_security_check),
):
    """Create a task and trigger its run; queued if the conversation
    is busy (design §7 serial-run semantics). Agent use rights are
    re-checked on every task - never cached from conversation creation
    (RBAC design §10)."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.deleted_at is not None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await access.can_use_agent(user.id, conv.agent_id):
        raise HTTPException(
            status_code=403,
            detail="你已失去该智能体的使用权限，历史内容仍可查看",
        )
    unavailable_ids = await access.unavailable_capability_ids(
        user.id, conv.agent_id,
    )
    if unavailable_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "unavailable_capabilities",
                "capability_ids": unavailable_ids,
                "message": "智能体存在当前不可用的能力，请先解绑后再试",
            },
        )

    await security_check(
        request, user, body.approval_profile,
    )

    try:
        task, queued = await _product_runtime(request).create_task(
            db,
            user,
            conv,
            body.message,
            body.title,
            approval_profile=body.approval_profile.value,
        )
    except ModelConfigError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - runtime failure
        raise HTTPException(
            status_code=502, detail=f"Runtime trigger failed: {e}",
        ) from e
    return TaskCreatedOut(task=await present_task(db, task), queued=queued)


@router.post(
    "/conversations/{conversation_id}/tasks-with-files",
    response_model=TaskCreatedOut,
    status_code=202,
)
async def create_task_with_files(
    conversation_id: str,
    request: Request,
    message: str = Form(default=""),
    title: str | None = Form(default=None),
    approval_profile: str = Form(default="standard"),
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
    security_check = Depends(initial_task_security_check),
):
    """Create a Task with input files, preserving serial-run isolation."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.deleted_at is not None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await access.can_use_agent(user.id, conv.agent_id):
        raise HTTPException(
            status_code=403,
            detail="你已失去该智能体的使用权限，历史内容仍可查看",
        )
    unavailable_ids = await access.unavailable_capability_ids(
        user.id,
        conv.agent_id,
    )
    if unavailable_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "unavailable_capabilities",
                "capability_ids": unavailable_ids,
                "message": "智能体存在当前不可用的能力，请先解绑后再试",
            },
        )
    if not files or len(files) > MAX_ATTACHMENT_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"每个任务最多上传 {MAX_ATTACHMENT_FILES} 个文件",
        )
    try:
        effective_profile = ApprovalProfile(approval_profile)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid approval_profile",
        ) from exc
    await security_check(request, user, effective_profile)

    task_id = str(uuid.uuid4())
    from workmate_product.artifact_storage import artifact_store_from_settings
    from ...attachments import publish_attachment, cleanup_failed_upload
    from ...config import get_settings
    object_store = artifact_store_from_settings(get_settings())
    task_dir = attachment_task_dir(conversation_id, task_id)
    os.makedirs(task_dir, exist_ok=False)
    attachments: list[dict] = []
    used_names: set[str] = set()
    object_keys = []
    actor_id = user.id
    if object_store is not None:
        # Release initial authorization reads during potentially slow remote I/O.
        await db.rollback()
    try:
        for upload in files:
            safe_name = safe_attachment_name(
                upload.filename or "attachment",
                used_names,
            )
            target = os.path.join(task_dir, safe_name)
            size = 0
            with open(target, "wb") as destination:
                while chunk := await upload.read(ATTACHMENT_CHUNK_BYTES):
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"文件 {safe_name} 超过 "
                                f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB 限制"
                            ),
                        )
                    destination.write(chunk)
            mime_type = (
                upload.content_type
                or mimetypes.guess_type(safe_name)[0]
                or "application/octet-stream"
            )
            object_metadata = {}
            if object_store is not None:
                key = f".inputs/{conversation_id}/{task_id}/{safe_name}"
                with open(target, "rb") as source:
                    content = source.read()
                object_keys.append(key)
                digest = await publish_attachment(object_store,key,content)
                object_metadata = {"storage_key": key, "sha256": digest}
                os.unlink(target)
            attachments.append({
                **object_metadata,
                "name": safe_name,
                "path": attachment_workspace_path(task_id, safe_name),
                "mime_type": mime_type,
                "size": size,
            })

        if object_store is not None:
            user = await db.get(User,actor_id)
            conv = await db.get(Conversation,conversation_id)
            if user is None or conv is None or conv.deleted_at is not None or conv.user_id != actor_id:
                raise HTTPException(404,"Conversation not found")
        task_message = message.strip() or "请处理这些附件"
        task, queued = await _product_runtime(request).create_task(
            db,
            user,
            conv,
            task_message,
            title,
            task_id=task_id,
            attachments=attachments,
            approval_profile=effective_profile.value,
        )
        return TaskCreatedOut(
            task=await present_task(db, task),
            queued=queued,
        )
    except BaseException as exc:
        try:
            await cleanup_failed_upload(db,task_id,task_dir,object_store,object_keys)
        except Exception as cleanup_error:
            # Remote storage may also be unavailable during compensating
            # deletion. Preserve the original upload/admission failure; the
            # exact Task prefix identifies any unreferenced input objects.
            logging.getLogger(__name__).warning(
                'Attachment cleanup incomplete: task_id=%s conversation_id=%s object_count=%s error_type=%s',
                task_id, conversation_id, len(object_keys), type(cleanup_error).__name__,
            )
        if isinstance(exc,ModelConfigError):
            raise HTTPException(status_code=503,detail=str(exc)) from exc
        if isinstance(exc,HTTPException) or not isinstance(exc,Exception):
            raise
        raise HTTPException(status_code=502,detail=f"Runtime trigger failed: {exc}") from exc
    finally:
        if object_store is not None:
            shutil.rmtree(task_dir,ignore_errors=True)


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    conversation_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    query = (
        select(Task)
        .join(Conversation, Task.conversation_id == Conversation.id)
        .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if conversation_id:
        query = query.where(Task.conversation_id == conversation_id)
    tasks = (await db.scalars(query)).all()
    return await present_tasks(db, list(tasks))


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, _ = await _get_owned_task(task_id, user, db)
    return await present_task(db, task)


@router.post("/{task_id}/stop", response_model=TaskOut)
async def stop_task(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task, conv = await _get_owned_task(task_id, user, db)
    if task.status not in (
        "running", "awaiting_permission", "created", "cancelling",
    ):
        raise HTTPException(status_code=409, detail=f"Task is {task.status}")
    try:
        await _product_runtime(request).stop(db, user, conv, task)
    except ApprovalConflict as exc:
        raise HTTPException(status_code=409,detail=str(exc)) from exc
    return await present_task(db, task)


@router.post("/{task_id}/confirm", response_model=TaskOut)
async def confirm_task(
    task_id: str,
    body: ConfirmBatchRequest,
    request: Request,
    bypass_remaining: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve/reject pending HITL tool calls (design §18)."""
    task, conv = await _get_owned_task(task_id, user, db)
    if bypass_remaining and task.approval_profile == ApprovalProfile.UNATTENDED:
        raise HTTPException(409, detail='Unattended tasks cannot enable Full Access')
    decisions = [decision.model_dump() for decision in body.decisions]
    decision_version = body.decision_version
    try:
        await _product_runtime(request).confirm(
            db, user, conv, task,
            decisions,
            decision_version=decision_version,
            full_access=bypass_remaining,
        )
    except ApprovalConflict as exc:
        detail = str(exc)
        if detail == "full_access_requires_sandbox":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": detail,
                    "message": "Full Access requires an isolated Bubblewrap workspace.",
                },
            ) from exc
        if detail.startswith("Task is not awaiting permission"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "task_not_awaiting_permission",
                    "message": detail,
                },
            ) from exc
        raise HTTPException(status_code=409, detail=detail) from exc
    return await present_task(db, task)


@router.post("/{task_id}/full-access", response_model=TaskOut)
async def enable_full_access(
    task_id: str,
    body: ConfirmBatchRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resume one parked Task under governed Full Access and Agent DENY."""
    task, conv = await _get_owned_task(task_id, user, db)
    if task.approval_profile == ApprovalProfile.UNATTENDED:
        raise HTTPException(409, detail='Unattended tasks cannot enable Full Access')
    try:
        await _product_runtime(request).confirm(
            db,
            user,
            conv,
            task,
            [decision.model_dump() for decision in body.decisions],
            decision_version=body.decision_version,
            full_access=True,
        )
    except ApprovalConflict as exc:
        detail = str(exc)
        if detail == "full_access_requires_sandbox":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": detail,
                    "message": "Full Access requires an isolated Bubblewrap workspace.",
                },
            ) from exc
        raise HTTPException(status_code=409, detail=detail) from exc
    return await present_task(db, task)


def _task_event_query(*, details: bool = False):
    # Project JSON in the DB so normal polling does not transfer/decode the
    # full excerpt payload. Legacy rows are compacted after loading.
    payload = TaskEvent.payload if details else case(
        (TaskEvent.payload["browser_projection"].as_string().is_not(None),
         TaskEvent.payload["browser_projection"]),
        else_=TaskEvent.payload,
    )
    return select(TaskEvent.id, TaskEvent.task_id, TaskEvent.seq, TaskEvent.event_type,
                  payload.label("payload"), TaskEvent.created_at)


@router.get("/{task_id}/events", response_model=list[TaskEventOut])
async def get_task_events(
    task_id: str,
    event_type: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    """Task timeline projection; ``event_type`` filter gives the Tool
    Audit view (design §13 - a filtered view, never a second table)."""
    task, _ = await _get_owned_task(task_id, user, db)
    query = (
        _task_event_query()
        .where(TaskEvent.task_id == task.id)
        .order_by(TaskEvent.seq)
        .offset(offset)
        .limit(limit)
    )
    if event_type:
        query = query.where(TaskEvent.event_type == event_type)
    events = (await db.execute(query)).all()
    skill_events = await historical_skill_event_ids(db, task.id)
    return [TaskEventOut.model_validate(e).model_copy(update={
        "payload": (compact_skill_payload(e.payload or {}) if e.id in skill_events
                    else compact_event_payload(e.event_type, e.payload or {})),
    }) for e in events]


@router.get("/{task_id}/knowledge-events", response_model=list[TaskEventOut])
async def get_knowledge_events(
    task_id: str,
    details: bool = False,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Owned-task retrieval facts; excerpts/queries only on explicit expansion."""
    task, _ = await _get_owned_task(task_id, user, db)
    events = (await db.execute(_task_event_query(details=details).where(
        TaskEvent.task_id == task.id,
        or_(TaskEvent.event_type.in_(["knowledge_sources", "knowledge_retrieval"]),
            and_(TaskEvent.event_type == "tool_result",
                 TaskEvent.payload["tool"].as_string() == "search_knowledge")),
    ).order_by(TaskEvent.seq).offset(offset).limit(limit))).all()
    return [TaskEventOut.model_validate(e).model_copy(update={
        "payload": {k: v for k, v in e.payload.items() if k != "browser_projection"} if details else compact_event_payload(e.event_type, e.payload or {}),
    }) for e in events]


@router.get("/{task_id}/pending-confirm", response_model=PendingConfirmOut)
async def get_pending_confirm(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rebuild the HITL approval card from the audit log.

    Persisted request and decision history reconstruct only unresolved calls.
    The REST card and realtime reset use the same interpretation.
    """
    task, _ = await _get_owned_task(task_id, user, db)
    if task.status != TaskStatus.AWAITING_PERMISSION:
        raise HTTPException(
            status_code=404, detail="Task is not awaiting permission",
        )
    pending = await pending_confirm(db, task)
    if pending is None:
        raise HTTPException(
            status_code=404, detail="No pending approval request",
        )
    return PendingConfirmOut.model_validate(pending)


# ----------------------------------------------------------------------
# AG-UI SSE (design §6/§8)
# ----------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/events")
async def stream_conversation_events(
    conversation_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AG-UI event stream for a conversation.

    Validate the actor and replay handshake before HTTP200. Transport and
    recovery are supplied by the role's single selected Product stream.
    """
    conv = await db.get(Conversation, conversation_id)
    actor_id = user.id
    if conv is None or conv.deleted_at is not None or conv.user_id != actor_id:
        raise HTTPException(status_code=404, detail="Conversation not found")

    last_id=request.headers.get('Last-Event-ID')
    stream=request.app.state.product_stream
    from ...auth.deps import get_current_token
    from ...services.realtime_stream_http import realtime_response
    claims = await get_current_token(request)
    if claims['sub'] != actor_id:
        raise HTTPException(status_code=401, detail='Not authenticated')
    token_version = claims.get('ver', 0)
    await db.rollback()
    return await realtime_response(request, stream, conversation_id, actor_id=actor_id,
        token_version=token_version, last_event_id=last_id)


@router.get('/{task_id}/tools/{tool_call_id}/content')
async def get_tool_content(
    task_id: str, tool_call_id: str,
    kind: str = Query(pattern='^(arguments|result|metadata)$'),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Read exact immutable display content through Task ownership, never a user key."""
    import asyncio
    from ...config import get_settings
    from workmate_product.artifact_storage import tool_content_store_from_settings
    from workmate_product.application.artifact_objects import ArtifactObjectUnavailable
    from workmate_product.persistence.repositories.tool_content import find_content
    from .artifacts import SnapshotFileResponse

    await _get_owned_task(task_id, user, db)
    ref = await find_content(db, task_id, tool_call_id, kind)
    if ref is None:
        raise HTTPException(404, 'Tool content not found')
    settings = get_settings()
    try:
        store = tool_content_store_from_settings(settings)
    except ValueError:
        store = None
    if store is None:
        raise HTTPException(503, 'Tool content storage unavailable')
    # The descriptor is trusted only after validation by the object reader.
    await db.rollback()
    operation = asyncio.create_task(asyncio.to_thread(store.snapshot,
        ref['storage_key'], ref['sha256'], ref['size_bytes']))
    try:
        snapshot = await asyncio.shield(operation)
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
    except (ArtifactObjectUnavailable, ValueError):
        raise HTTPException(410, 'Tool content unavailable') from None
    return SnapshotFileResponse(snapshot, media_type='text/plain; charset=utf-8',
        filename='tool-result.txt' if kind == 'result' else 'tool-' + kind + '.json',
        content_disposition_type='inline',
        headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
