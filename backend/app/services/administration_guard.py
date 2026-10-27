"""Recheck the actor after the shared administration lock is acquired."""
from sqlalchemy import select

from workmate_product.persistence.models import Scope, ScopeKind, User, UserStatus
from .access_decision import AccessDecisionService


async def actor_authorized(db, actor_id, permissions, scope_id=None):
    if actor_id is None:  # Internal migration/bootstrap calls have no HTTP actor.
        return True
    actor = await db.scalar(select(User).where(User.id == actor_id)
        .execution_options(populate_existing=True))
    if actor is None or actor.status != UserStatus.ACTIVE:
        return False
    if scope_id is None:
        scope_id = await db.scalar(select(Scope.id).where(Scope.kind == ScopeKind.ORGANIZATION))
    grants = await AccessDecisionService(db).permissions_for_scope(actor_id, scope_id) if scope_id else set()
    return set(permissions) <= grants
