"""Pinned Skill materialization script and real Toolkit integration."""
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest
from agentscope.tool import Toolkit
from workmate_product.application.execution_context import CapabilityReference,SkillArtifactReference
from workmate_product.application.skill_artifacts import parse_skill_archive
from workmate_runtime.assembly.skills import _MATERIALIZE_SCRIPT,materialize_execution_skills,SkillMaterializationError
from test_worker_agent_assembly import context,Sessions,OWNER
from test_worker_workspaces import manager,IDENTITY


def artifact():
    output=BytesIO()
    with zipfile.ZipFile(output,'w') as bundle:
        bundle.writestr('SKILL.md','---\nname: sample\ndescription: A test workflow\n---\nUse scripts/run.py')
        bundle.writestr('scripts/run.py','print("ready")')
    return parse_skill_archive(output.getvalue())


def run_script(package,destination,mode,*,cwd=None):
    encoded=json.dumps(package.manifest).encode()
    framed=len(encoded).to_bytes(4,'big')+encoded+package.archive
    return subprocess.run([sys.executable,'-I','-c',_MATERIALIZE_SCRIPT,mode,str(destination),
        package.digest],input=framed,capture_output=True,timeout=30,cwd=cwd)


def test_script_installs_exact_files_and_resume_only_verifies(tmp_path):
    package=artifact()
    destination=tmp_path/'skills'/'task'/'sample'
    result=run_script(package,destination,'install')
    assert result.returncode==0,result.stderr
    assert (destination/'scripts'/'run.py').read_bytes()==package.files['scripts/run.py']
    before=(destination/'SKILL.md').stat().st_mtime_ns
    assert run_script(package,destination,'verify').returncode==0
    assert (destination/'SKILL.md').stat().st_mtime_ns==before
    assert run_script(package,destination,'install').returncode!=0
    path=destination/'SKILL.md'
    path.chmod(0o600)
    path.write_text('tampered')
    assert run_script(package,destination,'verify').returncode!=0


@pytest.mark.parametrize('case',['parent_symlink','file_hardlink','extra_file'])
def test_script_rejects_unsafe_or_changed_materialized_tree(tmp_path,case):
    package=artifact()
    destination=tmp_path/'skills'/'task'
    outside=tmp_path/'outside'
    outside.mkdir()
    if case=='parent_symlink':
        (tmp_path/'skills').symlink_to(outside,target_is_directory=True)
        assert run_script(package,destination,'install').returncode!=0
        assert list(outside.iterdir())==[]
    elif case=='file_hardlink':
        destination.mkdir(parents=True)
        victim=outside/'victim'
        victim.write_bytes(b'untouched')
        (destination/'SKILL.md').hardlink_to(victim)
        assert run_script(package,destination,'install').returncode!=0
        assert victim.read_bytes()==b'untouched'
    else:
        assert run_script(package,destination,'install').returncode==0
        (destination/'extra.py').write_text('injected import')
        assert run_script(package,destination,'verify').returncode!=0


async def test_materializer_registers_only_pinned_core_skills_using_task_backend(tmp_path,monkeypatch):
    import workmate_runtime.assembly.skills as module
    package=artifact()
    ctx=context()
    cap=CapabilityReference('cap','skill','scope','custom-alias')
    ctx=replace(ctx,capabilities=(cap,),initial_spec=ctx.initial_spec.model_copy(update={
        'capability_ids':('cap',),'skill_artifacts':(SkillArtifactReference(capability_id='cap',digest=package.digest),)}))
    async def load(db,digest):
        assert digest==package.digest
        return package
    monkeypatch.setattr(module,'load_skill_artifact',load)
    workspace=manager(tmp_path)
    binding=await workspace.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,read_model_input_types=['text/plain'])
    calls=[]
    async def execute(command,raw):
        calls.append((command,raw))
        return SimpleNamespace(exit_code=0)
    monkeypatch.setattr(binding.backend,'_exec_with_input',execute)
    skills=await materialize_execution_skills(ctx,binding,Sessions(),resume=False)
    assert len(skills)==1 and skills[0].name=='sample'
    assert skills[0].dir==f'/workspace/skills/task/cap/{package.digest}'
    assert calls[0][0][4]=='install' and calls[0][1].endswith(package.archive)
    assert len(calls[0][0])==7
    toolkit=Toolkit(skills_or_loaders=skills)
    assert (await toolkit._get_available_skills())['sample'].markdown=='Use scripts/run.py'
    await materialize_execution_skills(ctx,binding,Sessions(),resume=True)
    assert calls[-1][0][4]=='verify'
    await workspace.close()


from test_worker_agent_assembly import assembly,LocalGuard


