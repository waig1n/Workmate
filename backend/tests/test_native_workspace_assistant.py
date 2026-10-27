"""Native bootstrap owns initial Skill artifacts, preserving later admin edits."""
import asyncio
import json
import pytest
from sqlalchemy import select
from workmate_product.persistence.models import Agent, CapabilityInstallation
from workmate_product.persistence.models import CapabilitySkillArtifact
from app.prompts import DEFAULT_WORKSPACE_ASSISTANT_PROMPT
from app.services.workspace_assistant import ensure_workspace_assistant, INITIAL_SKILL_IDS
from workmate_product.application.skill_repository import load_skill_artifact
from workmate_product.skills.catalog import BuiltinCatalog
from test_durable_projection import projection_database  # noqa: F401


async def create(sessions,**kwargs):
    async with sessions.begin() as db:
        agent=await ensure_workspace_assistant(db,**kwargs)
        return agent.id


async def test_concurrent_native_bootstrap_creates_one_assistant_with_requested_ready_skills(projection_database):
    sessions,admin=projection_database
    first,second=await asyncio.gather(create(sessions),create(sessions))
    assert first==second
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE system_key='workspace_assistant'")==1
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==len(INITIAL_SKILL_IDS)
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==len(INITIAL_SKILL_IDS)
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==len(INITIAL_SKILL_IDS)
    audits=await admin.fetch('SELECT * FROM security_audit_events')
    assert sorted(row['event_type'] for row in audits)==sorted(['agent.create']+['capability.install']*len(INITIAL_SKILL_IDS)+['resource.share']*(len(INITIAL_SKILL_IDS)+1))
    audits=[row for row in audits if row['event_type']!='resource.share']
    assert all(row['actor_user_id'] is None and json.loads(row['metadata'])['bootstrap']=='workspace_assistant' for row in audits)
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',first)==len(INITIAL_SKILL_IDS)
    async with sessions() as db:
        agent=await db.get(Agent,first)
        assert agent.system_prompt==DEFAULT_WORKSPACE_ASSISTANT_PROMPT
        assert agent.platform_tool_profile=='workspace' and agent.visibility=='public' and agent.include_descendants
        assert agent.created_by is None
        rows=(await db.execute(select(CapabilityInstallation,CapabilitySkillArtifact).join(
            CapabilitySkillArtifact,CapabilitySkillArtifact.capability_id==CapabilityInstallation.id))).all()
        assert {installation.name for installation,_ in rows}==set(INITIAL_SKILL_IDS)
        for installation,pointer in rows:
            artifact=await load_skill_artifact(db,pointer.artifact_digest)
            assert artifact.name==installation.name
            assert installation.metadata_['hub_id']=='workmate-builtin'
            assert 'SKILL.md' in artifact.files


async def test_existing_assistant_preserves_admin_prompt_bindings_and_disabled_capabilities(projection_database):
    sessions,admin=projection_database
    identifier=await create(sessions)
    removed=await admin.fetchval('SELECT capability_installation_id FROM agent_capability_bindings WHERE agent_id=$1 LIMIT 1',identifier)
    await admin.execute('DELETE FROM agent_capability_bindings WHERE agent_id=$1 AND capability_installation_id=$2',identifier,removed)
    await admin.execute("UPDATE agents SET system_prompt='administrator prompt',display_name='Custom assistant' WHERE id=$1",identifier)
    await admin.execute('UPDATE capability_installations SET enabled=false WHERE id=$1',removed)
    class NeverReadCatalog:
        def __getattr__(self,name): raise AssertionError('existing assistant must not re-bootstrap Skill content')
    assert await create(sessions,catalog=NeverReadCatalog())==identifier
    assert await admin.fetchval('SELECT system_prompt FROM agents WHERE id=$1',identifier)=='administrator prompt'
    assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',identifier)==len(INITIAL_SKILL_IDS)-1
    assert await admin.fetchval('SELECT enabled FROM capability_installations WHERE id=$1',removed) is False
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==len(INITIAL_SKILL_IDS)


async def test_bootstrap_failure_rolls_back_partial_skills_even_if_caller_commits(projection_database):
    sessions,admin=projection_database
    class BrokenCatalog(BuiltinCatalog):
        async def archive(self,user_id,card_id,version=None):
            if card_id=='skill-creator': raise RuntimeError('shipped Skill unavailable')
            return await super().archive(user_id,card_id,version)
    async with sessions.begin() as db:
        with pytest.raises(RuntimeError,match='shipped Skill unavailable'):
            await ensure_workspace_assistant(db,catalog=BrokenCatalog())
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE system_key='workspace_assistant'")==0
    for table in ('capability_installations','capability_skill_artifacts','skill_artifacts','agent_capability_bindings','security_audit_events'):
        assert await admin.fetchval(f'SELECT count(*) FROM {table}')==0
    await create(sessions)


async def test_existing_unready_named_skill_is_not_silently_replaced(projection_database):
    sessions,admin=projection_database
    async with sessions.begin() as db:
        db.add(CapabilityInstallation(id='existing',capability_type='skill',scope_id='scope',
            name='agent-bootstrap',
            display_name='Administrator Skill',metadata_={'description':'custom content'},enabled=True))
    await create(sessions)
    assert await admin.fetchval("SELECT count(*) FROM agents WHERE system_key='workspace_assistant'")==1
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==len(INITIAL_SKILL_IDS)
    assert await admin.fetchval("SELECT count(*) FROM agent_capability_bindings WHERE capability_installation_id='existing'")==0
    assert await admin.fetchval("SELECT name FROM capability_installations WHERE id='existing'")=='agent-bootstrap'


