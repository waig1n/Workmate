"""Trusted node receipt foundation: real PG, no owner takeover or host claims.

The verifier fixture models an already authenticated node supervisor. These tests
prove atomic persistence and binding checks, not actual Docker/cgroup death.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
import json

import pytest
from workmate_contracts import snapshot_digest
from workmate_contracts.node_evidence import (NodeContainment,WorkerIncarnation,WorkspaceGeneration,
    ResourceRegistration,ResourceObservation,PermissionParkedBoundary,NodeQuiescenceEvidence)
from workmate_product.application.commands import CommandRejected,CommandConflict
from workmate_product.application.node_recovery import (register_incarnation,open_resource_ledger,
    register_resource,record_quiescence_evidence,resource_ledger_digest)
from test_durable_projection import projection_database  # noqa: F401
from test_control_commands import parked
from test_durable_interactions import CALL,SECOND,park,consume


async def verified(*args):
    return True


async def denied(*args):
    return False


@dataclass
class NodeCase:
    sessions: object
    admin: object
    worker: WorkerIncarnation
    workspace: WorkspaceGeneration
    registrations: list

    async def prepare(self,*,root=True):
        await self.admin.execute("""UPDATE runtime_sessions SET status='preparing',reply_id=NULL,
            owner_id='old-worker',lease_until=clock_timestamp()+interval '60 seconds';
            UPDATE runtime_task_executions SET status='preparing',reply_id=NULL;""")
        async with self.sessions.begin() as db:
            await register_incarnation(db,self.worker,verify_authority=verified)
            await open_resource_ledger(db,task_id='t',execution_epoch=1,worker_id=self.worker.worker_id,
                workspace=self.workspace,verify_authority=verified)
            if root:
                await register_resource(db,self.resource('root',1,kind='session_infrastructure'),verify_authority=verified)
        if root:
            self.registrations.append(self.resource('root',1,kind='session_infrastructure'))
        return self

    def resource(self,resource_id,seq,*,kind='process',launch_state='started'):
        return ResourceRegistration(resource_id=resource_id,worker_id=self.worker.worker_id,
            session_id='s',task_id='t',execution_epoch=1,registration_seq=seq,kind=kind,
            launch_state=launch_state,containment_ref=self.worker.containment.runtime_instance_id,
            original_tool_call_id='a' if kind=='external_job' else None)

    async def add(self,resource):
        async with self.sessions.begin() as db:
            await register_resource(db,resource,verify_authority=verified)
        self.registrations.append(resource)

    async def park(self):
        await self.admin.execute("""UPDATE runtime_sessions SET status='running',reply_id='reply';
            UPDATE runtime_task_executions SET status='running',reply_id='reply';""")
        command=await parked(self.sessions,self.admin)
        row=await self.admin.fetchrow('SELECT * FROM runtime_snapshots ORDER BY state_version DESC LIMIT 1')
        return PermissionParkedBoundary(snapshot_id=row['id'],snapshot_sha256=snapshot_digest(json.loads(row['payload'])),
            state_version=row['state_version'],runtime_seq=row['runtime_seq'],reply_id='reply',
            request_version=command.request_version,pending_tool_call_ids=('a','b'))

    def evidence(self,boundary,*,conclusion='confirmed',worker_state='terminated',containment_state='terminated',state='terminated'):
        return NodeQuiescenceEvidence(evidence_id='proof',authority_id=self.worker.node_authority_id,
            worker=self.worker,workspace=self.workspace,task_id='t',execution_epoch=1,boundary=boundary,
            registered_through_seq=len(self.registrations),
            ledger_sha256=resource_ledger_digest(self.worker,self.workspace,'t',1,self.registrations),
            observed_at=datetime.now(timezone.utc),worker_state=worker_state,containment_state=containment_state,
            conclusion=conclusion,observations=tuple(ResourceObservation(registration=r,state=state,
                observation_ref='observed-'+r.resource_id) for r in self.registrations))

    async def accept(self,evidence,verify=verified):
        async with self.sessions.begin() as db:
            return await record_quiescence_evidence(db,evidence,verify_authority=verify)


def make_case(projection_database):
    sessions,admin=projection_database
    worker=WorkerIncarnation(worker_id='old-worker',storage_node_id='node',node_authority_id='node-authority',
        host_boot_id='11111111-1111-1111-1111-111111111111',host_pid=12345,process_start_ticks=100,
        containment=NodeContainment(kind='container',runtime_instance_id='container-old',generation='container-generation-1',
            pid_namespace_inode=123456))
    workspace=WorkspaceGeneration(session_id='s',conversation_id='c',storage_node_id='node',generation='workspace-generation-1',
        volume_id='volume-original',workspace_id='workspace-c')
    return NodeCase(sessions,admin,worker,workspace,[])


async def assert_no_transfer(admin):
    session=await admin.fetchrow('SELECT * FROM runtime_sessions')
    assert session['owner_id']=='old-worker' and session['execution_epoch']==1
    assert session['active_task_id']=='t' and session['status']=='awaiting_interaction'
    assert await admin.fetchval('SELECT active_product_task_id FROM conversations')=='t'
    assert await admin.fetchval('SELECT status FROM tasks')=='awaiting_permission'


async def test_verified_receipt_freezes_exact_ledger_once_without_transferring_owner(projection_database):
    case=await make_case(projection_database).prepare()
    await case.add(case.resource('child',2))
    boundary=await case.park()
    evidence=case.evidence(boundary)
    old_snapshot=await case.admin.fetchval('SELECT payload FROM runtime_snapshots')
    old_request=await case.admin.fetchval("SELECT payload FROM task_events WHERE event_type='hitl_request'")
    results=await asyncio.gather(case.accept(evidence),case.accept(evidence))
    assert results[0]==results[1]
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==1
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is True
    assert await case.admin.fetchval("SELECT count(*) FROM task_events WHERE event_type='runtime_quiescence_recorded'")==1
    assert await case.admin.fetchval('SELECT payload FROM runtime_snapshots')==old_snapshot
    assert await case.admin.fetchval("SELECT payload FROM task_events WHERE event_type='hitl_request'")==old_request
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('field',['worker','containment','resource'])
@pytest.mark.parametrize('state',['active','unknown'])
async def test_live_or_unknown_observation_never_confirms_quiescence(projection_database,field,state):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    kwargs={'conclusion':'unknown',{'worker':'worker_state','containment':'containment_state','resource':'state'}[field]:state}
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary,**kwargs))
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==0
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('state',['reserved','unknown'])
async def test_unresolved_launch_is_not_hidden_by_terminated_root(projection_database,state):
    case=await make_case(projection_database).prepare()
    await case.add(case.resource('pending-launch',2,launch_state=state))
    boundary=await case.park()
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary,conclusion='unknown'))
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==2
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_empty_or_after_the_fact_ledger_cannot_prove_quiescence(projection_database):
    case=await make_case(projection_database).prepare(root=False)
    boundary=await case.park()
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary,conclusion='unknown'))
    async with case.sessions.begin() as db:
        with pytest.raises((CommandRejected,CommandConflict)):
            await open_resource_ledger(db,task_id='t',execution_epoch=1,worker_id=case.worker.worker_id,
                workspace=case.workspace.model_copy(update={'generation':'replacement'}),verify_authority=verified)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_external_job_is_not_covered_by_local_namespace_death(projection_database):
    case=await make_case(projection_database).prepare()
    await case.add(case.resource('remote-job',2,kind='external_job'))
    boundary=await case.park()
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary,conclusion='unknown'))
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==0


async def test_untrusted_authority_cannot_register_or_accept(projection_database):
    case=make_case(projection_database)
    async with case.sessions.begin() as db:
        with pytest.raises(CommandRejected):
            await register_incarnation(db,case.worker,verify_authority=denied)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_worker_incarnations')==0
    await case.prepare()
    boundary=await case.park()
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary),verify=denied)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


@pytest.mark.parametrize('expired',[False,True])
async def test_trusted_death_is_independent_of_database_lease(projection_database,expired):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    if expired:
        await case.admin.execute("UPDATE runtime_sessions SET lease_until=clock_timestamp()-interval '1 second'")
    await case.accept(case.evidence(boundary))
    await assert_no_transfer(case.admin)


async def test_incarnation_registration_is_immutable_and_idempotent(projection_database):
    case=await make_case(projection_database).prepare()
    async with case.sessions.begin() as db:
        await register_incarnation(db,case.worker,verify_authority=verified)
    async with case.sessions.begin() as db:
        with pytest.raises(CommandConflict):
            await register_incarnation(db,case.worker.model_copy(update={'process_start_ticks':101}),verify_authority=verified)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_worker_incarnations')==1


@pytest.mark.parametrize('change',[{'process_start_ticks':101},{'host_pid':12346},
    {'host_boot_id':'22222222-2222-2222-2222-222222222222'}])
async def test_reused_process_identity_does_not_prove_original_worker_exit(projection_database,change):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    evidence=case.evidence(boundary).model_copy(update={'worker':case.worker.model_copy(update=change)})
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.accept(evidence)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


@pytest.mark.parametrize('field,value',[('snapshot_sha256','0'*64),('request_version','0'*64),
    ('pending_tool_call_ids',('a',)),('snapshot_id','other-snapshot'),('reply_id','other-reply'),
    ('runtime_seq',999),('state_version',999)])
async def test_proof_must_match_exact_current_permission_boundary(projection_database,field,value):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.accept(case.evidence(boundary.model_copy(update={field:value})))
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('field,value',[('generation','replacement'),('volume_id','new-volume'),
    ('workspace_id','other-workspace')])
async def test_workspace_replacement_cannot_reuse_original_proof(projection_database,field,value):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    proof=case.evidence(boundary).model_copy(update={'workspace':case.workspace.model_copy(update={field:value})})
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.accept(proof)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_new_snapshot_invalidates_older_parked_proof(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    proof=case.evidence(boundary)
    seq=await park(case.admin,[CALL,SECOND])
    await consume(case.sessions)
    for table in ('runtime_sessions','runtime_task_executions'):
        await case.admin.execute(f'UPDATE {table} SET state_version=2,snapshot_id=$1',str(seq))
    assert seq>boundary.runtime_seq
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.accept(proof)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_conflicting_retry_never_replaces_accepted_proof(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    proof=case.evidence(boundary)
    await case.accept(proof)
    with pytest.raises(CommandConflict):
        await case.accept(proof.model_copy(update={'observed_at':proof.observed_at+timedelta(seconds=1)}))
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==1
    assert await case.admin.fetchval('SELECT digest FROM runtime_node_quiescence_evidence')==proof.digest()


async def test_register_first_invalidates_stale_ledger_proof(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    proof=case.evidence(boundary)
    await case.add(case.resource('late-child',2))
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.accept(proof)
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==2
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_freeze_first_rejects_later_resource_registration(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    await case.accept(case.evidence(boundary))
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.add(case.resource('late-child',2))
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==1
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_registrations')==1


async def test_registration_races_atomic_freeze_without_lost_resource(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    results=await asyncio.gather(case.accept(case.evidence(boundary)),
        case.add(case.resource('racing-child',2)),return_exceptions=True)
    assert sum(not isinstance(result,Exception) for result in results)==1
    assert all(not isinstance(result,Exception) or isinstance(result,(CommandRejected,CommandConflict)) for result in results)
    frozen=await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers')
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==(1 if frozen else 2)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==(1 if frozen else 0)
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('change',["lease_until=clock_timestamp()-interval '1 second'","owner_id='replacement'","execution_epoch=2"])
async def test_stale_owner_cannot_extend_ledger(projection_database,change):
    case=await make_case(projection_database).prepare()
    await case.admin.execute('UPDATE runtime_sessions SET '+change)
    with pytest.raises((CommandRejected,CommandConflict)):
        await case.add(case.resource('unauthorized-child',2))
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==1


async def test_outer_transaction_rollback_undoes_receipt_and_freeze(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    with pytest.raises(RuntimeError,match='abort transaction'):
        async with case.sessions.begin() as db:
            await record_quiescence_evidence(db,case.evidence(boundary),verify_authority=verified)
            raise RuntimeError('abort transaction')
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==0
    await assert_no_transfer(case.admin)


@pytest.mark.parametrize('offset',[-3600,3600])
async def test_observation_time_must_cover_current_durable_boundary(projection_database,offset):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    proof=case.evidence(boundary)
    with pytest.raises(CommandRejected):
        await case.accept(proof.model_copy(update={'observed_at':proof.observed_at+timedelta(seconds=offset)}))
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_unregistered_resource_cannot_replace_registered_root(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    proof=case.evidence(boundary)
    different=case.resource('unregistered-root',1,kind='session_infrastructure')
    proof=proof.model_copy(update={'observations':(ResourceObservation(registration=different,
        state='terminated',observation_ref='other-root-death'),),
        'ledger_sha256':resource_ledger_digest(case.worker,case.workspace,'t',1,[different])})
    with pytest.raises(CommandRejected):
        await case.accept(proof)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_tampered_ledger_digest_cannot_freeze_resources(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary).model_copy(update={'ledger_sha256':'0'*64}))
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_node_quiescence_evidence')==0


@pytest.mark.parametrize('state',['reserved','unknown'])
async def test_confirmed_proof_cannot_omit_unresolved_registration(projection_database,state):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    root_only=case.evidence(boundary)
    await case.add(case.resource('unresolved',2,launch_state=state))
    # Keep the asserted observation later than the registration: rejection must
    # be for an incomplete ledger, rather than merely a stale timestamp.
    root_only=root_only.model_copy(update={'observed_at':datetime.now(timezone.utc)})
    with pytest.raises(CommandRejected):
        await case.accept(root_only)
    assert await case.admin.fetchval('SELECT resource_seq FROM runtime_resource_ledgers')==2
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


@pytest.mark.parametrize('kind,status',[('StopTask','accepted'),('ResumeInteraction','claimed'),('ResumeInteraction','indeterminate')])
async def test_unresolved_control_command_blocks_death_receipt(projection_database,kind,status):
    from test_control_commands import admit,stop
    from workmate_contracts import Command
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    if kind=='StopTask':
        await admit(case.sessions,stop())
    else:
        state=json.loads(await case.admin.fetchval('SELECT state FROM task_projection_states'))
        request=state['pending_interactions'][0]
        cmd=Command(command_id='resume',kind=kind,conversation_id='c',runtime_session_id='s',product_task_id='t',
            reply_id='reply',expected_epoch=1,interaction_id=request['interaction_id'],request_version=request['request_version'],
            payload={'decision_version':'decision','snapshot_id':boundary.snapshot_id,'state_version':1,
                'decisions':[{'tool_call_id':'a','approved':True}]})
        await admit(case.sessions,cmd)
        await case.admin.execute('UPDATE runtime_commands SET status=$1',status)
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary))
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


async def test_initial_schema_immutable_evidence_guards(projection_database):
    import asyncpg
    case=make_case(projection_database)
    tables=('runtime_resource_launch_resolutions','runtime_node_quiescence_evidence','runtime_resource_registrations','runtime_resource_ledgers','runtime_worker_incarnations')
    await case.prepare()
    for change in ("workspace_digest='tampered'","worker_id='replacement'","resource_seq=0"):
        with pytest.raises(asyncpg.PostgresError,match='immutable'):
            await case.admin.execute('UPDATE runtime_resource_ledgers SET '+change)
    boundary=await case.park()
    await case.accept(case.evidence(boundary))
    for table in tables[1:]:
        with pytest.raises(asyncpg.PostgresError,match='immutable|cannot be deleted'):
            await case.admin.execute(f'DELETE FROM {table}')
        field='closed=false' if table=='runtime_resource_ledgers' else "digest='tampered'"
        with pytest.raises(asyncpg.PostgresError,match='immutable'):
            await case.admin.execute(f'UPDATE {table} SET '+field)
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is True


async def test_resource_creation_time_is_not_transaction_start_time(projection_database):
    from sqlalchemy import text
    case=await make_case(projection_database).prepare()
    async with case.sessions.begin() as db:
        started=await db.scalar(text('SELECT transaction_timestamp()'))
        await db.execute(text('SELECT pg_sleep(0.01)'))
        await register_resource(db,case.resource('late-registration',2),verify_authority=verified)
        created=await db.scalar(text('SELECT created_at FROM runtime_resource_registrations WHERE registration_seq=2'))
        assert created>started


async def test_same_tool_call_id_with_changed_original_input_is_rejected(projection_database):
    case=await make_case(projection_database).prepare()
    boundary=await case.park()
    state=json.loads(await case.admin.fetchval('SELECT state FROM task_projection_states'))
    # Runtime event and snapshot are immutable. Simulate a divergent mutable
    # Product projection and require receipt intake to compare original input.
    state['interactions'][state['pending_interactions'][0]['interaction_id']]['tool_calls'][0]['input']='{"path":"different"}'
    await case.admin.execute('UPDATE task_projection_states SET state=$1::jsonb',json.dumps(state))
    with pytest.raises(CommandRejected):
        await case.accept(case.evidence(boundary))
    assert await case.admin.fetchval('SELECT closed FROM runtime_resource_ledgers') is False


@pytest.mark.parametrize('existing',['sandbox','baseline'])
async def test_started_execution_cannot_backfill_prelaunch_ledger(projection_database,existing):
    case=make_case(projection_database)
    await case.admin.execute("UPDATE runtime_sessions SET status='preparing',reply_id=NULL,owner_id='old-worker',lease_until=clock_timestamp()+interval '60 seconds'; UPDATE runtime_task_executions SET status='preparing',reply_id=NULL")
    if existing=='sandbox':
        await case.admin.execute("INSERT INTO runtime_sandbox_attestations(task_id,session_id,worker_id,execution_epoch,workspace_id) VALUES ('t','s','old-worker',1,'workspace-c')")
    else:
        await case.admin.execute("INSERT INTO runtime_delivery_baselines(task_id,session_id,worker_id,execution_epoch,storage_node_id,payload,digest) VALUES ('t','s','old-worker',1,'node','{}',$1)",'0'*64)
    async with case.sessions.begin() as db:
        await register_incarnation(db,case.worker,verify_authority=verified)
        with pytest.raises(CommandRejected):
            await open_resource_ledger(db,task_id='t',execution_epoch=1,worker_id=case.worker.worker_id,
                workspace=case.workspace,verify_authority=verified)
    assert await case.admin.fetchval('SELECT count(*) FROM runtime_resource_ledgers')==0
