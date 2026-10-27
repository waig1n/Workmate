"""Explicit operator opt-in for existing installations; never called at startup."""
import uuid

from sqlalchemy import select
from workmate_product.application.resource_sharing import grant
from workmate_product.persistence.models import (
    Agent, AgentCapabilityBinding, CapabilityInstallation, CapabilitySkillArtifact,
    ResourceShare, Scope, SecurityAuditEvent, SkillArtifactRecord,
)
from workmate_product.skills.catalog import BuiltinCatalog
from ..config import get_settings
from .bootstrap import get_root_scope
from .workspace_assistant import BUILTIN_HUB_ID, INITIAL_SKILL_IDS, WORKSPACE_ASSISTANT_KEY


async def apply_builtin_defaults(db, *, apply=False, catalog=None):
    """Preview or atomically add defaults without replacing content or bindings.

    Explicit apply can restore revoked general-Skill grants. It never enables
    disabled Skills, changes prompts, publishes an Agent or removes bindings.
    """
    async with db.begin_nested():
        return await _defaults(db, apply=apply, catalog=catalog or BuiltinCatalog())


async def _defaults(db, *, apply, catalog):
    root=await get_root_scope(db)
    if root is None:
        raise ValueError('Root organization is missing; complete gateway initialization first')
    if apply:
        await db.execute(select(Scope.id).where(Scope.id==root.id).with_for_update())
    names=set()
    configured=set(get_settings().builtin_skill_organization_use_ids)
    cursor=None
    seen=set()
    while True:
        page=await catalog.list_skills('workmate-defaults',None,cursor,100)
        names.update(card.id for card in page.cards)
        cursor=page.next_cursor
        if not cursor:
            break
        if cursor in seen:
            raise ValueError('Builtin catalog returned a repeated cursor')
        seen.add(cursor)
    unknown=configured-names
    if unknown:
        raise ValueError(f'Unknown builtin Skill IDs: {sorted(unknown)}')
    names &= configured
    installations=(await db.scalars(select(CapabilityInstallation).where(
        CapabilityInstallation.scope_id==root.id,CapabilityInstallation.capability_type=='skill',
        CapabilityInstallation.metadata_['hub_id'].as_string()==BUILTIN_HUB_ID))).all()
    installed={item.metadata_.get('card_id'):item for item in installations}
    shares=set((await db.scalars(select(ResourceShare.capability_id).where(
        ResourceShare.target_scope_id==root.id,ResourceShare.include_descendants))).all())
    ready=dict((await db.execute(select(CapabilitySkillArtifact.capability_id,SkillArtifactRecord.name)
        .join(SkillArtifactRecord,SkillArtifactRecord.digest==CapabilitySkillArtifact.artifact_digest)
        .where(CapabilitySkillArtifact.capability_id.in_([item.id for item in installations]),
               SkillArtifactRecord.format_version==1))).all())
    assistant=await db.scalar(select(Agent).where(Agent.scope_id==root.id,
        Agent.system_key==WORKSPACE_ASSISTANT_KEY,Agent.deleted_at.is_(None)))
    bound=set((await db.scalars(select(AgentCapabilityBinding.capability_installation_id)
        .where(AgentCapabilityBinding.agent_id==assistant.id))).all()) if assistant else set()
    report={'organization_id':root.id,'assistant_id':assistant.id if assistant else None,
            'grant_skills':[],'bind_skills':[],'skipped_disabled':[],'blockers':[],'warnings':[],'applied':False}
    if assistant is None:
        report['warnings'].append('Platform assistant is missing; only Skill grants are applied. Restart gateway with the assistant enabled to create it with default bindings.')
    for name in sorted(names | set(INITIAL_SKILL_IDS)):
        item=installed.get(name)
        if item is None or ready.get(item.id)!=name:
            report['blockers'].append(f'{name}: missing or invalid builtin artifact; complete gateway initialization')
            continue
        if not item.enabled:
            report['skipped_disabled'].append(name)
            if name in INITIAL_SKILL_IDS:
                report['blockers'].append(f'{name}: required default Skill is disabled')
            continue
        if item.id not in shares:
            if name not in names:
                report['blockers'].append(f'{name}: required Skill is not approved for organization-wide default use')
            else:
                report['grant_skills'].append(name)
        if assistant and name in INITIAL_SKILL_IDS and item.id not in bound:
            report['bind_skills'].append(name)
    if not apply or report['blockers']:
        return report
    for name in report['grant_skills']:
        await grant(db,'skills',installed[name].id,root.id,None,include_descendants=True)
    for name in report['bind_skills']:
        db.add(AgentCapabilityBinding(agent_id=assistant.id,capability_installation_id=installed[name].id))
    if report['grant_skills'] or report['bind_skills']:
        db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='builtin.defaults_applied',
            actor_user_id=None,scope_id=root.id,metadata_={**report,'applied':True,'source':'operator_command'}))
    await db.flush()
    return {**report,'applied':True}
