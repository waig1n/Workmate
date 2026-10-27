"""Real PostgreSQL Skill storage/governance on the initial schema."""
import asyncio

import pytest
import pytest_asyncio

from workmate_product.application.skill_repository import record_skill_artifact,bind_skill_artifact,load_skill_artifact,ready_skill_artifacts
from workmate_product.application.skill_artifacts import SkillArtifactError
from workmate_product.persistence.models import (
    SkillArtifactRecord,CapabilitySkillArtifact,Role,Permission,RolePermission,RoleBinding,
    CapabilityInstallation,AgentCapabilityBinding,SkillUploadSource,ResourceShare,
)
from test_execution_context import database,bind_capability
from test_skill_artifacts import archive


@pytest_asyncio.fixture
async def skill_database(database):
    sessions,admin=database
    await bind_capability(sessions,'sample',ready=False,grant=False)
    async with sessions.begin() as db:
        db.add(ResourceShare(id='sample-explicit-grant',capability_id='sample',target_scope_id='org'))
        db.add_all([Role(id='r',key='skill-manager',display_name='Manager'),
                    Permission(id='p',key='skill.manage')])
        await db.flush()
        db.add_all([RolePermission(id='rp',role_id='r',permission_id='p'),
                    RoleBinding(user_id='u',scope_id='org',role_id='r')])
    yield sessions,admin


async def ingest(sessions,raw=None):
    async with sessions.begin() as db:
        artifact=await record_skill_artifact(db,raw or archive())
        await bind_skill_artifact(db,capability_id='sample',digest=artifact.digest,actor_user_id='u')
        return artifact


@pytest.mark.asyncio
async def test_ingestion_binding_and_audit_share_caller_transaction(skill_database):
    sessions,admin=skill_database
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            artifact=await record_skill_artifact(db,archive())
            await bind_skill_artifact(db,capability_id='sample',digest=artifact.digest,actor_user_id='u')
            for table in ('skill_artifacts','capability_skill_artifacts','security_audit_events'):
                assert await admin.fetchval(f'SELECT count(*) FROM {table}')==0
            raise RuntimeError('rollback')
    for table in ('skill_artifacts','capability_skill_artifacts','security_audit_events'):
        assert await admin.fetchval(f'SELECT count(*) FROM {table}')==0
    artifact=await ingest(sessions)
    async with sessions.begin() as db:
        loaded=await load_skill_artifact(db,artifact.digest)
    assert loaded.archive==artifact.archive


@pytest.mark.asyncio
async def test_concurrent_ingestion_deduplicates_immutable_content_and_audit(skill_database):
    sessions,admin=skill_database
    artifacts=await asyncio.gather(*(ingest(sessions) for _ in range(4)))
    assert len({a.digest for a in artifacts})==1
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==1
    assert await admin.fetchval('SELECT count(*) FROM security_audit_events')==1
    import asyncpg
    for operation in ('DELETE FROM skill_artifacts',"UPDATE skill_artifacts SET name='changed'"):
        with pytest.raises(asyncpg.RaiseError,match='immutable'):
            await admin.execute(operation)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[
    'DELETE FROM role_bindings',"UPDATE users SET status='disabled'",
    'UPDATE capability_installations SET enabled=false',
    "UPDATE capability_installations SET capability_type='mcp'",
])
async def test_ready_binding_requires_current_manage_authority(skill_database,mutation):
    sessions,admin=skill_database
    async with sessions.begin() as db:
        artifact=await record_skill_artifact(db,archive())
    await admin.execute(mutation)
    with pytest.raises(SkillArtifactError):
        async with sessions.begin() as db:
            await bind_skill_artifact(db,capability_id='sample',digest=artifact.digest,actor_user_id='u')
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==0


@pytest.mark.asyncio
async def test_missing_artifact_binding_is_not_ready(skill_database):
    sessions,admin=skill_database
    async with sessions.begin() as db:
        cap=await db.get(CapabilityInstallation,'sample')
        with pytest.raises(SkillArtifactError,match='ready'):
            await ready_skill_artifacts(db,[cap])


@pytest.mark.asyncio
async def test_binding_new_version_keeps_original_archive_available(skill_database):
    sessions,admin=skill_database
    first=await ingest(sessions)
    second=await ingest(sessions,archive({'SKILL.md':'---\nname: sample\ndescription: New version\nversion: 2\n---\nNew body.'}))
    assert first.digest!=second.digest
    async with sessions.begin() as db:
        assert (await load_skill_artifact(db,first.digest)).description=='Example workflow'
        assert (await ready_skill_artifacts(db,[await db.get(CapabilityInstallation,'sample')]))=={'sample':second.digest}
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==2


@pytest.mark.asyncio
async def test_execution_pins_initial_skill_revision_when_installation_is_updated(skill_database):
    from test_execution_context import record,claim,read
    sessions,admin=skill_database
    first=await ingest(sessions)
    spec=await record(sessions)
    assert spec.skill_artifacts[0].digest==first.digest
    second=await ingest(sessions,archive({'SKILL.md':'---\nname: sample\ndescription: Revised\n---\nNext.'}))
    await claim(admin)
    context=await read(sessions)
    assert context.initial_spec.skill_artifacts[0].digest==first.digest
    assert context.initial_spec.skill_artifacts[0].digest!=second.digest


@pytest.mark.asyncio
async def test_unready_bound_skill_cannot_be_admitted(skill_database):
    from test_execution_context import record
    sessions,admin=skill_database
    with pytest.raises(SkillArtifactError,match='ready'):
        await record(sessions)
    assert await admin.fetchval('SELECT count(*) FROM runtime_execution_specs')==0


@pytest.mark.asyncio
async def test_installation_alias_does_not_rewrite_immutable_skill_source(skill_database):
    sessions,admin=skill_database
    await admin.execute("UPDATE capability_installations SET name='custom-alias'")
    stored=await ingest(sessions)
    assert stored.name=='sample'
    assert await admin.fetchval('SELECT name FROM capability_installations')=='custom-alias'


@pytest.mark.asyncio
async def test_uninstall_removes_ready_pointer_but_retains_immutable_source(skill_database):
    sessions,admin=skill_database
    original=await ingest(sessions)
    await admin.execute("DELETE FROM agent_capability_bindings; DELETE FROM capability_installations")
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==0
    async with sessions.begin() as db:
        assert (await load_skill_artifact(db,original.digest)).archive==original.archive
