"""Trusted resource-close receipts against isolated PostgreSQL transactions."""
import pytest
from test_execution_context import database, claim
from test_workspace_residencies import idle
from workmate_runtime.assembly.workspaces import WorkspaceIdentity
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.execution.workspace_residency import WorkspaceResidencyCoordinator


IDENTITY=WorkspaceIdentity('u','a','c','s','node')
OWNER=ExecutionOwner('s','t','worker',1)


class LostCommitResponse:
    def __init__(self,sessions,*,lose_commit=1):
        self.sessions=sessions
        self.lost=False
        self.commits=0
        self.lose_commit=lose_commit

    def begin(self):
        original=self.sessions.begin()
        parent=self
        class Transaction:
            async def __aenter__(self): return await original.__aenter__()
            async def __aexit__(self,*args):
                result=await original.__aexit__(*args)
                if args[0] is None:
                    parent.commits+=1
                if args[0] is None and parent.commits==parent.lose_commit:
                    parent.lost=True
                    raise ConnectionError('controlled lost commit response')
                return result
        return Transaction()


async def test_owned_release_is_committed_before_affinity_withdrawal(database):
    sessions,admin=database
    await claim(admin)
    observed=[]
    class Hints:
        async def withdraw_affinity(self,record):
            observed.append(await admin.fetchval('SELECT status FROM workspace_residencies'))
    coordinator=WorkspaceResidencyCoordinator(sessions,'node',registry=Hints())
    hold=await coordinator.record_held(IDENTITY,OWNER,'generation')
    await coordinator.release_owned(IDENTITY,OWNER,hold)
    await coordinator.release_owned(IDENTITY,OWNER,hold)
    assert observed==['released','released']
    assert await admin.fetchval('SELECT active_task_id FROM runtime_sessions')=='t'
    assert await admin.fetchval('SELECT hold_version FROM workspace_residencies')==1


async def test_idle_release_request_cannot_close_running_task(database):
    sessions,admin=database
    await claim(admin)
    coordinator=WorkspaceResidencyCoordinator(sessions,'node')
    hold=await coordinator.record_held(IDENTITY,OWNER,'generation')
    assert await coordinator.begin_idle_release(IDENTITY,hold) is None
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='held'
    await idle(admin)
    release=await coordinator.begin_idle_release(IDENTITY,hold)
    assert release is not None
    await coordinator.finish_idle_release(release)
    await coordinator.finish_idle_release(release)
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='released'


async def test_cleanup_failure_keeps_persistent_blocked_evidence(database):
    sessions,admin=database
    await claim(admin)
    coordinator=WorkspaceResidencyCoordinator(sessions,'node')
    hold=await coordinator.record_held(IDENTITY,OWNER,'generation')
    await idle(admin)
    release=await coordinator.begin_idle_release(IDENTITY,hold)
    await coordinator.block_idle_release(release,RuntimeError('do not persist secrets'))
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='blocked'
    assert await admin.fetchval('SELECT error_code FROM workspace_release_requests')=='RuntimeError'


async def test_foreign_identity_or_hold_is_rejected_before_mutation(database):
    sessions,admin=database
    await claim(admin)
    coordinator=WorkspaceResidencyCoordinator(sessions,'foreign')
    with pytest.raises(ValueError):
        await coordinator.record_held(IDENTITY,OWNER,'generation')
    assert await admin.fetchval('SELECT count(*) FROM workspace_residencies')==0


async def test_held_registration_recovers_a_lost_commit_response(database):
    sessions,admin=database
    await claim(admin)
    coordinator=WorkspaceResidencyCoordinator(LostCommitResponse(sessions),'node')
    hold=await coordinator.record_held(IDENTITY,OWNER,'generation')
    assert hold['hold_version']==1
    assert await admin.fetchval('SELECT count(*) FROM workspace_residencies')==1
    await coordinator.release_owned(IDENTITY,OWNER,hold)


async def test_idle_cas_confirms_only_the_claim_obtained_before_commit_loss(database):
    sessions,admin=database
    await claim(admin)
    coordinator=WorkspaceResidencyCoordinator(sessions,'node')
    hold=await coordinator.record_held(IDENTITY,OWNER,'generation')
    await idle(admin)
    await coordinator.request_idle_release(hold)
    # Lose the CAS commit response, not the release request transaction.
    coordinator.sessions=LostCommitResponse(sessions,lose_commit=2)
    release=await coordinator.begin_idle_release(IDENTITY,hold)
    assert release is not None
    assert await admin.fetchval('SELECT status FROM workspace_residencies')=='releasing'
    await coordinator.finish_idle_release(release)
