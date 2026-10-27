# -*- coding: utf-8 -*-
"""Product-facing Skill Hub and scoped installation API (RBAC §11.3)."""

from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Query, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict
from ...auth import get_access_decision, get_current_user
from ...db import get_db
from workmate_product.persistence.models import CapabilityType, Scope, User
from ...services import capabilities as caps
from workmate_product.application import resource_lists, catalog_status
from ..pagination import ResourcePage, page_result
from ...services.access_decision import AccessDecisionService
from ..schemas import CapabilityListItem, SkillInstallRequest
from ._capabilities import capability_item
import httpx
from workmate_product.application.skill_installation import (
    SkillInstallationError,
    authorize_skill_install,
    install_skill_artifact,
    uninstall_skill,
    set_skill_enabled,
)
from workmate_product.application.skill_uploads import (
    upload_skill_archive,
    list_uploaded_skills,
    load_uploaded_skill,
)
from workmate_product.application.skill_artifacts import SkillArtifactError, MAX_ARCHIVE_BYTES
from workmate_product.application.skill_repository import load_skill_artifact
from workmate_product.skill_storage import PreparedSkillBlobStore, get_skill_blob_store
from workmate_product.persistence.models import CapabilitySkillArtifact
from workmate_product.skills.catalog import BuiltinCatalog, SkillCatalogError
from ...services.bootstrap import get_root_scope


router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=ResourcePage[CapabilityListItem])
async def list_installed(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    access: AccessDecisionService = Depends(get_access_decision),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 12,
    q: Annotated[str, Query(max_length=200)] = "",
    source: Literal["all", "uploaded", "builtin", "third-party"] = "all",
):
    pairs, total = await resource_lists.capabilities(
        db, access, user.id, CapabilityType.SKILL, page=page, page_size=page_size, q=q, source=source,
    )
    items = [capability_item(installation, manageable, usable, await db.get(Scope, installation.scope_id))
             for installation, manageable, usable in pairs]
    return page_result(items, total, page, page_size)


class SkillUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _hubs(request):
    return getattr(request.app.state,'skill_catalogs',{'workmate-builtin':BuiltinCatalog()})


def _hub(request,identifier):
    provider=_hubs(request).get(identifier)
    if provider is None:
        raise HTTPException(404,'Skill Hub not found')
    return provider


def _failure(exc):
    if isinstance(exc,(SkillCatalogError,SkillInstallationError)):
        status=exc.status_code if 400 <= exc.status_code < 500 else 502
        return HTTPException(status,exc.detail)
    if isinstance(exc,SkillArtifactError):
        return HTTPException(422,str(exc))
    if isinstance(exc,KeyError):
        return HTTPException(404,'Skill not found')
    return HTTPException(502,'Skill Hub request failed')


_ERRORS=(SkillCatalogError,SkillInstallationError,SkillArtifactError,KeyError,httpx.HTTPError)


@router.get('/hubs')
async def list_hubs(request:Request,user:User=Depends(get_current_user)):
    cards=[{'hub_id':h.hub_id,'display_name':h.display_name,'description':h.description,'icon_url':h.icon_url}
           for h in _hubs(request).values()]
    cards.append({'hub_id':'uploaded','display_name':'Uploaded Skills',
                  'description':'Skills uploaded as ZIP archives.','icon_url':None})
    return sorted(cards,key=lambda c:c['hub_id'])


