"""Worker validates the immutable upgrade accepted by the separate Product service."""
from workmate_product.application.sandbox_proof import sandbox_proof_sql


async def current_sandbox_matches(db, session, task):
    return await db.fetchval(f'''SELECT 1 FROM runtime_sandbox_attestations p
        WHERE p.task_id=$1 AND p.session_id=$2 AND p.worker_id=$3 AND p.execution_epoch=$4
        AND {sandbox_proof_sql('p')}''',task['id'],session['id'],session['owner_id'],session['execution_epoch']) == 1


async def cold_permission_proof_matches(db, session, task):
    requires_proof = task['approval_profile'] == 'full_access' or await db.fetchval(
        'SELECT 1 FROM runtime_permission_upgrades WHERE task_id=$1 LIMIT 1',task['id'])
    return not requires_proof or await current_sandbox_matches(db,session,task)


async def full_access_matches(db,command,payload,session,task, *, require_current=False):
    if task['approval_profile'] == 'unattended':
        return False
    from .suspension import read_recovery_bridge
    from .postgres import ExecutionOwner
    bridge = await read_recovery_bridge(db,ExecutionOwner(session['id'],task['id'],session['owner_id'],session['execution_epoch']),_pending_claim=not require_current)
    source_worker = bridge['source_worker_id'] if bridge else session['owner_id']
    source_epoch = bridge['source_epoch'] if bridge else session['execution_epoch']
    if require_current and bridge:
        current = await db.fetchval(f'''SELECT 1 FROM runtime_sandbox_attestations p
            WHERE p.task_id=$1 AND p.session_id=$2 AND p.worker_id=$3 AND p.execution_epoch=$4
            AND {sandbox_proof_sql('p')}''',task['id'],session['id'],session['owner_id'],session['execution_epoch'])
        if current != 1:
            return False
    return await db.fetchval(f'''SELECT 1 FROM runtime_permission_upgrades u
        JOIN runtime_sandbox_attestations p ON p.task_id=u.task_id
        JOIN runtime_execution_specs spec ON spec.task_id=u.task_id
        WHERE u.task_id=$1 AND u.session_id=$2 AND u.command_id=$3 AND u.permission_version=$4
            AND u.state_version=$9 AND u.worker_id=$5 AND u.execution_epoch=$6 AND u.actor_user_id=$7
            AND p.session_id=u.session_id AND p.worker_id=u.worker_id AND p.execution_epoch=u.execution_epoch
            AND {sandbox_proof_sql('p')}
            AND u.initial_profile=$8 AND spec.payload->>'approval_profile'=u.initial_profile
    ''',task['id'],session['id'],command['id'],payload.decision_version,source_worker,
        source_epoch,command['actor_user_id'],task['approval_profile'],payload.state_version)==1
