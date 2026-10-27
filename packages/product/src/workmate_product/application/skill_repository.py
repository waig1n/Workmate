"""Transactional Skill artifact storage and current installation governance.

No HTTP surface is exposed. Bytes may be read only after the caller resolves
current Product authority and a pinned execution descriptor (or manage access).
"""
import uuid
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .authorization import AccessDecisionService
from .execution_context import _fresh_identity_map
from .skill_artifacts import SkillArtifactError,parse_skill_archive
from ..skill_storage import get_skill_blob_store, key_for_digest
from ..authorization.permissions import SKILL_MANAGE
from ..persistence.models import (
    CapabilityInstallation,CapabilitySkillArtifact,Scope,SecurityAuditEvent,SkillArtifactRecord,User,
)


@dataclass(frozen=True)
class SkillArtifactMetadata:
    digest: str
    format_version: int
    storage_key: str
    size_bytes: int
    manifest: tuple
    name: str
    description: str
    version: str | None

    def validate(self, artifact):
        if (artifact.digest != self.digest or len(artifact.archive) != self.size_bytes
                or artifact.manifest != self.manifest
                or (artifact.name, artifact.description, artifact.version) !=
                   (self.name, self.description, self.version)):
            raise SkillArtifactError('Skill artifact integrity check failed')


def _metadata(row):
    if row.storage_key != key_for_digest(row.digest):
        raise SkillArtifactError('Skill object key differs from pinned digest')
    return SkillArtifactMetadata(row.digest, row.format_version, row.storage_key, row.size_bytes,
        tuple(tuple(item) for item in row.manifest), row.name, row.description, row.version)


async def load_skill_artifact_metadata(db, digest):
    """Metadata only, for a caller that already authorized a pinned Skill."""
    key_for_digest(digest)
    row = await db.scalar(select(SkillArtifactRecord)
        .where(SkillArtifactRecord.digest == digest).execution_options(populate_existing=True))
    if row is None or row.format_version != 1:
        raise SkillArtifactError('Pinned Skill artifact is unavailable')
    return _metadata(row)


async def load_skill_artifact(db,digest):
    metadata = await load_skill_artifact_metadata(db, digest)
    raw = await get_skill_blob_store(db).get_verified(digest, metadata.size_bytes)
    artifact = parse_skill_archive(raw)
    metadata.validate(artifact)
    return artifact


@_fresh_identity_map
async def record_skill_artifact(db,raw):
    """Trusted package ingestion; joins caller transaction, with no installation grant."""
    artifact=parse_skill_archive(raw)
    # An interrupted/rolled-back DB transaction may leave an orphan object, but
    # can never publish a ready pointer before verified bytes exist.
    storage_key = await get_skill_blob_store(db).put_verified(artifact.digest, artifact.archive)
    if storage_key != key_for_digest(artifact.digest):
        raise SkillArtifactError('Skill object storage returned an invalid key')
    await db.execute(insert(SkillArtifactRecord).values(digest=artifact.digest,format_version=1,
        storage_key=storage_key,size_bytes=len(raw),
        manifest=[list(item) for item in artifact.manifest],name=artifact.name,
        description=artifact.description,version=artifact.version).on_conflict_do_nothing(index_elements=['digest']))
    return await load_skill_artifact(db,artifact.digest)


@_fresh_identity_map
async def bind_skill_artifact(db,*,capability_id,digest,actor_user_id):
    """Set a ready artifact pointer only with current skill.manage authority.

    Scope -> installation locks match Product capability management. Callers
    creating an installation and binding must keep their audit/receipt commit in
    this same transaction. Existing execution specs keep their immutable digest.
    """
    candidate=await db.scalar(select(CapabilityInstallation.scope_id).where(CapabilityInstallation.id==capability_id))
    scope=await db.scalar(select(Scope).where(Scope.id==candidate).with_for_update())
    capability=await db.scalar(select(CapabilityInstallation).where(CapabilityInstallation.id==capability_id).with_for_update())
    actor=await db.get(User,actor_user_id)
    if (scope is None or capability is None or capability.scope_id!=scope.id or scope.kind=='personal'
            or capability.capability_type!='skill' or capability.enabled is not True
            or actor is None or actor.status!='active'
            or not await AccessDecisionService(db).has_permission(actor_user_id,scope.id,SKILL_MANAGE)):
        raise SkillArtifactError('Current Skill installation authority is unavailable')
    artifact=await load_skill_artifact(db,digest)
    binding=await db.scalar(select(CapabilitySkillArtifact).where(CapabilitySkillArtifact.capability_id==capability_id).with_for_update())
    previous=binding.artifact_digest if binding else None
    if previous==digest:
        return digest
    if binding is None:
        db.add(CapabilitySkillArtifact(capability_id=capability_id,artifact_digest=digest))
    else:
        binding.artifact_digest=digest
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='capability.skill_artifact_bound',
        actor_user_id=actor_user_id,scope_id=scope.id,metadata_={'capability_id':capability_id,
            'artifact_digest':digest,'previous_artifact_digest':previous}))
    await db.flush()
    return digest


async def ready_skill_artifacts(db,capabilities):
    """Return current ready pointers, never package bytes, for authorized bindings."""
    identifiers={c.id for c in capabilities if c.capability_type=='skill'}
    if not identifiers:
        return {}
    rows=(await db.execute(select(CapabilitySkillArtifact.capability_id,CapabilitySkillArtifact.artifact_digest)
        .join(SkillArtifactRecord,SkillArtifactRecord.digest==CapabilitySkillArtifact.artifact_digest)
        .where(CapabilitySkillArtifact.capability_id.in_(identifiers),SkillArtifactRecord.format_version==1))).all()
    result=dict(rows)
    if set(result)!=identifiers:
        raise SkillArtifactError('A bound Skill has no ready immutable artifact')
    return result
