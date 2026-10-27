"""Read-only, scope-filtered control-plane security audit."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ...auth import get_current_user
from ...db import get_db
from workmate_product.persistence.models import Scope, SecurityAuditEvent, User
from ...services.access_decision import AccessDecisionService
from ...services.bootstrap import get_root_scope
from ...services.security_audit import _sanitize
from ...services.user_management import UserManagementError, _decode_cursor, _encode_cursor

router = APIRouter(prefix="/admin/security-audit-events", tags=["admin-audit"])


@router.get("")
async def list_events(
    scope_id: str | None = None,
    target_user_id: str | None = None,
    actor_user_id: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    access = AccessDecisionService(db)
    matrix = await access.permissions_for_scopes(user.id, await access.accessible_scope_ids(user.id))
    allowed = {sid for sid, keys in matrix.items() if "audit.read" in keys}
    if not allowed or (scope_id is not None and scope_id not in allowed):
        raise HTTPException(status_code=403, detail={
            "code": "permission_required", "message": "Audit access is not allowed in this scope.",
        })
    root = await get_root_scope(db)
    global_reader = bool(root and root.id in allowed)
    actor, target = aliased(User), aliased(User)
    query = select(SecurityAuditEvent, actor.display_name, target.display_name, Scope.name).outerjoin(
        actor, actor.id == SecurityAuditEvent.actor_user_id,
    ).outerjoin(target, target.id == SecurityAuditEvent.target_user_id).outerjoin(
        Scope, Scope.id == SecurityAuditEvent.scope_id,
    )
    visible = SecurityAuditEvent.scope_id.in_(allowed)
    if global_reader:
        visible = or_(visible, SecurityAuditEvent.scope_id.is_(None))
    query = query.where(visible)
    for column, value in (
        (SecurityAuditEvent.scope_id, scope_id),
        (SecurityAuditEvent.target_user_id, target_user_id),
        (SecurityAuditEvent.actor_user_id, actor_user_id),
        (SecurityAuditEvent.event_type, event_type),
    ):
        if value is not None:
            query = query.where(column == value)
    if since:
        query = query.where(SecurityAuditEvent.created_at >= since)
    if until:
        query = query.where(SecurityAuditEvent.created_at <= until)
    if cursor:
        try:
            at, event_id = _decode_cursor(cursor)
        except UserManagementError as error:
            raise HTTPException(status_code=422, detail={"code": error.code, "message": error.message}) from None
        query = query.where(or_(SecurityAuditEvent.created_at < at, and_(
            SecurityAuditEvent.created_at == at, SecurityAuditEvent.id < event_id,
        )))
    rows = list((await db.execute(query.order_by(
        SecurityAuditEvent.created_at.desc(), SecurityAuditEvent.id.desc(),
    ).limit(limit + 1))).all())
    next_cursor = _encode_cursor(rows[limit - 1][0].created_at, rows[limit - 1][0].id) if len(rows) > limit else None
    items = []
    for event, actor_name, target_name, scope_name in rows[:limit]:
        metadata = _sanitize(event.metadata_)
        if not global_reader:
            # Historical events may contain cross-scope access arrays. Do not
            # expose those merely because the event itself belongs to a department.
            metadata = {key: value for key, value in metadata.items() if key in {
                "name", "old_name", "new_name", "status", "role_key", "include_descendants", "failure",
            }}
        items.append({
            "id": event.id, "event_type": event.event_type,
            "actor_user_id": event.actor_user_id, "actor_name": actor_name,
            "target_user_id": event.target_user_id, "target_name": target_name,
            "scope_id": event.scope_id, "scope_name": scope_name,
            "request_id": event.request_id, "metadata": metadata, "created_at": event.created_at,
        })
    return {"items": items, "next_cursor": next_cursor}
