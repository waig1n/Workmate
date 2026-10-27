"""Explicit upgrade adds defaults without silently rewriting administrator state."""
import pytest
from sqlalchemy import select
from app.config_file import DEFAULT_ORGANIZATION_SKILL_IDS
from workmate_product.skills.catalog import builtin_root
from app.services.builtin_defaults import apply_builtin_defaults
from app.services.workspace_assistant import ensure_builtin_skill_library,ensure_workspace_assistant,INITIAL_SKILL_IDS
from workmate_product.persistence.models import AgentCapabilityBinding,CapabilityInstallation
from test_durable_projection import projection_database  # noqa: F401


async def setup(sessions):
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
        return (await ensure_workspace_assistant(db)).id


async def test_explicit_upgrade_preview_apply_and_idempotence(projection_database):
    sessions,admin=projection_database
    agent=await setup(sessions)
    await admin.execute("DELETE FROM resource_shares WHERE capability_id IS NOT NULL")
    await admin.execute("DELETE FROM agent_capability_bindings WHERE agent_id=$1",agent)
    await admin.execute("UPDATE agents SET system_prompt='custom prompt' WHERE id=$1",agent)
    async with sessions.begin() as db:
        extra=await db.scalar(select(CapabilityInstallation).where(CapabilityInstallation.name=='academic-paper-review'))
        db.add(AgentCapabilityBinding(agent_id=agent,capability_installation_id=extra.id))
    async with sessions.begin() as db:
        report=await apply_builtin_defaults(db)
    assert set(report['bind_skills'])==set(INITIAL_SKILL_IDS)
    assert set(report['grant_skills']) == set(DEFAULT_ORGANIZATION_SKILL_IDS)
    assert not report['blockers'] and not report['applied']
    assert await admin.fetchval('SELECT count(*) FROM resource_shares WHERE capability_id IS NOT NULL')==0
    async with sessions.begin() as db:
        result=await apply_builtin_defaults(db,apply=True)
    assert result['applied']
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',agent) == len(INITIAL_SKILL_IDS) + 1
    assert await admin.fetchval('SELECT system_prompt FROM agents WHERE id=$1',agent)=='custom prompt'
    async with sessions.begin() as db:
        repeat=await apply_builtin_defaults(db,apply=True)
    assert repeat['applied'] and repeat['grant_skills']==repeat['bind_skills']==[]
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='builtin.defaults_applied'")==1


@pytest.mark.parametrize('problem',['disabled','unready','foreign-source'])
async def test_invalid_required_skill_blocks_entire_apply(projection_database,problem):
    sessions,admin=projection_database
    agent=await setup(sessions)
    await admin.execute('DELETE FROM resource_shares WHERE capability_id IS NOT NULL')
    identifier=await admin.fetchval("SELECT id FROM capability_installations WHERE name='data-analysis'")
    if problem=='disabled':
        await admin.execute('UPDATE capability_installations SET enabled=false WHERE id=$1',identifier)
    elif problem=='unready':
        await admin.execute('DELETE FROM capability_skill_artifacts WHERE capability_id=$1',identifier)
    else:
        await admin.execute("UPDATE capability_installations SET metadata=jsonb_set(metadata::jsonb,'{hub_id}','\"uploaded\"'::jsonb) WHERE id=$1",identifier)
    async with sessions.begin() as db:
        report=await apply_builtin_defaults(db,apply=True)
    assert report['blockers'] and not report['applied']
    assert await admin.fetchval('SELECT count(*) FROM resource_shares WHERE capability_id IS NOT NULL')==0
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',agent) == len(INITIAL_SKILL_IDS)


async def test_startup_does_not_restore_revoked_skill_even_when_recreating_assistant(projection_database):
    from workmate_product.application.skill_artifacts import SkillArtifactError
    sessions,admin=projection_database
    agent=await setup(sessions)
    await admin.execute('DELETE FROM agent_capability_bindings WHERE agent_id=$1',agent)
    await admin.execute('DELETE FROM agents WHERE id=$1',agent)
    await admin.execute("DELETE FROM resource_shares WHERE capability_id IN (SELECT id FROM capability_installations WHERE name='agent-bootstrap')")
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
        with pytest.raises(SkillArtifactError,match='lacks organization use grant'):
            await ensure_workspace_assistant(db)
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE system_key='workspace_assistant'")==0


