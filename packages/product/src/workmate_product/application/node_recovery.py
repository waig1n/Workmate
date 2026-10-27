"""Node evidence intake without ownership transfer or execution dispatch.

The authority callback must authenticate the observer and its actual measurements
within this transaction, with no network I/O or commit. These functions are not
HTTP endpoints and are never callable by a model/browser as self-attestation.
A deployment must supply its trusted observer before enabling this path.
Preparing/no-event checks reject known late registration; they cannot establish
that no preparation process has launched. The observer must enforce an actual
commit-before-launch gate. No existing Worker is retroactively enrolled here.
"""
import json
from workmate_product.persistence.repositories import node_recovery as repository
from workmate_contracts import snapshot_digest
from workmate_contracts.node_evidence import WorkerIncarnation,WorkspaceGeneration,ResourceRegistration,NodeQuiescenceEvidence,ResourceLaunchResolution
from .commands import CommandRejected,CommandConflict
from .control_commands import _transaction


def _validated(kind,value):
    return kind.model_validate_json(value.model_dump_json())


async def _authorized(db,value,verify):
    if not callable(verify) or await verify(db,value) is not True:
        raise CommandRejected('Trusted node authority or measurements unavailable')


def resource_ledger_digest(worker,workspace,task_id,execution_epoch,registrations,*,resolutions=()):
    payload={'worker_digest':worker.digest(),'workspace_digest':workspace.digest(),
        'task_id':task_id,'execution_epoch':execution_epoch,
        'registrations':[r.model_dump(mode='json') for r in sorted(registrations,key=lambda r:r.registration_seq)]}
    if resolutions:
        payload['launch_resolutions']=[r.model_dump(mode='json') for r in
            sorted(resolutions,key=lambda r:r.registration.registration_seq)]
    return snapshot_digest(payload)


async def _execution(db,task_id,epoch,worker_id,*,live):
    if type(epoch) is not int or epoch<1:
        raise CommandRejected('An exact positive execution epoch is required')
    candidate=await repository.task_conversation(db, id=task_id)
    if not candidate:
        raise CommandRejected('Execution is unavailable')
    hint=await repository.conversation_agent(db, id=candidate['conversation_id'])
    if not hint:
        raise CommandRejected('Conversation is unavailable')
    conversation,task,session,execution=await repository.lock_execution_owner(
        db, agent_id=hint['agent_id'], conversation_id=candidate['conversation_id'], task_id=task_id)
    if (not session or not execution or conversation['agent_id']!=hint['agent_id']
            or task['conversation_id']!=conversation['id'] or conversation['active_product_task_id']!=task_id
            or session['active_task_id']!=task_id or session['owner_id']!=worker_id
            or execution['session_id']!=session['id']
            or any(row['execution_epoch']!=epoch for row in (session,execution))):
        raise CommandRejected('Execution ownership or active gates changed')
    if live and not await repository.owner_is_live(db, {'id':session['id']}):
        raise CommandRejected('Resource registration requires a live owner')
    return conversation,task,session,execution


async def register_incarnation(db,incarnation,*,verify_authority):
    _transaction(db)
    incarnation=_validated(WorkerIncarnation,incarnation)
    async with db.begin_nested():
        await _authorized(db,incarnation,verify_authority)
        row=await repository.record_incarnation(db, {'id':incarnation.worker_id,'payload':incarnation.model_dump_json(),'digest':incarnation.digest()})
        if row['digest']!=incarnation.digest():
            raise CommandConflict('Worker incarnation cannot be rebound to another identity')
        return row['digest']


async def open_resource_ledger(db,task_id,execution_epoch,worker_id,workspace,*,verify_authority):
    _transaction(db)
    workspace=_validated(WorkspaceGeneration,workspace)
    async with db.begin_nested():
        await _authorized(db,workspace,verify_authority)
        conversation,task,session,execution=await _execution(db,task_id,execution_epoch,worker_id,live=True)
        instance=await repository.worker_incarnation(db, id=worker_id)
        if not instance:
            raise CommandRejected('Worker incarnation must be registered before resources')
        worker=WorkerIncarnation.model_validate_json(json.dumps(instance['payload']))
        if (worker.digest()!=instance['digest'] or workspace.session_id!=session['id']
                or workspace.conversation_id!=conversation['id']
                or workspace.storage_node_id!=session['storage_node_id']
                or workspace.storage_node_id!=worker.storage_node_id):
            raise CommandRejected('Workspace generation and incarnation do not match execution')
        existing=await repository.lock_resource_ledger(db, tid=task_id, epoch=execution_epoch)
        if existing:
            if existing['worker_id']!=worker_id or existing['workspace_digest']!=workspace.digest():
                raise CommandConflict('Resource ledger identity cannot change')
            return workspace.digest()
        if (any(r['status']!='preparing' or r['reply_id'] is not None for r in (session,execution))
                or await repository.execution_has_started(db, {'tid':task_id})):
            raise CommandRejected('A resource ledger cannot be reconstructed after execution started')
        await repository.insert_resource_ledger(db, dict(tid=task_id,epoch=execution_epoch,worker=worker_id,sid=session['id'],workspace=workspace.model_dump_json(),digest=workspace.digest()))
        return workspace.digest()


