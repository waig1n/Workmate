# -*- coding: utf-8 -*-
"""Agents router - scoped agent registry (design §10, RBAC design §6/§11.1).

All visibility/permission decisions go through AccessDecisionService
(RBAC design §7): invisible agents are 404, visible-but-forbidden
actions 403, invalid capability bindings 422, referenced agents 409.
"""

from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from ...auth import get_access_decision, get_current_user
from ...auth.permissions import AGENT_CREATE, MCP_MANAGE, SKILL_MANAGE
from ...db import get_db
from workmate_product.persistence.models import (
    Agent,
    CapabilityInstallation,
    CapabilityType,
    Scope,
    ScopeKind,
    User,
)
from workmate_product.application import resource_lists
from ..pagination import ResourcePage, page_result
from ...services.access_decision import AccessDecisionService
from ..schemas import AgentOut, CapabilityListItem, AgentCreate, AgentUpdate
from ._capabilities import capability_item
from ._agent_views import AgentOutFactory
from sqlalchemy.exc import IntegrityError
from workmate_product.application import agent_management
from workmate_product.application.platform_types import PlatformToolError
from workmate_product.application.knowledge_access import KnowledgeBindingError
from ...services.knowledge import knowledge_settings


router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=ResourcePage[AgentOut])
async def list_agents(
    scope: str = "all",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 12,
    q: Annotated[str, Query(max_length=200)] = "",
    category: Literal["all", "mine", "shared", "builtin"] = "all",
):
    if scope not in ("all", "personal", "organization"):
        raise HTTPException(status_code=422, detail="Invalid scope filter")
    agents, total = await resource_lists.agents(
        db, access, user.id, page=page, page_size=page_size, q=q, scope=scope, category=category,
    )
    factory = AgentOutFactory(db, access, user.id)
    await factory.prepare(agents)
    items = [await factory.build(agent) for agent in agents]
    return page_result(items, total, page, page_size)


async def _get_visible_agent(
    db: AsyncSession,
    access: AccessDecisionService,
    user_id: str,
    agent_id: str,
) -> Agent:
    """404 for anything the user cannot see (RBAC §7 - no enumeration)."""
    agent = await db.get(Agent, agent_id)
    if agent is None or not await access.can_view_agent(user_id, agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get(
    "/capability-candidates",
    response_model=list[CapabilityListItem],
)
async def capability_candidates(
    scope_id: str | None = None,
    agent_id: str | None = None,
    q: Annotated[str, Query(max_length=200)] = "",
    capability_type: Literal["skill", "mcp"] | None = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    """Capabilities bindable in an Agent configuration context.

    This view is deliberately separate from Skill/MCP management lists:
    an Agent manager may bind governed capabilities without gaining the
    right to inspect connection values or mutate the installation itself.
    """
    if (scope_id is None) == (agent_id is None):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of scope_id or agent_id",
        )

    if agent_id is not None:
        agent = await _get_visible_agent(
            db, access, user.id, agent_id,
        )
        if not await access.can_manage_agent(user.id, agent.id):
            raise HTTPException(
                status_code=403,
                detail="Not allowed to manage this agent",
            )
        target_scope = await db.get(Scope, agent.scope_id)
    else:
        target_scope = await db.get(Scope, scope_id)
        if target_scope is None:
            raise HTTPException(status_code=404, detail="Scope not found")
        if target_scope.kind == ScopeKind.PERSONAL:
            if target_scope.owner_user_id != user.id:
                raise HTTPException(status_code=403, detail="Not scope owner")
        elif not await access.has_permission(
            user.id, target_scope.id, AGENT_CREATE,
        ):
            raise HTTPException(
                status_code=403,
                detail="Permission required in this scope: agent.create",
            )

    if target_scope is None:
        raise HTTPException(status_code=404, detail="Scope not found")
    usable_ids = await access.accessible_capability_ids(user.id)
    allowed_ids = await access.bindable_capability_ids(target_scope)

    statement = (
        select(CapabilityInstallation)
        .join(Scope, Scope.id == CapabilityInstallation.scope_id)
        .where(
            CapabilityInstallation.id.in_(allowed_ids),
            CapabilityInstallation.enabled,
            Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)),
        )
        .order_by(CapabilityInstallation.capability_type, CapabilityInstallation.name, CapabilityInstallation.id)
    )
    if capability_type:
        statement = statement.where(CapabilityInstallation.capability_type == capability_type)
    if q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(or_(
            CapabilityInstallation.name.ilike(f"%{term}%", escape="\\"),
            CapabilityInstallation.display_name.ilike(f"%{term}%", escape="\\"),
        ))
    if limit is not None:
        statement = statement.limit(limit)
    rows = (await db.scalars(statement.offset(offset))).all()
    permission_matrix = await access.permissions_for_scopes(
        user.id, {installation.scope_id for installation in rows},
    )
    scopes_by_id = {
        scope.id: scope
        for scope in (
            await db.scalars(
                select(Scope).where(
                    Scope.id.in_({item.scope_id for item in rows}),
                ),
            )
        ).all()
    } if rows else {}
    result = []
    for installation in rows:
        permission = (
            SKILL_MANAGE
            if installation.capability_type == CapabilityType.SKILL
            else MCP_MANAGE
        )
        manageable = permission in permission_matrix[installation.scope_id]
        result.append(capability_item(
            installation,
            manageable,
            installation.id in usable_ids,
            scopes_by_id.get(installation.scope_id),
        ))
    return result


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
):
    agent = await _get_visible_agent(db, access, user.id, agent_id)
    factory = AgentOutFactory(db, access, user.id)
    return await factory.build(agent, include_binding_issues=True)


async def _mutate(db, actor_id, operation, **kwargs):
    try:
        if not db.in_transaction():
            await db.begin()
        result = await operation(db, actor_id=actor_id,
                                 knowledge_enabled=knowledge_settings().enabled, **kwargs)
        # Build the response while the Agent lock is still held. A concurrent
        # delete after commit must not turn a successful PATCH into a 500.
        response = await _view(db, actor_id, result) if result is not None else None
        await db.commit()
        return response
    except (PlatformToolError, KnowledgeBindingError) as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail if isinstance(exc, PlatformToolError) else str(exc)) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, 'Agent product mutation conflicted with another request') from exc


async def _view(db, actor_id, agent_id):
    agent = await db.get(Agent, agent_id, populate_existing=True)
    return await AgentOutFactory(db, AccessDecisionService(db), actor_id).build(agent, include_binding_issues=True)


@router.post('', response_model=AgentOut, status_code=201)
async def create_agent(body: AgentCreate, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    return await _mutate(db, actor_id, agent_management.create_agent, body=body)


@router.patch('/{agent_id}', response_model=AgentOut)
async def update_agent(agent_id: str, body: AgentUpdate, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    return await _mutate(db, actor_id, agent_management.update_agent, agent_id=agent_id, body=body)


@router.delete('/{agent_id}', status_code=204)
async def delete_agent(agent_id: str, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    await _mutate(db, user.id, agent_management.delete_agent, agent_id=agent_id)


@router.post('/{agent_id}/restore-default-prompt', response_model=AgentOut)
async def restore_default_prompt(agent_id: str, user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    return await _mutate(db, actor_id, agent_management.restore_default_prompt, agent_id=agent_id)
