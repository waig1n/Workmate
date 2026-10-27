# -*- coding: utf-8 -*-
"""Conversations router (design §11)."""

import base64
import binascii
import json
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from ...auth import get_access_decision, get_current_user
from ...config_file import model_profiles, default_model_profile_name
from ...db import get_db
from workmate_product.persistence.models import Agent, Conversation, TaskMessage, User
from ...services.access_decision import AccessDecisionService
from ..schemas import (
    ConversationCreate,
    ConversationPage,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    ConversationModelUpdate,
)
from ._agent_views import AgentOutFactory
from workmate_product.application.conversation_models import select_conversation_model
from workmate_product.application.platform_types import PlatformToolError
from ...services.knowledge import knowledge_settings


router = APIRouter(prefix="/conversations", tags=["conversations"])

READ_ONLY_REASON = "你已失去该智能体的使用权限，历史内容仍可查看"
UNAVAILABLE_CAPABILITIES_REASON = (
    "该智能体存在当前不可用的能力，请先解绑后再继续对话"
)


async def _get_owned(
    conversation_id: str,
    user: User,
    db: AsyncSession,
) -> Conversation:
    conv = await db.get(
        Conversation,
        conversation_id,
        options=[selectinload(Conversation.agent)],
    )
    if conv is None or conv.deleted_at is not None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


async def _conversation_out(
    conv: Conversation,
    user: User,
    db: AsyncSession,
    access: AccessDecisionService,
    factory: AgentOutFactory | None = None,
) -> ConversationOut:
    """Conversation view with revocation state (RBAC design §10):
    history stays readable, ``read_only`` disables new tasks."""
    if factory is None:
        factory = AgentOutFactory(db, access, user.id)
    can_use = await factory.can_use(conv.agent_id)
    unavailable_ids = (
        await factory.unavailable_ids(conv.agent_id)
        if can_use else []
    )
    read_only = not can_use or bool(unavailable_ids)
    if not can_use:
        agent = await db.get(Agent, conv.agent_id)
        read_only_reason = (
            "该智能体已删除，历史内容仍可查看"
            if agent is not None and agent.deleted_at is not None
            else READ_ONLY_REASON
        )
    elif unavailable_ids:
        read_only_reason = UNAVAILABLE_CAPABILITIES_REASON
    else:
        read_only_reason = None
    from workmate_product.application.execution_quarantine import conversation_quarantined,READ_ONLY_REASON as QUARANTINE_REASON
    if await conversation_quarantined(db,conv):
        read_only,read_only_reason=True,QUARANTINE_REASON
    return ConversationOut(
        id=conv.id,
        agent_id=conv.agent_id,
        title=conv.title,
        read_only=read_only,
        read_only_reason=read_only_reason,
        model_profile=conv.model_profile,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        agent=(
            await factory.build(conv.agent)
            if can_use else None
        ),
    )


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    limit: int | None = Query(default=None, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    convs = (
        await db.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
            .options(selectinload(Conversation.agent))
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit),
        )
    ).all()
    factory = AgentOutFactory(db, access, user.id)
    await factory.prepare([conv.agent for conv in convs])
    return [
        await _conversation_out(conv, user, db, access, factory)
        for conv in convs
    ]


@router.get("/history", response_model=ConversationPage)
async def conversation_history(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    q: str = Query(default="", max_length=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    query = select(Conversation).where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
    if q.strip():
        query = query.where(Conversation.title.icontains(q.strip(), autoescape=True))
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor))
            if not isinstance(decoded, list) or len(decoded) != 2:
                raise ValueError("Invalid cursor shape")
            stamp, identifier = decoded
            timestamp = datetime.fromisoformat(stamp)
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("Invalid identifier")
        except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid history cursor") from exc
        query = query.where(or_(
            Conversation.updated_at < timestamp,
            and_(Conversation.updated_at == timestamp, Conversation.id < identifier),
        ))
    rows = (await db.scalars(
        query.options(selectinload(Conversation.agent))
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit + 1),
    )).all()
    page = rows[:limit]
    factory = AgentOutFactory(db, access, user.id)
    await factory.prepare([conv.agent for conv in page])
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            last.updated_at.isoformat(), last.id,
        ]).encode()).decode()
    return ConversationPage(
        items=[await _conversation_out(conv, user, db, access, factory) for conv in page],
        next_cursor=next_cursor,
    )


@router.post("", response_model=ConversationOut, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    # Invisible agents are 404; visible but unusable are 403 (RBAC §7).
    agent = await db.scalar(
        select(Agent).where(Agent.id == body.agent_id).with_for_update(read=True)
        .execution_options(populate_existing=True),
    )
    if agent is None or not await access.can_view_agent(
        user.id, body.agent_id,
    ):
        raise HTTPException(status_code=404, detail="Agent not found")
    if not await access.can_use_agent(user.id, body.agent_id):
        raise HTTPException(
            status_code=403, detail="Not allowed to use this agent",
        )
    if body.model_profile is not None and body.model_profile not in (
        model_profiles()
    ):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown model profile: {body.model_profile}",
        )

    conv = Conversation(
        id=str(uuid.uuid4()),
        user_id=user.id,
        agent_id=agent.id,
        title=body.title,
        model_profile=body.model_profile,
    )
    db.add(conv)
    await db.commit()
    # The Worker prepares execution state when the first Task is admitted.
    await db.refresh(conv)
    conv.agent = agent
    return await _conversation_out(conv, user, db, access)


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    conv = await _get_owned(conversation_id, user, db)
    return await _conversation_out(conv, user, db, access)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    conv = await _get_owned(conversation_id, user, db)
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Title cannot be blank")
    conv.title = title
    await db.commit()
    await db.refresh(conv)
    return await _conversation_out(conv, user, db, access)


@router.get(
    "/{conversation_id}/messages",
    response_model=list[MessageOut],
)
async def list_messages(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # Bounds: negative offset is a Postgres error (500), a negative or
    # huge limit is an unbounded response.
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    await _get_owned(conversation_id, user, db)
    messages = (
        await db.scalars(
            select(TaskMessage)
            .where(TaskMessage.conversation_id == conversation_id)
            .order_by(TaskMessage.created_at)
            .offset(offset)
            .limit(limit),
        )
    ).all()
    return [MessageOut.model_validate(m) for m in messages]


@router.patch('/{conversation_id}/model', response_model=ConversationOut)
async def update_conversation_model(conversation_id: str, body: ConversationModelUpdate,
                                    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    profile = default_model_profile_name() if body.model_profile is None else body.model_profile
    if profile not in model_profiles():
        raise HTTPException(422, f'Unknown model profile: {profile}')
    try:
        if not db.in_transaction():
            await db.begin()
        await select_conversation_model(db, actor_id=actor_id, conversation_id=conversation_id,
            profile_name=profile, knowledge_enabled=knowledge_settings().enabled)
        conv = await db.get(Conversation, conversation_id, populate_existing=True,
                            options=[selectinload(Conversation.agent)])
        response = await _conversation_out(conv, user, db, AccessDecisionService(db))
        await db.commit()
        return response
    except PlatformToolError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.delete('/{conversation_id}', status_code=204)
async def delete_conversation(conversation_id: str, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    from workmate_product.application.conversation_retirement import request_retirement
    try:
        if not db.in_transaction():
            await db.begin()
        await request_retirement(db, actor_id=user.id, conversation_id=conversation_id)
        await db.commit()
    except PlatformToolError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
