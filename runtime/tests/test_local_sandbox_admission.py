"""Local mode admits governed execution without creating isolation evidence."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from workmate_runtime.assembly.sandbox_proof import attest_task_sandbox
from workmate_runtime.execution.controller import ExecutionAborted,SandboxVerificationFailed
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.transport.postgres import ExecutionOwner

OWNER=ExecutionOwner('session','task','worker',1)


def binding():
    from workmate_runtime.sandbox.local import LocalSandboxProvider,LocalTaskBackend
    provider=LocalSandboxProvider.__new__(LocalSandboxProvider)
    provider.name='local'
    backend=LocalTaskBackend.__new__(LocalTaskBackend)
    backend._execution_owner=OWNER
    backend._task_resources=TaskResourceRegistry()
    backend._task_resources.open_scope(OWNER,fresh_claim=True,all_resource_paths_tracked=True)
    return SimpleNamespace(owner=OWNER,provider=provider,backend=backend,
        workspace=SimpleNamespace(workspace_id='workspace'))


class Sessions:
    def __init__(self,*,profile='standard',lease=True,epoch=1):
        self.profile,self.lease,self.epoch=profile,lease,epoch
        self.statements=[]
    @asynccontextmanager
    async def begin(self): yield self
    async def execute(self,query,params):
        query=str(query)
        self.statements.append(query)
        if 'FROM tasks' in query:
            row={'status':'created','approval_profile':self.profile}
        elif 'FROM runtime_sessions' in query:
            row={'active_task_id':OWNER.task_id,'owner_id':OWNER.worker_id,
                'execution_epoch':self.epoch,'status':'preparing','reply_id':None}
        else:
            pytest.fail('Local mode must never INSERT isolation proof')
        return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:row))
    async def scalar(self,query,params): return self.lease


async def test_local_standard_task_admitted_without_attestation():
    sessions=Sessions()
    assert await attest_task_sandbox(binding(),OWNER,sessions) is None
    assert len(sessions.statements)==2


async def test_local_full_access_task_rejected_without_attestation():
    with pytest.raises(SandboxVerificationFailed,match='Full Access'):
        await attest_task_sandbox(binding(),OWNER,Sessions(profile='full_access'))


@pytest.mark.parametrize('settings',[{'lease':False},{'epoch':2}])
async def test_local_mode_still_checks_execution_fence(settings):
    with pytest.raises(ExecutionAborted):
        await attest_task_sandbox(binding(),OWNER,Sessions(**settings))


async def test_provider_name_alone_cannot_bypass_isolation():
    fake=binding()
    fake.provider=SimpleNamespace(name='local')
    with pytest.raises(SandboxVerificationFailed):
        await attest_task_sandbox(fake,OWNER,Sessions())


def test_local_never_satisfies_full_access_proof_predicate():
    from workmate_product.application.sandbox_proof import sandbox_proof_valid
    assert not sandbox_proof_valid({'isolation':'local','containment':'none','instance_id':'host','evidence':{}})


@pytest.mark.parametrize('invalid',['untrusted','owner','registry'])
async def test_local_mode_requires_trusted_owned_resource_backend(invalid):
    value=binding()
    if invalid=='untrusted':
        value.backend=SimpleNamespace(_execution_owner=OWNER,_task_resources=value.backend._task_resources)
    elif invalid=='owner':
        value.backend._execution_owner=None
    else:
        value.backend._task_resources=object()
    with pytest.raises(SandboxVerificationFailed):
        await attest_task_sandbox(value,OWNER,Sessions())


async def test_local_mode_does_not_reopen_sealed_resource_scope():
    from workmate_runtime.execution.resources import ResourceScopeError
    value=binding()
    value.backend._task_resources._scope(OWNER).sealed=True
    with pytest.raises(ResourceScopeError):
        await attest_task_sandbox(value,OWNER,Sessions())
