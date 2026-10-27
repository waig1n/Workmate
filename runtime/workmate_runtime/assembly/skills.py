"""Materialize pinned Product Skill sources using only the Task sandbox backend."""
import asyncio
import json
import posixpath
import re

from agentscope.skill import Skill
from workmate_product.application.skill_repository import load_skill_artifact
from workmate_product.application import skill_repository
from .skill_cache import SkillDirectoryCache
from workmate_runtime.execution.resources import TrustedTaskBackend, TaskResourceRegistry


class SkillMaterializationError(ValueError):
    """A ready Skill cannot be reconstructed exactly for this Task."""


# The argument vector and archive stdin travel through the Task-bound backend;
# no shell interpolation and no host extraction are used. Every directory and
# file is opened relative to an already pinned descriptor without following
# links. Start never overwrites an existing file; Resume never changes bytes.
_MATERIALIZE_SCRIPT = r'''
import hashlib,io,json,os,stat,sys,zipfile
mode,destination,digest=sys.argv[1:]
header=sys.stdin.buffer.read(4)
if len(header)!=4: raise RuntimeError('missing manifest frame')
length=int.from_bytes(header,'big')
if not 0<length<=33554432: raise RuntimeError('manifest frame too large')
encoded_manifest=sys.stdin.buffer.read(length)
if len(encoded_manifest)!=length: raise RuntimeError('incomplete manifest frame')
raw=sys.stdin.buffer.read(20971521)
if len(raw)>20971520 or hashlib.sha256(raw).hexdigest()!=digest:
    raise RuntimeError('archive digest mismatch')
manifest=json.loads(encoded_manifest)
opened=[]
def directory(parent,name,create):
    if create:
        try: os.mkdir(name,0o700,dir_fd=parent)
        except FileExistsError: pass
    fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
    opened.append(fd)
    return fd
try:
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    opened.append(fd)
    for component in destination.strip('/').split('/'):
        if component in {'','.','..'}: raise RuntimeError('unsafe destination')
        fd=directory(fd,component,mode=='install')
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        skill_files=[n for n in bundle.namelist() if n.rstrip('/').split('/')[-1]=='SKILL.md']
        if len(skill_files)!=1: raise RuntimeError('ambiguous archive')
        prefix=skill_files[0][:-len('SKILL.md')]
        for name,expected,size,executable in manifest:
            components=name.split('/')
            if any(c in {'','.','..'} for c in components): raise RuntimeError('unsafe member')
            checkpoint=len(opened)
            parent=fd
            for component in components[:-1]: parent=directory(parent,component,mode=='install')
            if mode=='install':
                data=bundle.read(prefix+name)
                if len(data)!=size or hashlib.sha256(data).hexdigest()!=expected: raise RuntimeError('member mismatch')
                output=os.open(components[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,
                               0o555 if executable else 0o444,dir_fd=parent)
                opened.append(output)
                view=memoryview(data)
                while view:
                    amount=os.write(output,view)
                    if amount<=0: raise RuntimeError('incomplete write')
                    view=view[amount:]
                os.fchmod(output,0o555 if executable else 0o444)
                os.fsync(output)
            elif mode=='verify':
                source=os.open(components[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent)
                opened.append(source)
                status=os.fstat(source)
                if (not stat.S_ISREG(status.st_mode) or status.st_nlink!=1 or status.st_size!=size
                        or stat.S_IMODE(status.st_mode)!=(0o555 if executable else 0o444)):
                    raise RuntimeError('invalid materialized file')
                digestor=hashlib.sha256()
                read=0
                while chunk:=os.read(source,65536):
                    read+=len(chunk)
                    if read>size: raise RuntimeError('materialized file grew')
                    digestor.update(chunk)
                if read!=size or digestor.hexdigest()!=expected: raise RuntimeError('materialized file changed')
            else: raise RuntimeError('unsupported mode')
            while len(opened)>checkpoint: os.close(opened.pop())
    expected_files={item[0] for item in manifest}
    expected_dirs={''}
    for name in expected_files:
        parts=name.split('/')
        expected_dirs.update('/'.join(parts[:i]) for i in range(1,len(parts)))
    def verify_tree(parent,prefix=''):
        for name in os.listdir(parent):
            relative=prefix+'/'+name if prefix else name
            status=os.stat(name,dir_fd=parent,follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode) and relative in expected_dirs:
                verify_tree(directory(parent,name,False),relative)
                os.close(opened.pop())
            elif not stat.S_ISREG(status.st_mode) or relative not in expected_files:
                raise RuntimeError('unexpected materialized entry')
    verify_tree(fd)
finally:
    for item in reversed(opened): os.close(item)
'''


