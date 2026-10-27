"""Trusted policy Stop for expired parked approvals; never fabricates cleanup."""
import uuid

from workmate_contracts import Command
from .control_commands import _transaction, _accept_stop_locked
from .task_capacity import TaskCapacityPolicy
from ..persistence.repositories import control_admission as repository


async def expire_approval(db, conversation_id, *, policy=TaskCapacityPolicy()):
    _transaction(db)
    candidate = await repository.conversation_agent(db, id=conversation_id)
    if candidate is None:
        return False
    await repository.lock_agent(db, id=candidate['agent_id'])
    conversation = await repository.lock_conversation(db, id=conversation_id)
    if (not conversation or conversation['agent_id'] != candidate['agent_id']
            or not conversation['active_product_task_id']):
        return False
    task = await repository.lock_task(db, id=conversation['active_product_task_id'])
    if not task or task['status'] != 'awaiting_permission':
        return False
    execution_hint = await repository.task_execution(db, id=task['id'])
    if not execution_hint:
        return False
    session = await repository.lock_session(db, id=execution_hint['session_id'])
    execution = await repository.lock_execution(db, id=task['id'])
    saved = await repository.lock_projection(db, id=task['id'])
    if (not saved or not session or not execution
            or session['status'] != 'awaiting_interaction' or execution['status'] != 'awaiting_interaction'
            or saved['state'].get('interaction_boundary_id') != execution['snapshot_id']):
        return False
    state = saved['state']
    # Accepted/claimed decisions belong to execution, not abandoned user input.
    if await repository.outstanding_resume(db, id=task['id']):
        return False
    for pending in state.get('pending_interactions', []):
        if pending['kind'] != 'permission' or pending.get('awaiting_resume'):
            continue
        version = pending['request_version']
        if not await policy.expired(db, task['id'], version):
            continue
        command = Command(command_id=str(uuid.uuid5(uuid.NAMESPACE_URL, 'approval-expiry:'+task['id'])),
            kind='StopTask', conversation_id=conversation_id, runtime_session_id=saved['session_id'],
            product_task_id=task['id'], payload={'reason': 'approval_expired'})
        await _accept_stop_locked(db, command, conversation['user_id'], conversation, expiry_version=version)
        return True
    return False
