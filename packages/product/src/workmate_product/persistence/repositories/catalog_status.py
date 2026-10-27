"""Page-local catalog queries on the caller Session; no transaction ownership.

Keep the EXISTS projection in one statement. Its ORM expressions retain existing
Session autoflush behavior; authorization decisions are supplied by Application.
"""
from sqlalchemy import and_, or_, select

from ..models import (
    CapabilityInstallation,
    CapabilityType,
    ResourceShare,
    Scope,
    ScopeClosure,
    ScopeKind,
    ScopeMembership,
)


def _skill_grant(user_id):
    memberships = select(ScopeMembership.scope_id).where(ScopeMembership.user_id == user_id)
    ancestors = (select(ScopeClosure.ancestor_id)
        .join(Scope, Scope.id == ScopeClosure.ancestor_id)
        .where(Scope.kind.in_((ScopeKind.ORGANIZATION, ScopeKind.DEPARTMENT)),
               ScopeClosure.descendant_id.in_(memberships)))
    return select(ResourceShare.id).where(
        ResourceShare.capability_id == CapabilityInstallation.id,
        or_(ResourceShare.target_scope_id.in_(memberships),
            and_(ResourceShare.include_descendants.is_(True),
                 ResourceShare.target_scope_id.in_(ancestors))),
    ).correlate(CapabilityInstallation).exists()


async def catalog_scope(db, kind, scope_id):
    if kind == CapabilityType.SKILL:
        return await db.scalar(select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION))
    return await db.get(Scope, scope_id)


async def installed_flags(db, *, scope_id, kind, user_id, can_manage, hub_id, cards):
    conditions = [CapabilityInstallation.scope_id == scope_id,
                  CapabilityInstallation.capability_type == kind]
    if not can_manage:
        conditions.append(_skill_grant(user_id))
    checks = []
    for card in cards:
        same_card = and_(
            CapabilityInstallation.metadata_['hub_id'].as_string() == (card.get('hub_id') or hub_id),
            CapabilityInstallation.metadata_['card_id'].as_string() == card['id'],
        )
        checks.append(select(CapabilityInstallation.id).where(
            *conditions, or_(CapabilityInstallation.name == card['name'], same_card),
        ).exists())
    return [bool(flag) for flag in (await db.execute(select(*checks))).one()]
