"""Atomic ready installations, shared by API and governed platform tools."""
import asyncio
import pytest
from sqlalchemy import select
from workmate_product.application.skill_installation import install_skill_artifact, uninstall_skill, SkillInstallationError
from workmate_product.persistence.models import CapabilityInstallation, CapabilitySkillArtifact
from test_execution_context import database
from test_skill_repository import skill_database
from test_skill_artifacts import archive

async def install(sessions, name='new-alias'):
    async with sessions.begin() as db:
        return await install_skill_artifact(db, actor_user_id='u', scope_id='org', raw=archive(),
            hub_id='external-test',card_id='sample',name=name)

@pytest.mark.asyncio
async def test_success_means_immutable_artifact_ready_and_registration_atomic(skill_database):
    sessions,admin=skill_database
    with pytest.raises(RuntimeError):
        async with sessions.begin() as db:
            cap=await install_skill_artifact(db,actor_user_id='u',scope_id='org',raw=archive(),hub_id='uploaded',card_id='x',name='alias')
            assert cap.name=='alias'
            assert await db.get(CapabilitySkillArtifact,cap.id)
            assert await admin.fetchval("SELECT count(*) FROM capability_installations WHERE name='alias'")==0
            raise RuntimeError('rollback')
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==0
    cap=await install(sessions)
    assert await admin.fetchval('SELECT artifact_digest FROM capability_skill_artifacts WHERE capability_id=$1',cap.id)
    assert await admin.fetchval("SELECT count(*) FROM security_audit_events WHERE event_type='capability.install'")==1

@pytest.mark.asyncio
async def test_concurrent_same_scope_name_installs_once(skill_database):
    sessions,admin=skill_database
    outcomes=await asyncio.gather(install(sessions),install(sessions),return_exceptions=True)
    assert sum(isinstance(o,CapabilityInstallation) for o in outcomes)==1
    errors=[o for o in outcomes if isinstance(o,SkillInstallationError)]
    assert len(errors)==1 and errors[0].status_code==409
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==1

@pytest.mark.asyncio
async def test_revoked_manager_cannot_install_and_invalid_archive_leaves_no_installation(skill_database):
    sessions,admin=skill_database
    await admin.execute('DELETE FROM role_bindings')
    with pytest.raises(SkillInstallationError) as failure:
        await install(sessions)
    assert failure.value.status_code in (403,404)
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==0

@pytest.mark.asyncio
async def test_uninstall_keeps_archive_and_bound_capability_is_rejected(skill_database):
    sessions,admin=skill_database
    with pytest.raises(SkillInstallationError) as bound:
        async with sessions.begin() as db:
            await uninstall_skill(db,actor_user_id='u',installation_id='sample')
    assert bound.value.status_code==409
    cap=await install(sessions)
    async with sessions.begin() as db:
        await uninstall_skill(db,actor_user_id='u',installation_id=cap.id)
    assert await admin.fetchval('SELECT count(*) FROM skill_artifacts')==1
    assert await admin.fetchval('SELECT count(*) FROM capability_skill_artifacts')==0

@pytest.mark.asyncio
async def test_uploaded_catalog_survives_uninstall_and_rechecks_scope_authority(skill_database):
    from workmate_product.application.skill_uploads import upload_skill_archive, list_uploaded_skills, load_uploaded_skill
    sessions,admin=skill_database
    async with sessions.begin() as db:
        cap=await upload_skill_archive(db,actor_user_id='u',scope_id='org',raw=archive({'SKILL.md':'---\nname: uploaded-skill\ndescription: Sample\ntags: [workflow]\n---\nWork.'}),filename='example.zip',display_name='Uploaded example')
    async with sessions.begin() as db:
        await uninstall_skill(db,actor_user_id='u',installation_id=cap.id)
    async with sessions.begin() as db:
        page=await list_uploaded_skills(db,actor_user_id='u')
        assert len(page.cards)==1 and page.cards[0].display_name=='Uploaded example'
        card,artifact=await load_uploaded_skill(db,actor_user_id='u',card_id=page.cards[0].id)
        assert artifact.name=='uploaded-skill'
        again=await install_skill_artifact(db,actor_user_id='u',scope_id='org',raw=artifact.archive,hub_id='uploaded',card_id=card.id)
        assert again.metadata_['tags']==['workflow']
    await admin.execute('DELETE FROM role_bindings')
    async with sessions.begin() as db:
        assert not (await list_uploaded_skills(db,actor_user_id='u')).cards
        with pytest.raises(SkillInstallationError):
            await load_uploaded_skill(db,actor_user_id='u',card_id=card.id)


@pytest.mark.asyncio
async def test_product_alias_preserves_unicode_and_spaces(skill_database):
    sessions,_=skill_database
    installed=await install(sessions,name='办公助手 Skill')
    assert installed.name=='办公助手 Skill'


@pytest.mark.asyncio
async def test_historical_department_upload_reinstalls_under_current_organization_authority(skill_database):
    from workmate_product.application.skill_uploads import upload_skill_archive, list_uploaded_skills, load_uploaded_skill
    from workmate_product.persistence.models import Scope, ScopeClosure, SkillUploadSource
    sessions, admin = skill_database
    async with sessions.begin() as db:
        cap = await upload_skill_archive(db, actor_user_id='u', scope_id='org',
            raw=archive({'SKILL.md': '---\nname: historical\ndescription: Historical upload\n---\nWork.'}),
            filename='historical.zip')
        source = await db.scalar(select(SkillUploadSource))
        digest = source.card_id
        db.add(Scope(id='old-department', kind='department', parent_id='org', name='Old department'))
        await db.flush()
        db.add(ScopeClosure(ancestor_id='org', descendant_id='old-department', depth=1))
        # Organization installations retain native provider source IDs.
        source.scope_id = 'old-department'
        source.card = {**source.card, 'id': 'old-department:' + digest}
        cap.metadata_ = {**cap.metadata_, 'card_id': source.card['id']}
        await db.merge(cap)
    async with sessions.begin() as db:
        await uninstall_skill(db, actor_user_id='u', installation_id=cap.id)
    async with sessions.begin() as db:
        page = await list_uploaded_skills(db, actor_user_id='u')
        assert page.cards[0].id == 'old-department:' + digest
        card, artifact = await load_uploaded_skill(db, actor_user_id='u', card_id=page.cards[0].id)
        reinstalled = await install_skill_artifact(db, actor_user_id='u', scope_id='org',
            raw=artifact.archive, hub_id='uploaded', card_id=card.id)
        assert reinstalled.scope_id == 'org'
        assert reinstalled.metadata_['card_id'] == card.id
    await admin.execute("DELETE FROM scope_closure WHERE descendant_id='old-department'")
    async with sessions.begin() as db:
        assert not (await list_uploaded_skills(db, actor_user_id='u')).cards
        with pytest.raises(SkillInstallationError) as denied:
            await load_uploaded_skill(db, actor_user_id='u', card_id=card.id)
        assert denied.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['upload', 'assistant'])
async def test_upload_creation_channel_is_persisted(skill_database, channel):
    from workmate_product.application.skill_uploads import upload_skill_archive
    sessions, _ = skill_database
    raw = archive({'SKILL.md': (
        f'---\nname: channel-{channel}\ndescription: Channel test\n---\nWork.'
    )})
    async with sessions.begin() as db:
        cap = await upload_skill_archive(db, actor_user_id='u', scope_id='org', raw=raw,
                                        filename='sample.zip', creation_channel=channel)
    async with sessions() as db:
        stored = await db.get(CapabilityInstallation, cap.id)
        assert stored.metadata_['creation_channel'] == channel
