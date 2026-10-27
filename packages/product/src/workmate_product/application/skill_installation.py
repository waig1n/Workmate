"""Governed ready Skill installation; every write joins the caller's transaction."""
import uuid
import yaml
from sqlalchemy import select, or_
from .authorization import AccessDecisionService
from .execution_context import _fresh_identity_map
from .skill_artifacts import SkillArtifactError, parse_skill_archive
from .skill_repository import record_skill_artifact, bind_skill_artifact
from ..authorization.permissions import SKILL_MANAGE
from ..persistence.models import AgentCapabilityBinding, CapabilityInstallation, Scope, User, SecurityAuditEvent


class SkillInstallationError(ValueError):
    def __init__(self,status_code,detail):
        self.status_code,self.detail=status_code,detail
        super().__init__(detail)


async def _managed_scope(db, actor_user_id, scope_id, *, lock=True):
    root=await db.scalar(select(Scope).where(Scope.kind=='organization',Scope.parent_id.is_(None)))
    if root is None:
        raise SkillInstallationError(404,'Organization scope not found')
    if scope_id is not None and scope_id!=root.id:
        raise SkillInstallationError(422,'Skills must be installed in the organization scope')
    scope_id=root.id
    query=select(Scope).where(Scope.id==scope_id)
    scope=await db.scalar(query.with_for_update() if lock else query)
    actor=await db.get(User,actor_user_id)
    if scope is None or actor is None or actor.status!='active':
        raise SkillInstallationError(404,'Scope not found')
    if scope.kind=='personal':
        raise SkillInstallationError(422,'Capabilities cannot be installed in Personal scope')
    access=AccessDecisionService(db)
    if not await access.has_permission(actor_user_id,scope.id,SKILL_MANAGE):
        visible=scope.id in await access.accessible_capability_scope_ids(actor_user_id)
        raise SkillInstallationError(403 if visible else 404,'Permission required in this scope: skill.manage')
    return scope


@_fresh_identity_map
async def authorize_skill_install(db, *, actor_user_id, scope_id):
    """Early request authorization; mutations always recheck after source I/O."""
    return (await _managed_scope(db,actor_user_id,scope_id)).id


@_fresh_identity_map
async def install_skill_artifact(db, *, actor_user_id, scope_id, raw, hub_id, card_id,
                                 name=None, display_name=None, description=None, tags=None):
    """Return only after package validation, registry, ready pointer and audit exist.

    The caller commits once, optionally with a fenced ToolExecutionReceipt. No
    App call or independently committed resource job occurs here. Public entry
    points prepare a request-scoped verified object store before this transaction;
    trusted direct callers may publish through their injected store here.
    ``raw`` must come from a server-selected catalog or an authorized upload,
    never from a caller-provided digest used as an authorization capability.
    """
    scope=await _managed_scope(db,actor_user_id,scope_id)
    if hub_id=='workmate-builtin':
        raise SkillInstallationError(422,'Built-in Skills are registered automatically')
    try:
        parsed=parse_skill_archive(raw)
    except SkillArtifactError as exc:
        raise SkillInstallationError(422,str(exc)) from exc
    lines=parsed.files['SKILL.md'].decode('utf-8').splitlines()
    metadata=yaml.safe_load('\n'.join(lines[1:lines.index('---',1)]))
    tags=tags if tags is not None else metadata.get('tags') or []
    display_name=display_name or metadata.get('display_name') or name or parsed.name
    if not isinstance(display_name,str) or len(display_name)>200:
        raise SkillInstallationError(422,'Skill display name must be at most 200 characters')
    effective_name=name or parsed.name
    if not isinstance(effective_name,str) or not 1 <= len(effective_name) <= 128:
        raise SkillInstallationError(422,'Skill installation name must contain 1-128 characters')
    if tags is not None and (not isinstance(tags,list) or not all(isinstance(t,str) for t in tags)):
        raise SkillInstallationError(422,'SKILL.md tags must be a string list')
    conflict=await db.scalar(select(CapabilityInstallation.id).where(
        CapabilityInstallation.scope_id==scope.id,CapabilityInstallation.capability_type=='skill',
        or_(CapabilityInstallation.name==effective_name,
            (CapabilityInstallation.metadata_['hub_id'].as_string()==hub_id) &
            (CapabilityInstallation.metadata_['card_id'].as_string()==card_id))))
    if conflict:
        raise SkillInstallationError(409,f'skill already installed in this scope: {effective_name}')
    artifact=await record_skill_artifact(db,raw)
    identifier=str(uuid.uuid4())
    installation=CapabilityInstallation(id=identifier,capability_type='skill',scope_id=scope.id,
        name=effective_name,display_name=display_name or effective_name,enabled=True,created_by=actor_user_id,
        metadata_={'description':description or artifact.description,'tags':tags or [],
                   'hub_id':hub_id,'card_id':card_id,'version':artifact.version,'is_stateful':None})
    db.add(installation)
    await db.flush()
    await bind_skill_artifact(db,capability_id=identifier,digest=artifact.digest,actor_user_id=actor_user_id)
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='capability.install',actor_user_id=actor_user_id,
        scope_id=scope.id,metadata_={'capability_id':identifier,'capability_type':'skill','artifact_digest':artifact.digest}))
    await db.flush()
    return installation


@_fresh_identity_map
async def uninstall_skill(db, *, actor_user_id, installation_id):
    scope_id=await db.scalar(select(CapabilityInstallation.scope_id).where(
        CapabilityInstallation.id==installation_id,CapabilityInstallation.capability_type=='skill'))
    scope=await _managed_scope(db,actor_user_id,scope_id)
    installation=await db.scalar(select(CapabilityInstallation).where(
        CapabilityInstallation.id==installation_id,CapabilityInstallation.capability_type=='skill').with_for_update())
    if installation is None or installation.scope_id!=scope.id:
        raise SkillInstallationError(404,'Skill not found')
    if (installation.metadata_ or {}).get('hub_id')=='workmate-builtin':
        raise SkillInstallationError(409,'Built-in Skills cannot be deleted; disable them instead')
    bound=await db.scalar(select(AgentCapabilityBinding.agent_id).where(
        AgentCapabilityBinding.capability_installation_id==installation_id).limit(1))
    if bound:
        raise SkillInstallationError(409,'Capability is bound to agents; remove the bindings first')
    await db.delete(installation)
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='capability.delete',actor_user_id=actor_user_id,
        scope_id=scope.id,metadata_={'capability_id':installation_id,'capability_type':'skill'}))
    await db.flush()


@_fresh_identity_map
async def set_skill_enabled(db, *, actor_user_id, installation_id, enabled):
    from .resource_sharing import lock_sharing
    await lock_sharing(db)
    installation=await db.scalar(select(CapabilityInstallation).where(
        CapabilityInstallation.id==installation_id,CapabilityInstallation.capability_type=='skill').with_for_update())
    if installation is None:
        raise SkillInstallationError(404,'Skill not found')
    await _managed_scope(db,actor_user_id,installation.scope_id)
    if not enabled and await db.scalar(select(AgentCapabilityBinding.agent_id).where(
            AgentCapabilityBinding.capability_installation_id==installation_id).limit(1)):
        raise SkillInstallationError(409,'Capability is bound to agents; remove the bindings first')
    installation.enabled=enabled
    db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='capability.update',actor_user_id=actor_user_id,
        scope_id=installation.scope_id,metadata_={'capability_id':installation_id,'enabled':enabled}))
    await db.flush()
    return installation
