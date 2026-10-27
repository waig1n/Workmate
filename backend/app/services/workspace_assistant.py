"""Trusted startup provisioning with Product-owned immutable Skill packages.

This entry point is for application bootstrap, not a user-facing installation
API. It joins the caller transaction and never invokes Runtime HTTP. Existing
assistants remain administrator-owned; bootstrap does not repair their bindings.
"""
import uuid

from sqlalchemy import select
from workmate_product.application.skill_artifacts import SkillArtifactError
from workmate_product.application.skill_repository import record_skill_artifact,load_skill_artifact
from workmate_product.persistence.models import CapabilitySkillArtifact,SecurityAuditEvent
from workmate_product.skills.catalog import BuiltinCatalog
from workmate_product.persistence.models import (
    Agent,
    AgentCapabilityBinding,
    CapabilityInstallation,
    Scope,
    Visibility,
    ResourceShare,
)
from ..config import get_settings
from ..prompts import DEFAULT_WORKSPACE_ASSISTANT_PROMPT
from .bootstrap import get_root_scope
from workmate_product.application.resource_sharing import grant

WORKSPACE_ASSISTANT_KEY='workspace_assistant'
INITIAL_SKILL_IDS=(
    'agent-bootstrap','chart-visualization','consulting-analysis','data-analysis',
    'deep-research','frontend-design','ppt-generation','skill-creator',
    'skill-reviewer',
)
BUILTIN_HUB_ID='workmate-builtin'


async def ensure_workspace_assistant(db,*,catalog=None):
    """Atomically create the ordinary organization assistant and its ready Skills.

    Builtin bytes are packaged from the shipped Product catalog. Bootstrap has
    no human actor and therefore does not impersonate an administrator to call
    governed install APIs. Only the fixed default assistant bindings use this path.
    A name collision never authorizes replacing an administrator's artifact.
    """
    async with db.begin_nested():
        root=await get_root_scope(db)
        if root is None:
            raise RuntimeError('root organization scope is missing')
        await db.execute(select(Scope.id).where(Scope.id==root.id).with_for_update())
        existing=await db.scalar(select(Agent).where(Agent.scope_id==root.id,
            Agent.system_key==WORKSPACE_ASSISTANT_KEY))
        if existing is not None:
            return existing
        catalog=catalog if catalog is not None else BuiltinCatalog()
        principal=f'workmate-builtin:{root.id}'
        installations=[]
        for name in INITIAL_SKILL_IDS:
            installation=await db.scalar(select(CapabilityInstallation).where(
                CapabilityInstallation.scope_id==root.id,CapabilityInstallation.capability_type=='skill',
                CapabilityInstallation.metadata_['hub_id'].as_string()==BUILTIN_HUB_ID,
                CapabilityInstallation.metadata_['card_id'].as_string()==name).with_for_update())
            if installation is not None:
                shared=await db.scalar(select(ResourceShare.id).where(
                    ResourceShare.capability_id==installation.id,
                    ResourceShare.target_scope_id==root.id,ResourceShare.include_descendants))
                if shared is None:
                    raise SkillArtifactError(f'Existing initial Skill lacks organization use grant: {name}')
                pointer=await db.get(CapabilitySkillArtifact,installation.id)
                metadata=installation.metadata_ or {}
                if pointer is None:
                    raise SkillArtifactError(f'Existing initial Skill has no ready artifact: {name}')
                if (not installation.enabled or metadata.get('hub_id')!=BUILTIN_HUB_ID
                        or metadata.get('card_id')!=name):
                    raise SkillArtifactError(f'Existing initial Skill has different source or is disabled: {name}')
                artifact=await load_skill_artifact(db,pointer.artifact_digest)
                if artifact.name!=name:
                    raise SkillArtifactError(f'Existing initial Skill artifact identity differs: {name}')
            else:
                if name not in get_settings().builtin_skill_organization_use_ids:
                    raise SkillArtifactError(f'Initial Skill is not approved for organization use: {name}')
                card=await catalog.get_skill(principal,name)
                if card.hub_id!=BUILTIN_HUB_ID or card.id!=name or card.name!=name:
                    raise SkillArtifactError('Initial Skill must come from its fixed builtin catalog identity')
                artifact=await record_skill_artifact(db,await catalog.archive(principal,name,card.version))
                if artifact.name!=name:
                    raise SkillArtifactError('Initial Skill package differs from its builtin catalog identity')
                identifier=str(uuid.uuid4())
                installation=CapabilityInstallation(id=identifier,capability_type='skill',scope_id=root.id,
                    name=name,
                    display_name=card.display_name or name,enabled=True,created_by=None,
                    metadata_={'description':artifact.description,'tags':card.tags,'hub_id':BUILTIN_HUB_ID,
                        'card_id':name,'version':artifact.version,'system_key':name})
                db.add(installation)
                await db.flush()
                db.add(CapabilitySkillArtifact(capability_id=identifier,artifact_digest=artifact.digest))
                db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='capability.install',actor_user_id=None,
                    scope_id=root.id,metadata_={'capability_id':identifier,'capability_type':'skill',
                        'artifact_digest':artifact.digest,'bootstrap':WORKSPACE_ASSISTANT_KEY}))
                await db.flush()
            installations.append(installation)
        agent=Agent(id=str(uuid.uuid4()),slug='workspace-assistant',display_name='工作台助手',
            description='处理日常办公任务，按需澄清需求，并在授权和确认后创建 Agent 或安装 Skill。',
            system_prompt=DEFAULT_WORKSPACE_ASSISTANT_PROMPT,tool_policy={},system_key=WORKSPACE_ASSISTANT_KEY,
            platform_tool_profile='workspace',scope_id=root.id,visibility=Visibility.PUBLIC,
            include_descendants=True,created_by=None)
        db.add(agent)
        await db.flush()
        await grant(db,'agents',agent.id,root.id,None,include_descendants=True)
        for installation in installations:
            await grant(db,'skills',installation.id,root.id,None,include_descendants=True)
            db.add(AgentCapabilityBinding(agent_id=agent.id,capability_installation_id=installation.id))
        db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='agent.create',actor_user_id=None,
            scope_id=root.id,metadata_={'agent_id':agent.id,'bootstrap':WORKSPACE_ASSISTANT_KEY}))
        await db.flush()
        return agent


