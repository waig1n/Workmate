"""Versioned organization settings; department delegation cannot rename the root."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from workmate_product.persistence.models import Scope, ScopeKind, User

from ...auth import get_current_user
from ...db import get_db
from ...services.administration_guard import actor_authorized
from ...services.security_audit import SecurityAuditService

router = APIRouter(prefix='/admin/organization', tags=['admin-organization'])


class OrganizationSettings(BaseModel):
    id: str
    name: str
    tree_version: int


class OrganizationUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    expected_tree_version: int = Field(ge=1)

    @field_validator('name', mode='before')
    @classmethod
    def trim_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value.casefold()) > 200:
                raise ValueError('Organization name exceeds the normalized 200-character limit.')
        return value


def _view(root):
    return OrganizationSettings(id=root.id, name=root.name, tree_version=root.tree_version)


async def _organization(db, user_id, *, lock=False):
    query = select(Scope).where(Scope.kind == ScopeKind.ORGANIZATION)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    root = await db.scalar(query)
    if root is None:
        raise HTTPException(404, detail={'code': 'organization_not_found', 'message': 'Organization not found.'})
    # Recheck active identity and root authority after acquiring the same root
    # lock used by role changes and department structure writes.
    if not await actor_authorized(db, user_id, {'scope.manage'}, root.id):
        raise HTTPException(403, detail={'code': 'scope_manage_required',
                                         'message': 'Organization management permission required.'})
    return root


@router.get('', response_model=OrganizationSettings)
async def get_organization(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return _view(await _organization(db, user.id))


@router.patch('', response_model=OrganizationSettings)
async def update_organization(body: OrganizationUpdate, user: User = Depends(get_current_user),
                              db: AsyncSession = Depends(get_db)):
    root = await _organization(db, user.id, lock=True)
    if root.tree_version != body.expected_tree_version:
        raise HTTPException(409, detail={'code': 'version_conflict',
            'message': 'Organization structure changed; reload before saving.'})
    old_name = root.name
    root.name = body.name
    root.normalized_name = body.name.casefold()
    root.tree_version += 1
    SecurityAuditService(db).record('organization.rename', actor_user_id=user.id, scope_id=root.id,
        metadata={'old_name': old_name, 'new_name': root.name, 'tree_version': root.tree_version})
    response = _view(root)
    await db.commit()
    return response
