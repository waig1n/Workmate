# -*- coding: utf-8 -*-
"""Product-facing scoped MCP registry API (RBAC design §11.3).

MCP connection values are write-only through this router: they go to
the runtime principal's library and never appear in any response.
"""

from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from ...auth import get_access_decision, get_current_user
from ...db import get_db
from workmate_product.persistence.models import CapabilityType, Scope, User
from workmate_product.application import resource_lists, catalog_status
from ..pagination import ResourcePage, page_result
from ...services.access_decision import AccessDecisionService
from ..schemas import CapabilityListItem, MCPInstallRequest, MCPUpdateRequest
from ._capabilities import capability_item
import httpx
from sqlalchemy.exc import IntegrityError
from workmate_product.mcp import repository
from workmate_product.mcp.definitions import MCPDefinitionError


router = APIRouter(prefix="/mcps", tags=["mcps"])


@router.get("", response_model=ResourcePage[CapabilityListItem])
async def installed(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 12,
    q: Annotated[str, Query(max_length=200)] = "",
    source: Literal["custom", "catalog"] | None = None,
):
    pairs, total = await resource_lists.capabilities(
        db, access, user.id, CapabilityType.MCP, page=page, page_size=page_size, q=q, source=source,
    )
    items = [capability_item(installation, manageable, usable, await db.get(Scope, installation.scope_id))
             for installation, manageable, usable in pairs]
    return page_result(items, total, page, page_size)


def _catalogs(request):
    catalogs = getattr(request.app.state, 'mcp_catalogs', None)
    if catalogs is None:
        raise HTTPException(503, 'MCP catalogs are unavailable')
    return catalogs


def _hub(request, hub_id):
    hub = _catalogs(request).get(hub_id)
    if hub is None:
        raise HTTPException(404, 'MCP Hub not found')
    return hub


def _failure(exc):
    if isinstance(exc, MCPDefinitionError):
        return HTTPException(exc.status_code if 400 <= exc.status_code < 500 else 502,
            {'code': exc.code, 'message': exc.detail} if exc.code else exc.detail)
    if isinstance(exc, KeyError):
        return HTTPException(404, 'MCP card not found')
    if isinstance(exc, IntegrityError):
        return HTTPException(409, 'MCP mutation conflicted with another request')
    return HTTPException(502, 'MCP catalog request failed')


_ERRORS = (MCPDefinitionError, KeyError, httpx.HTTPError, IntegrityError)


async def _card(request, actor_id, hub_id, card_id):
    card = await _hub(request, hub_id).get_mcp(actor_id, card_id)
    if card.hub_id != hub_id or card.id != card_id:
        raise MCPDefinitionError('MCP catalog returned a different source identity', 502)
    return card


async def _view(db, actor_id, installation):
    access = AccessDecisionService(db)
    return capability_item(installation, True,
        installation.id in await access.accessible_capability_ids(actor_id),
        await db.get(Scope, installation.scope_id))


@router.get('/hubs')
async def hubs(request: Request, user: User = Depends(get_current_user)):
    return sorted([dict(hub_id=hub.hub_id, display_name=hub.display_name,
                        description=hub.description, icon_url=hub.icon_url)
                   for hub in _catalogs(request).values()], key=lambda card: card['hub_id'])


@router.get('/catalog')
async def catalog(request: Request, hub_id: str = Query(...), q: str | None = None,
                  cursor: str | None = None, scope_id: str | None = None, limit: int = Query(40, ge=1, le=100),
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    await db.rollback()
    try:
        page = await _hub(request, hub_id).list_mcps(actor_id, q, cursor, limit)
        return await catalog_status.annotate(db, AccessDecisionService(db), actor_id,
            page.model_dump(mode="json"), hub_id, CapabilityType.MCP, scope_id=scope_id)
    except catalog_status.CatalogAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except _ERRORS as exc:
        raise _failure(exc) from exc


@router.post('', response_model=CapabilityListItem, status_code=201)
async def install(body: MCPInstallRequest, request: Request,
                  user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    try:
        if not db.in_transaction():
            await db.begin()
        await repository.authorize_mcp_install(db, actor_user_id=actor_id, scope_id=body.scope_id)
        await db.rollback()
        card = await _card(request, actor_id, body.hub_id, body.card_id)
        async with db.begin():
            installation = await repository.install_mcp(db, actor_user_id=actor_id,
                scope_id=body.scope_id, card=card, values=body.values, name=body.name)
            response = await _view(db, actor_id, installation)
        return response
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.patch('/{installation_id}', response_model=CapabilityListItem)
async def update(installation_id: str, body: MCPUpdateRequest, request: Request,
                 user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    actor_id = user.id
    try:
        if not db.in_transaction():
            await db.begin()
        installation = await repository.update_mcp(db, actor_user_id=actor_id,
            installation_id=installation_id, values=body.values,
            name=body.name, enabled=body.enabled)
        response = await _view(db, actor_id, installation)
        await db.commit()
        return response
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.delete('/{installation_id}', status_code=204)
async def uninstall(installation_id: str, user: User = Depends(get_current_user),
                    db: AsyncSession = Depends(get_db)):
    try:
        if not db.in_transaction():
            await db.begin()
        await repository.uninstall_mcp(db, actor_user_id=user.id, installation_id=installation_id)
        await db.commit()
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc
