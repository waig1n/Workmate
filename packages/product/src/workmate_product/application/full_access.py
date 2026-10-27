"""Task-scoped permission upgrades authorized separately from ordinary approval."""
from workmate_product.application.sandbox_proof import sandbox_proof_valid


from workmate_contracts.controls import ResumePayload
from .commands import CommandRejected
from .task_capacity import TaskCapacityPolicy
from workmate_product.persistence.repositories import permission_upgrades as persistence
from workmate_product.persistence.repositories.control_admission import append_audit


async def accept_full_access(db,command,*,actor_user_id,authorize_resume,policy=TaskCapacityPolicy()):
    from .control_commands import _accept_resume
    if not ResumePayload.model_validate(command.payload).full_access:
        raise CommandRejected('Full Access requires an explicit upgrade request')
    return await _accept_resume(db,command,actor_user_id=actor_user_id,
        authorize_resume=authorize_resume,upgrade=stage_full_access,policy=policy)


async def stage_full_access(db,command,task,session,*,record):
    """Both validation and immutable grant write join the admission transaction."""
    if task['approval_profile'] == 'unattended':
        raise CommandRejected('Unattended tasks cannot enable Full Access')
    authority = session
    if session['owner_id'] is None:
        parked = await persistence.recovery_boundary(db, session)
        from workmate_product.persistence.recovery_provenance import valid_release
        if (not valid_release(parked) or parked['task_id'] != task['id'] or parked['session_id'] != session['id']
                or parked['source_epoch'] != session['execution_epoch']
                or parked['state_version'] != session['state_version'] or parked['runtime_seq'] != session['runtime_seq']
                or parked['reply_id'] != session['reply_id'] or parked['storage_node_id'] != session['storage_node_id']
                or parked['resume_epoch'] is not None or not parked['cleanup_receipt']):
            raise CommandRejected('Full Access requires a proven released boundary')
        authority = {**session, 'owner_id':parked['source_worker_id']}
    proof=await persistence.sandbox_attestation(db, id=task['id'], epoch=authority['execution_epoch'])
    spec=await persistence.execution_spec(db, id=task['id'])
    # The lease query remains last and short-circuited after all proof checks.
    if (not proof or not spec or proof['session_id']!=session['id']
            or proof['worker_id']!=authority['owner_id'] or proof['execution_epoch']!=authority['execution_epoch']
            or not sandbox_proof_valid(proof)
            or spec['session_id']!=session['id'] or spec['payload'].get('approval_profile')!=task['approval_profile']
            or (session['owner_id'] is not None and not await persistence.lease_is_current(db, session))):
        raise CommandRejected('Full Access requires current proven sandbox isolation')
    if not record:
        return
    version=command.payload['decision_version']
    previous=await persistence.previous_upgrade(db, id=task['id'])
    actor=await persistence.conversation_actor(db, command)
    await persistence.insert_upgrade(db, actor, command, authority, task, version)
    actor=await persistence.conversation_actor(db, command)
    modes={'standard':'accept_edits','strict':'default','read_only':'explore','full_access':'bypass'}
    await append_audit(db,command,'permission_mode_changed',{'from':'bypass' if previous else modes[task['approval_profile']],
        'to':'bypass','reason':'full_access_enabled','actorUserId':actor,'decisionVersion':version})
    await persistence.append_security_audit(db, actor, command, proof, task, version)


async def effective_permission_profile(db,task,session,initial_spec,*,allow_pending_recovery=False):
    """Read only this Task's independently authorized version under caller locks."""
    upgrade=await persistence.latest_upgrade(db, id=task.id)
    if upgrade is None:
        return initial_spec.approval_profile,None
    if initial_spec.approval_profile == 'unattended':
        raise CommandRejected('Unattended tasks cannot enable Full Access')
    command=await persistence.command(db, id=upgrade['command_id'])
    proof=await persistence.sandbox_attestation(db, id=task.id, epoch=upgrade['execution_epoch'])
    lineage = await persistence.recovery_lineage(db,task_id=task.id,session_id=session.id,
        node_id=session.storage_node_id,source_worker=upgrade['worker_id'],source_epoch=upgrade['execution_epoch'],
        worker_id=session.owner_id,execution_epoch=session.execution_epoch,allow_pending_last=allow_pending_recovery)
    if (not command or not sandbox_proof_valid(proof) or upgrade['initial_profile']!=initial_spec.approval_profile
            or session.status=='preparing' or upgrade['state_version']>session.state_version
            or command['reply_id']!=session.reply_id or command['expected_epoch']!=upgrade['execution_epoch']
            or upgrade['session_id']!=session.id or upgrade['actor_user_id']!=initial_spec.actor_user_id
            or not lineage
            or proof['session_id']!=session.id or proof['worker_id']!=upgrade['worker_id']
            or proof['execution_epoch']!=upgrade['execution_epoch']
            or command['kind']!='ResumeInteraction' or command['status'] not in {'accepted','claimed','completed'}
            or command['task_id']!=task.id or command['session_id']!=session.id
            or command['actor_user_id']!=initial_spec.actor_user_id
            or command['payload'].get('full_access') is not True
            or command['payload'].get('decision_version')!=upgrade['permission_version']):
        raise CommandRejected('Task permission upgrade evidence is inconsistent')
    return 'full_access',upgrade['permission_version']