async def ensure_builtin_skill_library(db, *, catalog=None):
    """Register shipped immutable packages, preserving existing administrator choices."""
    root=await get_root_scope(db)
    if root is None:
        raise RuntimeError('root organization scope is missing')
    await db.execute(select(Scope.id).where(Scope.id==root.id).with_for_update())
    catalog=catalog if catalog is not None else BuiltinCatalog()
    principal=f'workmate-builtin:{root.id}'
    cursor=None
    seen=set()
    available=set()
    default_use=set(get_settings().builtin_skill_organization_use_ids)
    while True:
        page=(await catalog.list_skills(principal,None,cursor,100)).model_dump(mode="json")
        for card in page['cards']:
            available.add(card['id'])
            existing=await db.scalar(select(CapabilityInstallation).where(
                CapabilityInstallation.scope_id==root.id,CapabilityInstallation.capability_type=='skill',
                CapabilityInstallation.metadata_['hub_id'].as_string()==BUILTIN_HUB_ID,
                CapabilityInstallation.metadata_['card_id'].as_string()==card['id']))
            if existing is not None:
                continue
            artifact=await record_skill_artifact(db,await catalog.archive(principal,card['id'],card.get('version')))
            if artifact.name!=card['name']:
                raise SkillArtifactError('Builtin package differs from catalog identity')
            identifier=str(uuid.uuid4())
            db.add(CapabilityInstallation(id=identifier,capability_type='skill',scope_id=root.id,
                name=card['name'],
                display_name=card.get('display_name') or card['name'],enabled=True,created_by=None,
                metadata_={'hub_id':BUILTIN_HUB_ID,'card_id':card['id'],'system_key':card['id'],
                    'version':artifact.version,'description':artifact.description,'tags':card.get('tags',[])}))
            await db.flush()
            db.add(CapabilitySkillArtifact(capability_id=identifier,artifact_digest=artifact.digest))
            if card['id'] in default_use:
                await grant(db,'skills',identifier,root.id,None,include_descendants=True)
            db.add(SecurityAuditEvent(id=str(uuid.uuid4()),event_type='capability.register_builtin',
                scope_id=root.id,metadata_={'capability_id':identifier,'card_id':card['id']}))
        cursor=page.get('next_cursor')
        if not cursor:
            break
        if cursor in seen:
            raise RuntimeError('Builtin catalog returned a repeated cursor')
        seen.add(cursor)
    unknown=default_use-available
    if unknown:
        raise ValueError(f'Unknown builtin Skill IDs: {sorted(unknown)}')
    await db.flush()