async def test_assembler_connects_pinned_skill_source_to_actual_core_toolkit(assembly,monkeypatch):
    import workmate_runtime.assembly.skills as module
    assembler,fixture,workspace=assembly
    from workmate_runtime.assembly.skill_cache import SkillDirectoryCache
    from workmate_product.application.skill_repository import SkillArtifactMetadata
    assembler.skill_cache=SkillDirectoryCache(workspace.basedir.parent/(workspace.basedir.name+'-cache'))
    package=artifact()
    async def load_metadata(db,digest):
        return SkillArtifactMetadata(package.digest,1,f'sha256/{package.digest[:2]}/{package.digest}.zip',len(package.archive),package.manifest,
                                     package.name,package.description,package.version)
    monkeypatch.setattr(module.skill_repository,'load_skill_artifact_metadata',load_metadata)
    cap=CapabilityReference('cap','skill','scope','custom-alias')
    original=fixture['context']
    fixture['context']=replace(original,capabilities=(cap,),initial_spec=original.initial_spec.model_copy(update={
        'capability_ids':('cap',),'skill_artifacts':(SkillArtifactReference(capability_id='cap',digest=package.digest),)}))
    async def load(db,digest):
        assert digest==package.digest
        return package
    monkeypatch.setattr(module,'load_skill_artifact',load)
    constructor=workspace._task_backend
    modes=[]
    def bound_backend(*args):
        backend=constructor(*args)
        async def execute(command,raw):
            modes.append(command[4])
            return SimpleNamespace(exit_code=0)
        monkeypatch.setattr(backend,'_exec_with_input',execute)
        return backend
    monkeypatch.setattr(workspace,'_task_backend',bound_backend)
    prepared=await assembler.prepare_start(OWNER,None,LocalGuard())
    registered=await prepared.engine.agent.toolkit._get_available_skills()
    assert set(registered)=={'sample'}
    assert 'sample' in await prepared.engine.agent.toolkit.get_skill_instructions()
    snapshot=prepared.engine.snapshot()
    resumed=await assembler.prepare_resume(OWNER,snapshot,LocalGuard())
    assert resumed.snapshot()==snapshot
    assert set(await resumed.agent.toolkit._get_available_skills())=={'sample'}
    assert modes==['install','verify']


def test_large_manifest_uses_stdin_without_kernel_argument_limit(tmp_path):
    output=BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('SKILL.md','---\nname: sample\ndescription: Large package\n---\nWork.')
        for i in range(2000):
            bundle.writestr(f'resources/resource_{i}.txt',b'x')
    package=parse_skill_archive(output.getvalue())
    assert len(json.dumps(package.manifest).encode())>128*1024
    assert run_script(package,tmp_path/'large','install').returncode==0
    assert run_script(package,tmp_path/'large','verify').returncode==0


def test_resume_rejects_permission_tampering_without_content_change(tmp_path):
    package=artifact()
    destination=tmp_path/'skill'
    assert run_script(package,destination,'install').returncode==0
    path=destination/'scripts'/'run.py'
    path.chmod(0o555)
    assert path.read_bytes()==package.files['scripts/run.py']
    assert run_script(package,destination,'verify').returncode!=0


def test_sandbox_script_does_not_import_modules_from_workspace(tmp_path):
    (tmp_path/'zipfile.py').write_text("raise RuntimeError('injected workspace module')")
    package=artifact()
    result=run_script(package,tmp_path/'safe-skill','install',cwd=tmp_path)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'safe-skill'/'SKILL.md').read_bytes()==package.files['SKILL.md']


async def test_materializer_cache_avoids_download_and_still_checks_current_metadata(tmp_path,monkeypatch):
    import workmate_runtime.assembly.skills as module
    from workmate_runtime.assembly.skill_cache import SkillDirectoryCache
    package=artifact()
    ctx=context()
    cap=CapabilityReference('cap','skill','scope','custom-alias')
    ctx=replace(ctx,capabilities=(cap,),initial_spec=ctx.initial_spec.model_copy(update={
        'capability_ids':('cap',),'skill_artifacts':(SkillArtifactReference(capability_id='cap',digest=package.digest),)}))
    downloads=[]
    reads=[]
    permitted=True
    async def load(db,digest):
        downloads.append(digest)
        return package
    def validate(value):
        if value!=package:
            raise ValueError('metadata differs')
    async def load_metadata(db,digest):
        reads.append(digest)
        if not permitted:
            raise ValueError('pinned artifact unavailable')
        return SimpleNamespace(digest=digest,validate=validate)
    monkeypatch.setattr(module,'load_skill_artifact',load)
    monkeypatch.setattr(module.skill_repository,'load_skill_artifact_metadata',load_metadata,raising=False)
    workspace=manager(tmp_path)
    binding=await workspace.bind_task(IDENTITY,OWNER,fresh_claim=True,all_resource_paths_tracked=True,read_model_input_types=['text/plain'])
    calls=[]
    async def execute(command,raw):
        calls.append((command,raw))
        return SimpleNamespace(exit_code=0)
    monkeypatch.setattr(binding.backend,'_exec_with_input',execute)
    cache=SkillDirectoryCache(tmp_path/'private-cache')
    try:
        await materialize_execution_skills(ctx,binding,Sessions(),resume=False,cache=cache)
        await materialize_execution_skills(ctx,binding,Sessions(),resume=True,cache=cache)
        assert downloads==[package.digest]
        assert reads==[package.digest,package.digest]
        assert [call[0][4] for call in calls]==['install','verify']
        assert all(call[1].endswith(package.archive) for call in calls)
        assert all(str(cache.root) not in repr(call[0]) for call in calls)
        (cache.root/package.digest/'tree'/'SKILL.md').unlink()
        await materialize_execution_skills(ctx,binding,Sessions(),resume=True,cache=cache)
        assert downloads==[package.digest,package.digest]
        permitted=False
        with pytest.raises(ValueError,match='unavailable'):
            await materialize_execution_skills(ctx,binding,Sessions(),resume=True,cache=cache)
        assert len(calls)==3
    finally:
        await workspace.close()


async def test_remote_trusted_task_backend_accepted_but_ducktype_rejected():
    from workmate_runtime.execution.resources import TrustedTaskBackend,TaskResourceRegistry
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    class Remote(TrustedTaskBackend):
        _execution_owner=OWNER
        _task_resources=registry
    ctx=context()
    assert not ctx.initial_spec.skill_artifacts
    binding=SimpleNamespace(owner=OWNER,backend=Remote())
    assert await materialize_execution_skills(ctx,binding,None,resume=False)==()
    binding.backend=SimpleNamespace(_execution_owner=OWNER,_task_resources=registry)
    with pytest.raises(SkillMaterializationError):
        await materialize_execution_skills(ctx,binding,None,resume=False)
