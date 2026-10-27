"""Minimal directory projections in the caller-owned transaction."""
from sqlalchemy import or_, select
from .models import Scope, ScopeClosure, ScopeMembership, User


class UserProfileRepository:
    def __init__(self, db):
        self.db = db

    async def shared_scopes(self):
        return list((await self.db.scalars(select(Scope).where(
            Scope.kind.in_(('organization', 'department'))).order_by(Scope.id))).all())

    async def descendants(self, scope_id):
        return set((await self.db.scalars(select(ScopeClosure.descendant_id).where(
            ScopeClosure.ancestor_id == scope_id))).all())

    async def departments(self, user_ids, allowed_scope_ids=None):
        query = select(ScopeMembership.user_id, Scope.id, Scope.name).join(
            Scope, Scope.id == ScopeMembership.scope_id).where(
            ScopeMembership.user_id.in_(user_ids), Scope.kind == 'department')
        if allowed_scope_ids is not None:
            query = query.where(Scope.id.in_(allowed_scope_ids))
        rows = (await self.db.execute(query.order_by(Scope.name, Scope.id))).all()
        return {uid: [{'id': sid, 'name': name} for member, sid, name in rows if member == uid]
                for uid in user_ids}

    async def page(self, scope_ids, *, q, cursor, limit, user_id=None):
        # Authorization scope filtering precedes both search and pagination.
        query = select(User).where(User.id.in_(select(ScopeMembership.user_id).where(
            ScopeMembership.scope_id.in_(scope_ids))))
        if user_id is not None:
            query = query.where(User.id == user_id)
        if q:
            escaped = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            query = query.where(or_(User.display_name.ilike(f'%{escaped}%', escape='\\'),
                                   User.email.ilike(f'%{escaped}%', escape='\\')))
        if cursor:
            query = query.where(User.id > cursor)
        rows = list((await self.db.scalars(query.order_by(User.id).limit(limit + 1))).all())
        return rows[:limit], rows[limit - 1].id if len(rows) > limit else None