def _component(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
        raise SkillMaterializationError('Skill path identity is invalid')
    return value


async def materialize_execution_skills(context,binding,sessions,*,resume,cache: SkillDirectoryCache | None = None):
    if (binding.owner.task_id!=context.task_id or binding.owner.session_id!=context.session_id
            or not isinstance(binding.backend,TrustedTaskBackend)
            or not isinstance(getattr(binding.backend,"_task_resources",None),TaskResourceRegistry)
            or binding.backend._execution_owner!=binding.owner):
        raise SkillMaterializationError('Skill sources require the matching Task backend')
    binding.backend._task_resources._scope(binding.owner,launching=True)
    if type(resume) is not bool:
        raise SkillMaterializationError('Skill materialization requires an explicit phase')
    refs=context.initial_spec.skill_artifacts
    capabilities={c.id:c for c in context.capabilities if c.capability_type=='skill'}
    if len({r.capability_id for r in refs})!=len(refs) or {r.capability_id for r in refs}!=set(capabilities):
        raise SkillMaterializationError('Skill definitions differ from the authorized pinned bindings')
    # Fetch and verify all artifacts before any sandbox writes. Runtime DB reads
    # do not hold locks or transactions across package installation processes.
    artifacts=[]
    if refs:
        async with sessions.begin() as db:
            for ref in refs:
                if cache is None:
                    artifact=await load_skill_artifact(db,ref.digest)
                else:
                    metadata=await skill_repository.load_skill_artifact_metadata(db,ref.digest)
                    if metadata.digest!=ref.digest:
                        raise SkillMaterializationError('Skill metadata differs from its pinned digest')
                    artifact=await asyncio.to_thread(cache.load,metadata)
                    if artifact is None:
                        artifact=await load_skill_artifact(db,ref.digest)
                        metadata.validate(artifact)
                        artifact=await asyncio.to_thread(cache.store,artifact)
                if artifact.digest!=ref.digest:
                    raise SkillMaterializationError('Skill package differs from its pinned digest')
                artifacts.append((ref,artifact))
    if len({a.name for _,a in artifacts})!=len(artifacts):
        raise SkillMaterializationError('Duplicate Skill names cannot be assembled')
    skills=[]
    for ref,artifact in artifacts:
        workdir=binding.workspace.workdir
        if not isinstance(workdir,str) or not workdir.startswith('/') or posixpath.normpath(workdir)!=workdir:
            raise SkillMaterializationError('Skill workspace must have a canonical absolute workdir')
        destination=posixpath.join(workdir,'skills',_component(context.task_id),
            _component(ref.capability_id),ref.digest)
        encoded=json.dumps(artifact.manifest,separators=(',',':'),ensure_ascii=False).encode()
        if len(encoded)>32*1024*1024:
            raise SkillMaterializationError('Skill manifest exceeds framing limit')
        framed=len(encoded).to_bytes(4,'big')+encoded+artifact.archive
        result=await binding.backend._exec_with_input(['python3','-I','-c',_MATERIALIZE_SCRIPT,
            'verify' if resume else 'install',destination,ref.digest],framed)
        if result.exit_code!=0:
            raise SkillMaterializationError('Skill installation or integrity verification failed')
        skills.append(Skill(name=artifact.name,description=artifact.description,dir=destination,
                            markdown=artifact.markdown,updated_at=0.0))
    return tuple(skills)