async def register_resource(db,registration,*,verify_authority):
    _transaction(db)
    registration=_validated(ResourceRegistration,registration)
    async with db.begin_nested():
        await _authorized(db,registration,verify_authority)
        _,task,session,execution=await _execution(db,registration.task_id,registration.execution_epoch,registration.worker_id,live=True)
        ledger=await repository.lock_resource_ledger(db, tid=registration.task_id, epoch=registration.execution_epoch)
        if (not ledger or ledger['worker_id']!=registration.worker_id or ledger['session_id']!=registration.session_id
                or registration.session_id!=session['id'] or any(r['status'] not in {'preparing','running','awaiting_interaction'} for r in (session,execution))):
            raise CommandRejected('No original resource ledger is available')
        existing=await repository.registration_conflict(db, tid=registration.task_id, epoch=registration.execution_epoch, rid=registration.resource_id, seq=registration.registration_seq)
        if existing:
            if existing['digest']!=registration.digest():
                raise CommandConflict('Resource identity or sequence already has different content')
            return existing['digest']
        if ledger['closed'] or registration.registration_seq!=ledger['resource_seq']+1:
            raise CommandRejected('Resource ledger is closed or registration sequence has a gap')
        await repository.record_registration(db, dict(tid=registration.task_id,epoch=registration.execution_epoch,seq=registration.registration_seq,
                rid=registration.resource_id,payload=registration.model_dump_json(),digest=registration.digest()))
        return registration.digest()


async def record_launch_resolution(db,resolution,*,verify_authority):
    """Persist a trusted final launch fact, never infer it from a missing PID.

    The authority must independently verify an irrevocable fence against any
    future execution of this reservation. not_started requires positive proof
    of no past launch as well. Strings naming fences are not such proof.
    The old lease may expire; original ownership and active gates must remain.
    """
    _transaction(db)
    resolution=_validated(ResourceLaunchResolution,resolution)
    r=resolution.registration
    async with db.begin_nested():
        await _authorized(db,resolution,verify_authority)
        _,_,session,execution=await _execution(db,r.task_id,r.execution_epoch,r.worker_id,live=False)
        ledger=await repository.lock_resource_ledger(db, tid=r.task_id, epoch=r.execution_epoch)
        instance=await repository.worker_incarnation(db, id=r.worker_id)
        if (not ledger or not instance or ledger['worker_id']!=r.worker_id
                or ledger['session_id']!=r.session_id or session['id']!=r.session_id
                or snapshot_digest(instance['payload'])!=instance['digest']):
            raise CommandRejected('Launch resolution does not match original resource ledger')
        worker=WorkerIncarnation.model_validate_json(json.dumps(instance['payload']))
        if (resolution.authority_id!=worker.node_authority_id
                or (resolution.outcome=='started' and resolution.containment_ref!=worker.containment.runtime_instance_id)):
            raise CommandRejected('Launch authority or containment differs from original incarnation')
        row=await repository.resource_registration(db, tid=r.task_id, epoch=r.execution_epoch, seq=r.registration_seq)
        if not row or row['digest']!=r.digest() or snapshot_digest(row['payload'])!=row['digest']:
            raise CommandRejected('Launch resolution changes or omits original registration')
        existing=await repository.launch_resolution(db, tid=r.task_id, epoch=r.execution_epoch, seq=r.registration_seq)
        if existing:
            if existing['digest']!=resolution.digest():
                raise CommandConflict('Original launch already has a different final resolution')
            if snapshot_digest(existing['payload'])!=existing['digest']:
                raise CommandRejected('Persisted launch resolution is corrupt')
            return existing['digest']
        if ledger['closed'] or any(state['status'] not in {'preparing','running','awaiting_interaction'} for state in (session,execution)):
            raise CommandRejected('Closed resource ledger cannot receive new launch facts')
        now=await repository.database_time(db)
        if not max(row['created_at'],ledger['created_at'],instance['created_at'])<=resolution.observed_at<=now:
            raise CommandRejected('Launch observation does not cover the original registration')
        await repository.insert_launch_resolution(db, dict(tid=r.task_id,epoch=r.execution_epoch,seq=r.registration_seq,
                payload=resolution.model_dump_json(),digest=resolution.digest()))
        return resolution.digest()


