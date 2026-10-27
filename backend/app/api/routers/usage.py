"""Authorized personal and organizational usage views."""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_current_user
from ...db import get_db
from workmate_product.persistence.models import User
from ...services.usage import date_bounds, readable_scopes, usage_base, usage_report

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("/scopes")
async def scopes(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return [{"id": s.id, "name": s.name, "kind": s.kind} for s in await readable_scopes(db, user.id)]


@router.get("/options")
async def options(
    since: date | None = None, until: date | None = None, scope_id: str | None = None,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    _, _, start, end = date_bounds(since, until)
    base = await usage_base(db, user.id, start, end, scope_id)
    result = {}
    for kind, key in (("user", "users"), ("model", "models"), ("agent", "agents")):
        rows = (await db.execute(select(
            base.c[f"{kind}_id"].label("id"), base.c[f"{kind}_name"].label("name"),
        ).distinct().order_by(base.c[f"{kind}_id"]).limit(500))).mappings()
        result[key] = [dict(row) for row in rows]
    return result


@router.get("")
async def report(
    since: date | None = None, until: date | None = None,
    scope_id: str | None = None, user_id: str | None = None,
    model_profile: str | None = None, agent_id: str | None = None,
    group_by: Literal["user", "model", "agent"] = "user",
    limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    since, until, start, end = date_bounds(since, until)
    base = await usage_base(db, user.id, start, end, scope_id, user_id, model_profile, agent_id)
    return {"since": since, "until": until, **await usage_report(db, base, group_by, limit, offset)}
