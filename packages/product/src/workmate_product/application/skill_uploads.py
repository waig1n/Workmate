"""Persistent uploaded catalogs governed by current scope authority."""
from pathlib import PurePath
from sqlalchemy import select
from .execution_context import _fresh_identity_map
from .skill_installation import install_skill_artifact, _managed_scope, SkillInstallationError
from .skill_artifacts import parse_skill_archive, MAX_ARCHIVE_BYTES
from .skill_repository import load_skill_artifact
from .authorization import AccessDecisionService
from ..authorization.permissions import SKILL_MANAGE
from ..persistence.models import SkillUploadSource, Scope, ScopeClosure, User
from ..skills.catalog import SkillCard, page_cards


@_fresh_identity_map
async def upload_skill_archive(db, *, actor_user_id, scope_id, raw, filename,
                               display_name=None, description=None, creation_channel="upload"):
    if creation_channel not in {'upload', 'assistant'}:
        raise SkillInstallationError(422,'Invalid Skill creation channel')
    scope_id=(await _managed_scope(db,actor_user_id,scope_id)).id
    if PurePath(filename).suffix.lower()!='.zip':
        raise SkillInstallationError(422,'Only .zip Skill archives are supported')
    if len(raw)>MAX_ARCHIVE_BYTES:
        raise SkillInstallationError(413,'Skill archive exceeds 20 MiB')
    # Validation, authorization and registration all stay inside caller commit.
    parsed=parse_skill_archive(raw)
    card_id=f'{scope_id}:{parsed.digest}'
    installation=await install_skill_artifact(db,actor_user_id=actor_user_id,scope_id=scope_id,
        raw=raw,hub_id='uploaded',card_id=card_id,display_name=display_name,description=description)
    installation.metadata_={**installation.metadata_, 'creation_channel': creation_channel}
    installation=await db.merge(installation)
    source=await db.get(SkillUploadSource,(scope_id,parsed.digest))
    card=SkillCard(hub_id='uploaded',id=card_id,name=parsed.name,display_name=installation.display_name,
        description=installation.metadata_['description'],tags=installation.metadata_['tags'],version=parsed.version)
    if source is None:
        db.add(SkillUploadSource(scope_id=scope_id,card_id=parsed.digest,artifact_digest=parsed.digest,
            uploaded_by=actor_user_id,card=card.model_dump()))
    else:
        source.card=card.model_dump()
    await db.flush()
    return installation


@_fresh_identity_map
async def list_uploaded_skills(db, *, actor_user_id, q=None, cursor=None, limit=20):
    actor=await db.get(User,actor_user_id)
    if actor is None or actor.status!='active':
        raise SkillInstallationError(404,'Uploaded catalog not found')
    # Uploaded packages are scope library sources, shared only with current managers.
    access=AccessDecisionService(db)
    cards=[]
    root=await db.scalar(select(Scope).where(Scope.kind=='organization',Scope.parent_id.is_(None)))
    if root is None or not await access.has_permission(actor_user_id,root.id,SKILL_MANAGE):
        return page_cards(cards,q,cursor,limit)
    allowed=set((await db.scalars(select(ScopeClosure.descendant_id).join(
        Scope,Scope.id==ScopeClosure.descendant_id).where(
        ScopeClosure.ancestor_id==root.id,Scope.kind.in_(['organization','department'])))).all())
    allowed.add(root.id)
    rows=(await db.scalars(select(SkillUploadSource).order_by(SkillUploadSource.created_at,SkillUploadSource.scope_id,SkillUploadSource.card_id))).all()
    for source in rows:
        if source.scope_id in allowed:
            cards.append(SkillCard.model_validate(source.card))
    return page_cards(cards,q,cursor,limit)


@_fresh_identity_map
async def load_uploaded_skill(db, *, actor_user_id, card_id):
    scope_id,separator,digest=card_id.partition(':')
    if not separator:
        raise SkillInstallationError(404,'Uploaded Skill not found')
    # Historical department catalog IDs survive centralization. Management is
    # now organization-owned, while source identity and immutable bytes stay put.
    root=await _managed_scope(db,actor_user_id,None,lock=False)
    source_scope=await db.get(Scope,scope_id)
    if (source_scope is None or source_scope.kind not in {'organization','department'}
            or (scope_id!=root.id and await db.get(ScopeClosure,(root.id,scope_id)) is None)):
        raise SkillInstallationError(404,'Uploaded Skill not found')
    source=await db.get(SkillUploadSource,(scope_id,digest))
    if source is None:
        raise SkillInstallationError(404,'Uploaded Skill not found')
    return SkillCard.model_validate(source.card),await load_skill_artifact(db,source.artifact_digest)
