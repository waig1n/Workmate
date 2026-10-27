"""Department sharing: preview, explicit confirmation, and independent revoke."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth import get_access_decision, get_current_user
from ...db import get_db
from workmate_product.persistence.models import User
from ...services.access_decision import AccessDecisionService
from ...services import resource_sharing as sharing

router = APIRouter(prefix='/sharing', tags=['sharing'])
Kind = Literal['agents', 'skills', 'mcps', 'knowledge-bases']


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_scope_id: str
    confirmation: str | None = None
    allow_download: bool = False
    include_descendants: bool | None = None


@router.get('/{kind}/{resource_id}')
async def get_sharing(kind: Kind, resource_id: str, user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db), access: AccessDecisionService = Depends(get_access_decision)):
    resource = await sharing.managed_resource(db, access, user.id, kind, resource_id)
    targets = await sharing.target_departments(db, resource.scope_id, kind)
    grants = await sharing.existing_shares(db, kind, resource_id)
    return {'targets': [{'id': s.id, 'name': '全组织可用' if s.kind == 'organization' else s.name,
                         'kind': s.kind, 'parent_id': s.parent_id} for s in targets],
            'shared_scope_ids': sorted(g.target_scope_id for g in grants),
            'grants': [{'target_scope_id': g.target_scope_id, 'include_descendants': g.include_descendants, 'allow_download': g.allow_download} for g in grants]}


@router.post('/{kind}/{resource_id}/preview')
async def preview(kind: Kind, resource_id: str, body: ShareRequest, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db), access: AccessDecisionService = Depends(get_access_decision)):
    resource = await sharing.managed_resource(db, access, user.id, kind, resource_id)
    return await sharing.build_plan(db, access, user.id, kind, resource, body.target_scope_id, allow_download=body.allow_download, include_descendants=body.include_descendants)


@router.post('/{kind}/{resource_id}')
async def share(kind: Kind, resource_id: str, body: ShareRequest, user: User = Depends(get_current_user),
                db: AsyncSession = Depends(get_db), access: AccessDecisionService = Depends(get_access_decision)):
    await sharing.lock_sharing(db)
    resource = await sharing.managed_resource(db, access, user.id, kind, resource_id)
    plan = await sharing.build_plan(db, access, user.id, kind, resource, body.target_scope_id, allow_download=body.allow_download, include_descendants=body.include_descendants)
    if plan['blockers']:
        raise HTTPException(409, {'message': '无法共享，请先处理依赖', 'blockers': plan['blockers']})
    if body.confirmation != plan['confirmation']:
        raise HTTPException(409, '请重新预览并确认共享内容')
    await sharing.apply_plan(db, plan, kind, user.id)
    await db.commit()
    return plan


@router.delete('/{kind}/{resource_id}/{target_scope_id}', status_code=204)
async def unshare(kind: Kind, resource_id: str, target_scope_id: str, user: User = Depends(get_current_user),
                  db: AsyncSession = Depends(get_db), access: AccessDecisionService = Depends(get_access_decision)):
    await sharing.lock_sharing(db)
    await sharing.managed_resource(db, access, user.id, kind, resource_id)
    await sharing.revoke(db, kind, resource_id, target_scope_id, user.id)
    await db.commit()


class BatchShareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    targets: list[ShareRequest] = Field(min_length=1, max_length=100)


@router.post('/{kind}/{resource_id}/batch')
async def share_batch(kind: Kind, resource_id: str, body: BatchShareRequest,
                      user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
                      access: AccessDecisionService = Depends(get_access_decision)):
    if len({target.target_scope_id for target in body.targets}) != len(body.targets):
        raise HTTPException(422, '不能重复选择部门')
    await sharing.lock_sharing(db)
    resource = await sharing.managed_resource(db, access, user.id, kind, resource_id)
    plans = []
    for target in body.targets:
        plan = await sharing.build_plan(db, access, user.id, kind, resource, target.target_scope_id, allow_download=target.allow_download, include_descendants=target.include_descendants)
        if plan['blockers']:
            raise HTTPException(409, {'message': '无法共享，请先处理依赖', 'blockers': plan['blockers']})
        if target.confirmation != plan['confirmation']:
            raise HTTPException(409, '请重新预览并确认共享内容')
        plans.append(plan)
    for plan in plans:
        await sharing.apply_plan(db, plan, kind, user.id)
    await db.commit()
    return {'plans': plans}


@router.get('/{kind}/{resource_id}/access')
async def own_access(kind: Kind, resource_id: str, user: User = Depends(get_current_user),
                     db: AsyncSession = Depends(get_db), access: AccessDecisionService = Depends(get_access_decision)):
    result = await sharing.access_sources(db, access, user.id, kind, resource_id)
    if result is None:
        raise HTTPException(404, 'Resource not found')
    return result