async def _parked_boundary(db,evidence,task,session,execution):
    b=evidence.boundary
    if (task['status']!='awaiting_permission' or any(row['status']!='awaiting_interaction'
            or row['reply_id']!=b.reply_id or row['snapshot_id']!=b.snapshot_id or row['state_version']!=b.state_version
            for row in (session,execution)) or session['runtime_seq']!=b.runtime_seq):
        raise CommandRejected('Execution is not at the exact permission parked boundary')
    snapshot=await repository.parked_snapshot(db, id=b.snapshot_id)
    event=await repository.boundary_event(db, sid=session['id'], seq=b.runtime_seq)
    if (not snapshot or not event or snapshot['task_id']!=task['id'] or snapshot['session_id']!=session['id']
            or snapshot['execution_epoch']!=evidence.execution_epoch or snapshot['state_version']!=b.state_version
            or snapshot['runtime_seq']!=b.runtime_seq or snapshot['reply_id']!=b.reply_id
            or snapshot['status']!='awaiting_interaction' or snapshot_digest(snapshot['payload'])!=b.snapshot_sha256
            or event['task_id']!=task['id'] or event['execution_epoch']!=evidence.execution_epoch):
        raise CommandRejected('Parked snapshot identity or digest changed')
    payload=event['payload']
    proof=payload.get('boundary_evidence',{})
    pending=proof.get('pending_tools',[])
    if (payload.get('type')!='EXECUTION_BOUNDARY' or payload.get('status')!='awaiting_interaction'
            or payload.get('reply_id')!=b.reply_id or proof.get('protocol_version')!=1
            or proof.get('snapshot_sha256')!=b.snapshot_sha256
            or not pending or any(p.get('kind')!='permission' for p in pending)
            or [p.get('id') for p in pending]!=list(b.pending_tool_call_ids)
            or payload.get('pending_tool_call_ids')!=list(b.pending_tool_call_ids)):
        raise CommandRejected('Runtime boundary is not an exact permission-only proof')
    projection=await repository.projection_state(db, id=task['id'])
    cursor=await repository.projection_cursor(db, id=task['conversation_id'])
    if (not projection or not cursor or projection['session_id']!=session['id'] or projection['reply_id']!=b.reply_id
            or projection['last_runtime_seq']!=b.runtime_seq or cursor['last_runtime_seq']!=b.runtime_seq
            or cursor['session_id']!=session['id'] or projection['format_version']!=1):
        raise CommandRejected('Product permission projection is not current')
    state=projection['state']
    requests=state.get('pending_interactions',[])
    if (state.get('interaction_boundary_id')!=b.snapshot_id or len(requests)!=1
            or requests[0].get('kind')!='permission' or requests[0].get('request_version')!=b.request_version
            or requests[0].get('pending_tool_call_ids')!=list(b.pending_tool_call_ids)):
        raise CommandRejected('Original approval request changed')
    original=state.get('interactions',{}).get(requests[0].get('interaction_id'),{})
    calls={call.get('id'):call for call in original.get('tool_calls',[])}
    if (original.get('kind')!='permission' or original.get('request_version')!=b.request_version
            or original.get('reply_id')!=b.reply_id
            or any(p.get('id') not in calls or any(calls[p['id']].get(key)!=p.get(key)
                for key in ('name','input')) for p in pending)):
        raise CommandRejected('Original tool parameters differ from the parked boundary')
    if await repository.has_unresolved_control(db, {'tid':task['id']}):
        raise CommandRejected('An unresolved control command owns execution')
    return snapshot


