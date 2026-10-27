"""Permission-filtered resource pages on the caller's Session.

Callers supply authorization sets; this module neither resolves permissions nor
opens, commits or rolls back transactions. Existing ORM autoflush is preserved.
"""

from sqlalchemy import and_, func, or_, select

from workmate_product.persistence.models import (
    Agent,
    CapabilityInstallation,
    Scope,
    ScopeKind,
    Visibility,
)
from workmate_product.persistence.models import KnowledgeBase


def contains_text(query):
    return '%' + query.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def search(statement, query, *columns):
    if query.strip():
        statement = statement.where(or_(*(column.ilike(contains_text(query), escape='\\') for column in columns)))
    return statement


async def nonpersonal_scope_ids(db):
    return set((await db.scalars(select(Scope.id).where(Scope.kind != ScopeKind.PERSONAL))).all())


async def rows(db, statement, model, page, page_size):
    total = await db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    statement = statement.order_by(model.created_at.desc(), model.id)
    statement = statement.offset((page - 1) * page_size).limit(page_size)
    return list((await db.scalars(statement)).all()), total


async def agents(db, user_id, *, managed, shared, page=1, page_size=12, q='', scope='all', category='all'):
    statement = select(Agent).join(Scope, Scope.id == Agent.scope_id).where(
        Agent.deleted_at.is_(None),
        or_(and_(Scope.kind == ScopeKind.PERSONAL, Scope.owner_user_id == user_id),
            and_(Scope.kind != ScopeKind.PERSONAL,
                 or_(Agent.created_by == user_id, Agent.scope_id.in_(managed),
                     and_(Agent.visibility == Visibility.PUBLIC, Agent.id.in_(shared))))),
    )
    if scope == 'personal' or category == 'mine':
        statement = statement.where(Scope.kind == ScopeKind.PERSONAL)
    if scope == 'organization' or category == 'shared':
        statement = statement.where(Scope.kind != ScopeKind.PERSONAL)
    if category == 'builtin':
        statement = statement.where(Agent.system_key.is_not(None))
    elif category == 'shared':
        statement = statement.where(Agent.system_key.is_(None))
    statement = search(statement, q, Agent.display_name, Agent.slug, Agent.description, Scope.name)
    return await rows(db, statement, Agent, page, page_size)


async def capabilities(db, kind, *, managed, usable, page=1, page_size=12, q='', source='all'):
    statement = select(CapabilityInstallation).join(Scope, Scope.id == CapabilityInstallation.scope_id).where(
        Scope.kind != ScopeKind.PERSONAL, CapabilityInstallation.capability_type == kind,
        or_(CapabilityInstallation.scope_id.in_(managed), CapabilityInstallation.id.in_(usable)),
    )
    hub = CapabilityInstallation.metadata_['hub_id'].as_string()
    if kind == 'mcp' and source == 'custom':
        statement = statement.where(hub == 'workmate-custom-http')
    elif kind == 'mcp' and source == 'catalog':
        statement = statement.where(or_(hub.is_(None), hub != 'workmate-custom-http'))
    elif source == 'builtin':
        statement = statement.where(hub == 'workmate-builtin')
    elif source == 'uploaded':
        statement = statement.where(hub == 'uploaded')
    elif source == 'third-party':
        statement = statement.where(or_(hub.is_(None), hub.not_in(['uploaded', 'workmate-builtin'])))
    statement = search(statement, q, CapabilityInstallation.name, CapabilityInstallation.display_name,
                       CapabilityInstallation.metadata_['description'].as_string())
    return await rows(db, statement, CapabilityInstallation, page, page_size)


async def knowledge(db, user_id, *, managed, usable, page=1, page_size=12, q='', view='all', allowed=None):
    statement = select(KnowledgeBase).join(Scope, Scope.id == KnowledgeBase.scope_id).where(
        or_(KnowledgeBase.id.in_(usable | (allowed or set())), KnowledgeBase.scope_id.in_(managed)),
    )
    if view == 'personal':
        statement = statement.where(
            Scope.kind == ScopeKind.PERSONAL, Scope.owner_user_id == user_id,
        )
    elif view == 'shared':
        statement = statement.where(
            Scope.kind != ScopeKind.PERSONAL, KnowledgeBase.id.in_(usable),
        )
    elif view == 'managed':
        statement = statement.where(
            Scope.kind != ScopeKind.PERSONAL, KnowledgeBase.scope_id.in_(managed),
        )
    if allowed is not None:
        statement = statement.where(KnowledgeBase.id.in_(allowed), KnowledgeBase.enabled)
    statement = search(statement, q, KnowledgeBase.name, KnowledgeBase.description)
    return await rows(db, statement, KnowledgeBase, page, page_size)
