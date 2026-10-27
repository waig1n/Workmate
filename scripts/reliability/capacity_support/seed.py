"""Dedicated CAPACITY identities/capabilities; Skill bytes use the production S3 store."""
import asyncio
import os
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from common import settings,write


async def seed_skill(sessions,store):
    from skill_fixture import archive
    from workmate_product.application.skill_repository import record_skill_artifact
    from workmate_product.persistence.models import (Scope,ScopeMembership,CapabilityInstallation,
        CapabilitySkillArtifact,AgentCapabilityBinding,ResourceShare)
    async with sessions.begin() as db:
        db.info['skill_blob_store']=store
        root=await db.scalar(select(Scope).where(Scope.kind=='organization',Scope.parent_id.is_(None)))
        assert root is not None,'Migrations must seed the organization first'
        if await db.get(ScopeMembership,{'scope_id':root.id,'user_id':'fixture-user'}) is None:
            db.add(ScopeMembership(scope_id=root.id,user_id='fixture-user'))
        artifact=await record_skill_artifact(db,archive())
        db.add(CapabilityInstallation(id='fixture-skill',capability_type='skill',scope_id=root.id,name='fixture-file',created_by='fixture-user'))
        await db.flush()
        db.add_all([CapabilitySkillArtifact(capability_id='fixture-skill',artifact_digest=artifact.digest),
            ResourceShare(id='fixture-skill-share',capability_id='fixture-skill',target_scope_id=root.id,include_descendants=True),
            AgentCapabilityBinding(agent_id='fixture-agent',capability_installation_id='fixture-skill')])
    return artifact.digest


async def main():
    role=settings()
    from workmate_product.persistence.models import User,Scope,ScopeClosure,ScopeMembership,Agent
    from workmate_product.skill_storage import store_from_settings
    store=store_from_settings(role.runtime)
    engine=create_async_engine(os.environ['DATABASE_URL'])
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    try:
        async with sessions.begin() as db:
            assert await db.get(User,'fixture-user') is None,'Refuse to reseed an existing fixture'
            db.add(User(id='fixture-user',email='fixture-load@example.test',password_hash='x',display_name='CAPACITY')); await db.flush()
            db.add(Scope(id='fixture-scope',kind='personal',owner_user_id='fixture-user',name='CAPACITY')); await db.flush()
            db.add_all([ScopeClosure(ancestor_id='fixture-scope',descendant_id='fixture-scope',depth=0),
                ScopeMembership(scope_id='fixture-scope',user_id='fixture-user'),
                Agent(id='fixture-agent',slug='fixture-load',display_name='CAPACITY mixed',scope_id='fixture-scope',created_by='fixture-user',visibility='private',system_prompt='Use controlled fixture tools.'),
                Agent(id='capacity-text',slug='capacity-text',display_name='CAPACITY text',scope_id='fixture-scope',created_by='fixture-user',visibility='private',system_prompt='Return controlled fixture text.')])
        digest=await seed_skill(sessions,store)
        from mcp_fixture import seed_mcp
        from rag_fixture import seed_rag
        await seed_mcp(sessions); await seed_rag(sessions)
        write('seed.json',{'user_id':'fixture-user','scope_id':'fixture-scope','mixed_agent':'fixture-agent',
            'text_agent':'capacity-text','skill_digest':digest,'skill_store':type(store).__name__,
            'artifact_backend':role.runtime.artifact_storage.backend})
    finally: await engine.dispose()


if __name__=='__main__': asyncio.run(main())
