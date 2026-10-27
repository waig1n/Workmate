"""Controlled blob transport; production pinning/materialization still execute."""
import hashlib
from io import BytesIO
from pathlib import Path
import re
import zipfile


class FixtureStore:
    def __init__(self,root=Path('/shared/skill-blobs')):
        self.root=root;root.mkdir(parents=True,exist_ok=True)

    def path(self,digest):
        if not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Invalid digest')
        return self.root/digest

    async def put_verified(self,digest,raw):
        assert hashlib.sha256(raw).hexdigest()==digest
        self.path(digest).write_bytes(raw)
        return 'sha256/'+digest[:2]+'/'+digest+'.zip'

    async def get_verified(self,digest,size_bytes):
        raw=self.path(digest).read_bytes()
        assert len(raw)==size_bytes and hashlib.sha256(raw).hexdigest()==digest
        return raw


def archive():
    output=BytesIO()
    with zipfile.ZipFile(output,'w') as bundle:
        bundle.writestr('SKILL.md','---\nname: fixture-file\ndescription: Deterministic Fixture file fixture\n---\nExecute scripts/run.py with a hexadecimal key and byte count.')
        bundle.writestr('scripts/run.py',
            "import sys,re\nfrom pathlib import Path\nkey=sys.argv[1];size=int(sys.argv[2])\n"
            "assert re.fullmatch('[0-9a-f]{32}',key) and 0<size<=1048576\n"
            "pattern=key+'\\n'\nPath('/workspace/output/'+key+'.txt').write_text((pattern*((size//len(pattern))+1))[:size])\n")
    return output.getvalue()


async def seed_skill(sessions):
    from sqlalchemy import select
    from workmate_product.application.skill_repository import record_skill_artifact
    from workmate_product.persistence.models import Scope,ScopeMembership,CapabilityInstallation,CapabilitySkillArtifact,AgentCapabilityBinding,ResourceShare
    async with sessions.begin() as db:
        db.info['skill_blob_store']=FixtureStore()
        root=await db.scalar(select(Scope).where(Scope.kind=='organization',Scope.parent_id.is_(None)))
        assert root is not None
        if await db.get(ScopeMembership,{'scope_id':root.id,'user_id':'fixture-user'}) is None:
            db.add(ScopeMembership(scope_id=root.id,user_id='fixture-user'))
        artifact=await record_skill_artifact(db,archive())
        db.add(CapabilityInstallation(id='fixture-skill',capability_type='skill',scope_id=root.id,name='fixture-file',created_by='fixture-user'))
        await db.flush()
        db.add_all([CapabilitySkillArtifact(capability_id='fixture-skill',artifact_digest=artifact.digest),
            ResourceShare(id='fixture-skill-share',capability_id='fixture-skill',target_scope_id=root.id,include_descendants=True),
            AgentCapabilityBinding(agent_id='fixture-agent',capability_installation_id='fixture-skill')])
