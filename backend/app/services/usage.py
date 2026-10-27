"""Task-level usage snapshots and bounded SQL aggregation (no per-token writes)."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.permissions import USAGE_READ
from ..config_file import load_file_config, model_profiles
from workmate_product.persistence.models import (
    Agent,
    Conversation,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
    Task,
    User,
)
from workmate_product.persistence.models import TaskUsageScope
from .access_decision import AccessDecisionService


async def snapshot_usage_scopes(db: AsyncSession, task: Task, user_id: str) -> None:
    """Capture memberships AND ancestors once, without multiplying task totals."""
    scope_ids = (await db.scalars(
        select(ScopeClosure.ancestor_id)
        .join(ScopeMembership, ScopeMembership.scope_id == ScopeClosure.descendant_id)
        .join(Scope, Scope.id == ScopeClosure.ancestor_id)
        .where(ScopeMembership.user_id == user_id, Scope.kind != ScopeKind.PERSONAL)
        .distinct()
    )).all()
    db.add_all([TaskUsageScope(task_id=task.id, scope_id=sid) for sid in scope_ids])


def snapshot_usage_pricing(task: Task, conversation: Conversation) -> None:
    task.usage_model_profile = conversation.model_profile
    profile = model_profiles().get(conversation.model_profile)
    task.usage_model_id = profile.model if profile else None
    config = load_file_config()
    price = config.product.usage.prices.get(conversation.model_profile) if config else None
    task.usage_pricing = price.model_dump(mode="json") if price else None
    task.usage_currency = config.product.usage.currency if price else None


def finish_usage_cost(task: Task, *, model_matches: bool = True) -> None:
    """Estimate only known input/output usage at the frozen profile's rates."""
    task.usage_cost = None
    usage, price = task.token_usage, task.usage_pricing
    if not usage or not price or not model_matches or usage.get("complete") is False:
        return
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return
    task.usage_cost = (
        Decimal(usage["input_tokens"]) * Decimal(price["input_per_k"])
        + Decimal(usage["output_tokens"]) * Decimal(price["output_per_k"])
    ) / Decimal(1000)


def date_bounds(since: date | None, until: date | None):
    until = until or datetime.now(timezone.utc).date()
    since = since or until - timedelta(days=29)
    if since > until or (until - since).days >= 366:
        raise HTTPException(422, detail="日期范围必须为 1 至 366 天")
    start = datetime.combine(since, time.min, tzinfo=timezone.utc)
    end = datetime.combine(until, time.max, tzinfo=timezone.utc)
    return since, until, start, end


async def readable_scopes(db: AsyncSession, user_id: str) -> list[Scope]:
    access = AccessDecisionService(db)
    matrix = await access.permissions_for_scopes(user_id, await access.accessible_scope_ids(user_id))
    allowed = [sid for sid, keys in matrix.items() if USAGE_READ in keys]
    return list((await db.scalars(select(Scope).where(
        Scope.id.in_(allowed), Scope.kind != ScopeKind.PERSONAL,
    ).order_by(Scope.name, Scope.id))).all())


async def usage_base(
    db: AsyncSession, user_id: str, start: datetime, end: datetime,
    scope_id: str | None = None, target_user_id: str | None = None,
    model_profile: str | None = None, agent_id: str | None = None,
):
    """Apply authorization before aggregation, filters and option enumeration."""
    if scope_id:
        scope = await db.get(Scope, scope_id)
        if scope is None or scope.kind == ScopeKind.PERSONAL or not await AccessDecisionService(db).has_permission(user_id, scope_id, USAGE_READ):
            raise HTTPException(403, detail="无权查看此范围的用量")
    elif target_user_id and target_user_id != user_id:
        raise HTTPException(403, detail="个人用量仅能查看本人")
    inp = Task.token_usage["input_tokens"].as_integer()
    out = Task.token_usage["output_tokens"].as_integer()
    profile = func.coalesce(Task.usage_model_profile, Task.model_name, "unknown")
    query = select(
        Task.id, Task.created_at,
        Conversation.user_id.label("user_id"), User.display_name.label("user_name"),
        Conversation.agent_id.label("agent_id"), Agent.display_name.label("agent_name"),
        profile.label("model_id"), profile.label("model_name"),
        func.coalesce(inp, 0).label("input_tokens"), func.coalesce(out, 0).label("output_tokens"),
        case((inp.is_not(None) & out.is_not(None) & func.coalesce(Task.token_usage["complete"].as_boolean(), True), 1), else_=0).label("recorded"),
        Task.usage_cost.label("cost"), Task.usage_currency.label("currency"),
    ).join(Conversation, Task.conversation_id == Conversation.id).join(
        User, User.id == Conversation.user_id,
    ).join(Agent, Agent.id == Conversation.agent_id).where(
        Task.created_at >= start, Task.created_at <= end, Conversation.deleted_at.is_(None),
    )
    if scope_id:
        query = query.where(exists(select(TaskUsageScope.task_id).where(
            TaskUsageScope.task_id == Task.id, TaskUsageScope.scope_id == scope_id,
        )))
    else:
        query = query.where(Conversation.user_id == user_id)
    for column, value in ((Conversation.user_id, target_user_id), (profile, model_profile), (Conversation.agent_id, agent_id)):
        if value is not None:
            query = query.where(column == value)
    return query.subquery()


def _metrics(base):
    return (
        func.count().label("task_count"),
        func.coalesce(func.sum(base.c.recorded), 0).label("recorded_tasks"),
        func.coalesce(func.sum(base.c.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(base.c.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(case((base.c.cost.is_(None), 1), else_=0)), 0).label("unpriced_tasks"),
    )


async def aggregate(db, base, dimension=None, name=None, *, limit=None, offset=0):
    columns = [dimension.label("id"), name.label("name")] if dimension is not None else []
    query = select(*columns, *_metrics(base)).select_from(base)
    if dimension is not None:
        query = query.group_by(dimension, name).order_by(dimension)
    if limit is not None:
        query = query.limit(limit).offset(offset)
    rows = [dict(row) for row in (await db.execute(query)).mappings()]
    costs = select(base.c.currency, func.sum(base.c.cost).label("amount")).where(
        base.c.cost.is_not(None), base.c.currency.is_not(None),
    ).group_by(base.c.currency)
    if dimension is not None:
        costs = costs.add_columns(dimension.label("id")).where(
            dimension.in_([row["id"] for row in rows]),
        ).group_by(dimension)
    amounts = {}
    for row in (await db.execute(costs)).mappings():
        amounts.setdefault(row.get("id"), []).append({"currency": row["currency"], "amount": format(row["amount"].normalize(), "f")})
    for row in rows:
        row["costs"] = sorted(amounts.get(row.get("id"), []), key=lambda cost: cost["currency"])
    return rows


async def usage_report(db, base, group_by, limit, offset):
    summary = (await aggregate(db, base))[0]
    timestamp = func.timezone("UTC", base.c.created_at) if db.bind.dialect.name == "postgresql" else base.c.created_at
    day = func.date(timestamp)
    daily = await aggregate(db, base, day, day)
    daily = [{"date": str(row["id"]), **{k: v for k, v in row.items() if k not in ("id", "name")}} for row in daily]
    dimension = base.c[f"{group_by}_id"]
    groups = await aggregate(db, base, dimension, base.c[f"{group_by}_name"], limit=limit, offset=offset)
    count = await db.scalar(select(func.count(func.distinct(dimension))).select_from(base))
    return {"summary": summary, "daily": daily, "groups": groups, "total_groups": count, "limit": limit, "offset": offset}