@router.get('/catalog')
async def list_catalog(request:Request,hub_id:str=Query(...),q:str|None=None,cursor:str|None=None,
                       limit:int=Query(40,ge=1,le=100),user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    actor_id=user.id
    access=AccessDecisionService(db)
    root=await get_root_scope(db)
    if root is None or not await access.has_permission(actor_id,root.id,'skill.manage'):
        raise HTTPException(403,'skill.manage permission required in the organization scope')
    try:
        if hub_id=='uploaded':
            if not db.in_transaction():
                await db.begin()
            page=await list_uploaded_skills(db,actor_user_id=actor_id,q=q,cursor=cursor,limit=limit)
            return await catalog_status.annotate(db,access,actor_id,page.model_dump(mode="json"),hub_id,CapabilityType.SKILL)
        # Authentication may have opened a transaction; do not hold it over registry I/O.
        await db.rollback()
        page=await _hub(request,hub_id).list_skills(actor_id,q,cursor,limit)
        return await catalog_status.annotate(db,access,actor_id,page.model_dump(mode="json"),hub_id,CapabilityType.SKILL)
    except catalog_status.CatalogAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except _ERRORS as exc:
        raise _failure(exc) from exc


async def _view(db,installation,actor_id):
    access=AccessDecisionService(db)
    return capability_item(installation,True,installation.id in await access.accessible_capability_ids(actor_id),
                           await db.get(Scope,installation.scope_id))


@router.post('',response_model=CapabilityListItem,status_code=201)
async def install(body:SkillInstallRequest,request:Request,user:User=Depends(get_current_user),
                  db:AsyncSession=Depends(get_db)):
    actor_id=user.id
    if body.hub_id=='workmate-builtin':
        raise HTTPException(422,'Built-in Skills are registered automatically; manual import is not required')
    if body.hub_id=='skillhub-cn' and not body.confirmed:
        raise HTTPException(409,'Review the third-party Skill and explicitly confirm installation')
    try:
        if not db.in_transaction():
            await db.begin()
        await authorize_skill_install(db,actor_user_id=actor_id,scope_id=body.scope_id)
        await db.rollback()
        if body.hub_id=='uploaded':
            async with db.begin():
                card,artifact=await load_uploaded_skill(db,actor_user_id=actor_id,card_id=body.card_id)
            prepared = PreparedSkillBlobStore(get_skill_blob_store(db))
            await prepared.prepare(artifact.archive)
            db.info['skill_blob_store'] = prepared
            async with db.begin():
                installation=await install_skill_artifact(db,actor_user_id=actor_id,scope_id=body.scope_id,
                    raw=artifact.archive,hub_id='uploaded',card_id=card.id,name=body.name,
                    display_name=card.display_name,description=card.description,tags=card.tags)
        else:
            hub=_hub(request,body.hub_id)
            card=await hub.get_skill(actor_id,body.card_id)
            raw=await hub.archive(actor_id,body.card_id,card.version)
            prepared = PreparedSkillBlobStore(get_skill_blob_store(db))
            await prepared.prepare(raw)
            db.info['skill_blob_store'] = prepared
            async with db.begin():
                installation=await install_skill_artifact(db,actor_user_id=actor_id,scope_id=body.scope_id,
                    raw=raw,hub_id=body.hub_id,card_id=body.card_id,name=body.name,
                    display_name=card.display_name,description=card.description,tags=card.tags)
        return await _view(db,installation,actor_id)
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.post('/upload',response_model=CapabilityListItem,status_code=201)
async def upload(archive:UploadFile=File(...),scope_id:str|None=Form(None),display_name:str|None=Form(None),
                 description:str|None=Form(None),user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    actor_id=user.id
    try:
        if not db.in_transaction():
            await db.begin()
        await authorize_skill_install(db,actor_user_id=actor_id,scope_id=scope_id)
        await db.rollback()
        raw=await archive.read(MAX_ARCHIVE_BYTES+1)
        if len(raw)>MAX_ARCHIVE_BYTES:
            raise HTTPException(413,'Skill archive exceeds 20 MiB')
        prepared = PreparedSkillBlobStore(get_skill_blob_store(db))
        await prepared.prepare(raw)
        db.info['skill_blob_store'] = prepared
        async with db.begin():
            installation=await upload_skill_archive(db,actor_user_id=actor_id,scope_id=scope_id,raw=raw,
                filename=archive.filename or 'skill.zip',display_name=display_name,description=description)
        return await _view(db,installation,actor_id)
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.get('/{installation_id}/description')
async def skill_description(installation_id:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db),
                             access:AccessDecisionService=Depends(get_access_decision)):
    visible=await caps.list_visible(db,access,user,CapabilityType.SKILL)
    selected=next(((c,manageable) for c,manageable,_ in visible if c.id==installation_id),None)
    if selected is None:
        raise HTTPException(404,'Skill not found')
    installation,manageable=selected
    if not manageable:
        raise HTTPException(403,'skill.manage permission required to read Skill content')
    pointer=await db.get(CapabilitySkillArtifact,installation_id)
    if pointer is None:
        raise HTTPException(409,'Skill is not ready')
    try:
        artifact=await load_skill_artifact(db,pointer.artifact_digest)
    except _ERRORS as exc:
        raise _failure(exc) from exc
    return {'description':installation.metadata_.get('description') or artifact.description,
            'markdown':artifact.files['SKILL.md'].decode('utf-8')}


@router.delete('/{installation_id}',status_code=204)
async def uninstall(installation_id:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    actor_id=user.id
    try:
        if not db.in_transaction():
            await db.begin()
        await uninstall_skill(db,actor_user_id=actor_id,installation_id=installation_id)
        await db.commit()
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc


@router.patch('/{installation_id}',response_model=CapabilityListItem)
async def update(installation_id:str,body:SkillUpdateRequest,user:User=Depends(get_current_user),
                 db:AsyncSession=Depends(get_db)):
    actor_id=user.id
    try:
        installation=await set_skill_enabled(db,actor_user_id=actor_id,
            installation_id=installation_id,enabled=body.enabled)
        response=await _view(db,installation,actor_id)
        await db.commit()
        return response
    except _ERRORS as exc:
        await db.rollback()
        raise _failure(exc) from exc