async def record_quiescence_evidence(db,evidence,*,verify_authority):
    _transaction(db)
    evidence=_validated(NodeQuiescenceEvidence,evidence)
    async with db.begin_nested():
        await _authorized(db,evidence,verify_authority)
        if evidence.conclusion!='confirmed':
            raise CommandRejected('Unknown node observations cannot freeze a resource ledger')
        conversation,task,session,execution=await _execution(db,evidence.task_id,evidence.execution_epoch,evidence.worker.worker_id,live=False)
        ledger=await repository.lock_resource_ledger(db, tid=evidence.task_id, epoch=evidence.execution_epoch)
        instance=await repository.worker_incarnation(db, id=evidence.worker.worker_id)
        if (not ledger or not instance or instance['digest']!=evidence.worker.digest()
                or snapshot_digest(instance['payload'])!=instance['digest']
                or ledger['workspace_digest']!=evidence.workspace.digest()
                or snapshot_digest(ledger['workspace'])!=ledger['workspace_digest']
                or ledger['worker_id']!=evidence.worker.worker_id or ledger['session_id']!=session['id']
                or evidence.workspace.session_id!=session['id'] or evidence.workspace.conversation_id!=conversation['id']
                or evidence.workspace.storage_node_id!=session['storage_node_id']):
            raise CommandRejected('Node evidence does not match original incarnation and Workspace')
        existing=await repository.quiescence_receipt(db, id=evidence.evidence_id, tid=evidence.task_id, epoch=evidence.execution_epoch)
        if existing:
            if existing['digest']!=evidence.digest():
                raise CommandConflict('Quiescence evidence identity already has different content')
            if (not ledger['closed'] or ledger['ledger_digest']!=evidence.ledger_sha256
                    or ledger['resource_seq']!=evidence.registered_through_seq
                    or ledger['boundary_digest']!=evidence.boundary.digest()
                    or snapshot_digest(ledger['boundary'])!=evidence.boundary.digest()
                    or snapshot_digest(existing['payload'])!=existing['digest']):
                raise CommandRejected('Persisted quiescence receipt and frozen ledger disagree')
            return existing['evidence_id']
        if ledger['closed']:
            raise CommandRejected('Closed ledger has no matching evidence receipt')
        snapshot=await _parked_boundary(db,evidence,task,session,execution)
        rows=await repository.registered_resources(db, {'tid':evidence.task_id,'epoch':evidence.execution_epoch})
        resolution_rows=await repository.launch_resolutions(db, {'tid':evidence.task_id,'epoch':evidence.execution_epoch})
        resolutions=[ResourceLaunchResolution.model_validate_json(json.dumps(row['payload'])) for row in resolution_rows]
        claimed_resolutions=[o.resolution for o in sorted(evidence.observations,key=lambda o:o.registration.registration_seq)
            if o.resolution is not None]
        if (resolutions!=claimed_resolutions or any(row['digest']!=value.digest()
                or row['registration_seq']!=value.registration.registration_seq
                for row,value in zip(resolution_rows,resolutions))):
            raise CommandRejected('Node evidence omits or changes durable launch resolutions')
        registrations=[ResourceRegistration.model_validate_json(json.dumps(row['payload'])) for row in rows]
        if (len(rows)!=evidence.registered_through_seq or ledger['resource_seq']!=len(rows)
                or any(row['digest']!=r.digest() for row,r in zip(rows,registrations))
                or registrations!=[o.registration for o in sorted(evidence.observations,key=lambda o:o.registration.registration_seq)]
                or resource_ledger_digest(evidence.worker,evidence.workspace,evidence.task_id,evidence.execution_epoch,registrations,resolutions=resolutions)!=evidence.ledger_sha256):
            raise CommandRejected('Node evidence omits or changes registered resources')
        now=await repository.database_time(db)
        if not max([snapshot['created_at'],ledger['created_at'],instance['created_at'],*[r['created_at'] for r in rows],*[r['created_at'] for r in resolution_rows]])<=evidence.observed_at<=now:
            raise CommandRejected('Node evidence observation time does not cover this boundary')
        await repository.record_quiescence(db, ledger=dict(boundary=evidence.boundary.model_dump_json(),bdigest=evidence.boundary.digest(),digest=evidence.ledger_sha256,
                tid=evidence.task_id,epoch=evidence.execution_epoch),
            receipt=dict(id=evidence.evidence_id,tid=evidence.task_id,epoch=evidence.execution_epoch,payload=evidence.model_dump_json(),digest=evidence.digest()),
            audit=dict(tid=evidence.task_id,cid=conversation['id'],payload=json.dumps({
                'evidenceId':evidence.evidence_id,'authorityId':evidence.authority_id,'digest':evidence.digest()})))
        return evidence.evidence_id