async def test_command_requires_an_explicit_mode():
    from scripts.apply_builtin_defaults import parser
    with pytest.raises(SystemExit): parser().parse_args([])
    with pytest.raises(SystemExit): parser().parse_args(['--check','--apply'])
    assert parser().parse_args(['--check']).apply is False
    assert parser().parse_args(['--apply']).apply is True

async def test_default_grants_cover_later_department_members_without_management_roles(projection_database):
    from workmate_product.application.authorization import AccessDecisionService
    from workmate_product.persistence.models import Scope,ScopeClosure,ScopeMembership
    sessions,admin=projection_database
    await setup(sessions)
    async with sessions.begin() as db:
        db.add(Scope(id='later',kind='department',name='Later',normalized_name='later',parent_id='scope'))
        await db.flush()
        db.add_all([ScopeClosure(ancestor_id='scope',descendant_id='scope',depth=0),
            ScopeClosure(ancestor_id='scope',descendant_id='later',depth=1),
            ScopeClosure(ancestor_id='later',descendant_id='later',depth=0),
            ScopeMembership(scope_id='later',user_id='u')])
    async with sessions() as db:
        allowed=await AccessDecisionService(db).accessible_capability_ids('u')
        assert len(allowed) == len(DEFAULT_ORGANIZATION_SKILL_IDS)
    assert await admin.fetchval('SELECT count(*) FROM role_bindings')==0


async def test_existing_library_without_assistant_can_be_upgraded_before_startup(projection_database):
    sessions,admin=projection_database
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
    await admin.execute('DELETE FROM resource_shares WHERE capability_id IS NOT NULL')
    async with sessions.begin() as db:
        report=await apply_builtin_defaults(db,apply=True)
    assert report['assistant_id'] is None and report['warnings'] and report['applied']
    assert await admin.fetchval('SELECT count(*) FROM resource_shares') == len(DEFAULT_ORGANIZATION_SKILL_IDS)
    async with sessions.begin() as db:
        agent=await ensure_workspace_assistant(db)
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',agent.id) == len(INITIAL_SKILL_IDS)


async def test_optional_disabled_skill_stays_disabled_and_unshared(projection_database):
    sessions,admin=projection_database
    await setup(sessions)
    await admin.execute("UPDATE capability_installations SET enabled=false WHERE name='academic-paper-review'")
    await admin.execute('DELETE FROM resource_shares WHERE capability_id IS NOT NULL')
    async with sessions.begin() as db:
        report=await apply_builtin_defaults(db,apply=True)
    assert report['applied'] and report['skipped_disabled']==['academic-paper-review']
    assert await admin.fetchval('SELECT count(*) FROM resource_shares WHERE capability_id IS NOT NULL') == len(DEFAULT_ORGANIZATION_SKILL_IDS) - 1

async def test_platform_policy_limits_initial_sharing_without_changing_skill_files(projection_database,monkeypatch):
    from types import SimpleNamespace
    from app.services import workspace_assistant
    sessions,admin=projection_database
    monkeypatch.setattr(workspace_assistant,'get_settings',lambda:SimpleNamespace(builtin_skill_organization_use_ids=['data-analysis']))
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
    installed = await admin.fetch('SELECT name FROM capability_installations')
    assert {row['name'] for row in installed} == {p.parent.name for p in builtin_root().glob('*/SKILL.md')}
    shared=await admin.fetch('SELECT c.name FROM resource_shares s JOIN capability_installations c ON c.id=s.capability_id')
    assert [row['name'] for row in shared]==['data-analysis']


async def test_unknown_configured_skill_fails_initialization(projection_database,monkeypatch):
    from types import SimpleNamespace
    from app.services import workspace_assistant
    sessions,admin=projection_database
    monkeypatch.setattr(workspace_assistant,'get_settings',lambda:SimpleNamespace(builtin_skill_organization_use_ids=['not-shipped']))
    with pytest.raises(ValueError,match='Unknown builtin Skill IDs'):
        async with sessions.begin() as db:
            await ensure_builtin_skill_library(db)
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==0

async def test_direct_assistant_creation_cannot_bypass_platform_policy(projection_database,monkeypatch):
    from types import SimpleNamespace
    from workmate_product.application.skill_artifacts import SkillArtifactError
    from app.services import workspace_assistant
    sessions,admin=projection_database
    monkeypatch.setattr(workspace_assistant,'get_settings',lambda:SimpleNamespace(builtin_skill_organization_use_ids=[]))
    async with sessions.begin() as db:
        with pytest.raises(SkillArtifactError,match='not approved for organization use'):
            await ensure_workspace_assistant(db)
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==0