async def test_native_bootstrap_artifacts_are_database_immutable(projection_database):
    import asyncpg
    sessions,admin=projection_database
    await create(sessions)
    with pytest.raises(asyncpg.PostgresError,match='immutable'):
        await admin.execute("UPDATE skill_artifacts SET description='overwritten'")
    with pytest.raises(asyncpg.PostgresError,match='immutable'):
        await admin.execute('DELETE FROM skill_artifacts')


@pytest.mark.parametrize('change',[None,'source','disabled'])
async def test_preexisting_ready_builtin_requires_original_source_and_enabled_state(projection_database,change):
    sessions,admin=projection_database
    previous=await create(sessions)
    await admin.execute('DELETE FROM agent_capability_bindings WHERE agent_id=$1',previous)
    await admin.execute('DELETE FROM agents WHERE id=$1',previous)
    identifier=await admin.fetchval("SELECT id FROM capability_installations WHERE name='agent-bootstrap'")
    if change=='source':
        await admin.execute("UPDATE capability_installations SET metadata=jsonb_set(metadata::jsonb,'{hub_id}','\"other-hub\"'::jsonb) WHERE id=$1",identifier)
    elif change=='disabled':
        await admin.execute('UPDATE capability_installations SET enabled=false WHERE id=$1',identifier)
    if change=='disabled':
        with pytest.raises(ValueError,match='source|disabled'): await create(sessions)
        assert await admin.fetchval("SELECT count(*) FROM agents WHERE system_key='workspace_assistant'")==0
    else:
        recreated=await create(sessions)
        assert recreated!=previous
        assert await admin.fetchval('SELECT count(*) FROM agent_capability_bindings WHERE agent_id=$1',recreated)==len(INITIAL_SKILL_IDS)
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==len(INITIAL_SKILL_IDS)+(1 if change=='source' else 0)
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==len(INITIAL_SKILL_IDS)


async def test_bootstrapped_skills_are_pinned_by_real_execution_admission(projection_database):
    from workmate_product.application.execution_context import record_execution_spec,read_execution_context,SelectedModel
    from workmate_product.persistence.models import ScopeClosure,ScopeMembership,TaskMessage
    sessions,admin=projection_database
    assistant=await create(sessions)
    async with sessions.begin() as db:
        db.add(ScopeClosure(ancestor_id='scope',descendant_id='scope',depth=0))
        db.add(ScopeMembership(scope_id='scope',user_id='u'))
        db.add(TaskMessage(id='message',task_id='t',conversation_id='c',role='user',content={'text':'help with my work'}))
    await admin.execute("UPDATE conversations SET agent_id=$1,model_profile='chat'",assistant)
    await admin.execute("""UPDATE runtime_sessions SET status='accepted',execution_epoch=0,active_task_id=NULL,reply_id=NULL;
        UPDATE runtime_task_executions SET status='accepted',execution_epoch=0,reply_id=NULL;
        INSERT INTO runtime_commands(id,session_id,conversation_id,task_id,actor_user_id,kind,payload,content_digest)
        VALUES ('start','s','c','t','u','StartTask','{}',repeat('a',64));""")
    async with sessions.begin() as db:
        spec=await record_execution_spec(db,task_id='t',session_id='s',actor_user_id='u',
            selected_model=SelectedModel(profile_name='chat',model_type='openai',model='fixture',api_key_env='CHAT_MODEL_API_KEY'))
    expected=set(await admin.fetchval('SELECT array_agg(artifact_digest) FROM capability_skill_artifacts'))
    assert {ref.digest for ref in spec.skill_artifacts}==expected
    assert spec.platform_tool_profile=='workspace'
    await admin.execute("""UPDATE runtime_sessions SET status='preparing',execution_epoch=1,active_task_id='t',
        owner_id='worker',lease_until=clock_timestamp()+interval '60 seconds';
        UPDATE runtime_task_executions SET status='preparing',execution_epoch=1;
        UPDATE runtime_commands SET status='claimed',claimed_by='worker',claimed_epoch=1;""")
    async with sessions.begin() as db:
        context=await read_execution_context(db,session_id='s',task_id='t',worker_id='worker',execution_epoch=1,
            storage_node_id='node',knowledge_enabled=False)
        assert context.agent_id==assistant
        assert {ref.digest for ref in context.initial_spec.skill_artifacts}==expected


async def test_native_library_registers_all_builtins_with_grants_and_preserves_choices(projection_database):
    from app.services.workspace_assistant import ensure_builtin_skill_library
    sessions,admin=projection_database
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
    count=await admin.fetchval('SELECT count(*) FROM capability_installations')
    assert count>2
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==count
    assert await admin.fetchval('SELECT count(*) FROM resource_shares WHERE include_descendants')==count
    await admin.execute("DELETE FROM resource_shares WHERE capability_id IN (SELECT id FROM capability_installations WHERE name='deep-research')")
    await admin.execute("UPDATE capability_installations SET enabled=false WHERE name='deep-research'")
    async with sessions.begin() as db:
        await ensure_builtin_skill_library(db)
    assert await admin.fetchval('SELECT count(*) FROM capability_installations')==count
    assert await admin.fetchval("SELECT enabled FROM capability_installations WHERE name='deep-research'") is False

    assert await admin.fetchval('SELECT count(*) FROM resource_shares')==count-1

async def test_default_assistant_skill_set_matches_product_choice():
    assert set(INITIAL_SKILL_IDS)=={'agent-bootstrap','chart-visualization','consulting-analysis',
        'data-analysis','deep-research','frontend-design','ppt-generation','skill-creator',
        'skill-reviewer'}
