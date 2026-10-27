"""Local subprocess contract integration, explicitly without UID isolation.

The SDK/control admission is a fixture; real UID isolation is covered only by
root-only supervisor tests. File bytes and Skill script execution are real.
"""
import asyncio
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import pytest

from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
from workmate_runtime.sandbox.remote import RemoteSandboxProvider
from workmate_runtime.execution.resources import TaskResourceRegistry,ResourceScopeError
from workmate_runtime.delivery.remote_files import RemoteDeliveryFiles
from workmate_runtime.assembly.skills import materialize_execution_skills
from workmate_product.application.execution_context import CapabilityReference,SkillArtifactReference
from test_worker_agent_assembly import context,Sessions,OWNER
from test_worker_skills import artifact


class LocalControl(TrustedSandboxControlBackend):
    def __init__(self,workdir):
        self.python=sys.executable
        self.directory='/fixture-control'
        self.workdir=str(workdir)
        self.active=False
        self.epoch=''
        self.operations=[]

    async def operation(self,operation,**payload):
        self.operations.append(operation)
        epoch=payload.get('epoch')
        if operation=='begin':
            assert not self.active or self.epoch==epoch
            self.active,self.epoch=True,epoch
        elif operation=='seal':
            assert self.epoch==epoch
            self.active=False
        elif operation=='kill':
            assert not self.active and self.epoch==epoch
        return {'active':self.active,'epoch':self.epoch,'pids':[]}

    async def exec_shell(self,command,**kwargs):
        if len(command)>2 and command[2]==self.directory+'/launch.py':
            assert self.active and command[5]==self.epoch
            command=command[7:]
        process=await asyncio.create_subprocess_exec(*command,cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await asyncio.wait_for(process.communicate(),timeout=10)
        return SimpleNamespace(exit_code=process.returncode,stdout=stdout,stderr=stderr)


async def test_provider_binding_real_files_skills_and_sealed_artifact(tmp_path,monkeypatch):
    work=tmp_path/'workspace'
    work.mkdir()
    (work/'output').mkdir()
    control=LocalControl(work)
    workspace=SimpleNamespace(_workmate_initialized=True,_workmate_provider='docker',_workmate_control=control,
        workdir=str(work),_gateway=None)
    registry=TaskResourceRegistry()
    registry.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    provider=RemoteSandboxProvider(SimpleNamespace(provider='docker'))
    backend=provider.bind_backend(workspace,OWNER,registry)
    files=RemoteDeliveryFiles(control,str(work),tmp_path/'objects',max_file_bytes=1024,max_files=2)
    baseline=await files.baseline()
    await backend.write_file(str(work/'output'/'result.txt'),b'remote contract')
    assert await backend.read_file(str(work/'output'/'result.txt'))==b'remote contract'
    package=artifact()
    cap=CapabilityReference('cap','skill','scope','sample')
    ctx=context()
    ctx=replace(ctx,capabilities=(cap,),initial_spec=ctx.initial_spec.model_copy(update={
        'capability_ids':('cap',),'skill_artifacts':(SkillArtifactReference(capability_id='cap',digest=package.digest),)}))
    async def load(db,digest): return package
    monkeypatch.setattr('workmate_runtime.assembly.skills.load_skill_artifact',load)
    binding=SimpleNamespace(owner=OWNER,backend=backend,workspace=workspace)
    skills=await materialize_execution_skills(ctx,binding,Sessions(),resume=False)
    assert len(skills)==1
    assert (work/'skills'/'task'/'cap'/package.digest/'SKILL.md').is_file()
    await materialize_execution_skills(ctx,binding,Sessions(),resume=True)
    assert await registry.verify_preparation_cleanup(OWNER)
    with pytest.raises(ResourceScopeError):
        await backend.exec_shell(['true'])
    payload=await files.capture(baseline)
    assert payload['files'][0]['name']=='result.txt'
    assert registry._scope(OWNER).sealed
    assert control.operations==['begin','seal','kill','probe']
