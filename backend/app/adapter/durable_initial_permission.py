"""Project initial bypass activation from immutable proof, never from intent."""
from workmate_product.application.sandbox_proof import sandbox_proof_valid

from sqlalchemy import text
from workmate_product.application.execution_context import _stored_spec,ExecutionContextRejected
from workmate_product.persistence.models import RuntimeExecutionSpec
from workmate_product.projections.store import ProjectionConflict
from ..services.security_audit import SecurityAuditService


async def project_initial_permission(db,event,task):
    if task.approval_profile!='full_access':
        return
    proof=(await db.execute(text('SELECT * FROM runtime_sandbox_attestations WHERE task_id=:id'),
        {'id':task.id})).mappings().first()
    command=(await db.execute(text("SELECT * FROM runtime_commands WHERE task_id=:id AND kind='StartTask'"),
        {'id':task.id})).mappings().first()
    try:
        spec=_stored_spec(await db.get(RuntimeExecutionSpec,task.id,populate_existing=True))
    except (ExecutionContextRejected,ValueError,TypeError) as error:
        raise ProjectionConflict('Initial permission activation lacks its admission spec') from error
    actor=await db.scalar(text('SELECT user_id FROM conversations WHERE id=:id'),{'id':event.conversation_id})
    if (event.event_type!='REPLY_START' or not event.reply_id or not proof or not command
            or spec.approval_profile!='full_access' or spec.task_id!=task.id
            or spec.session_id!=event.runtime_session_id or spec.conversation_id!=event.conversation_id
            or spec.actor_user_id!=actor or proof['task_id']!=task.id
            or proof['session_id']!=event.runtime_session_id or proof['execution_epoch']!=event.execution_epoch
            or not sandbox_proof_valid(proof)
            or command['session_id']!=event.runtime_session_id or command['conversation_id']!=event.conversation_id
            or command['actor_user_id']!=actor or command['protocol_version']!=1
            or command['status'] not in {'claimed','completed','indeterminate'} or command['claimed_at'] is None
            or command['claimed_by']!=proof['worker_id'] or command['claimed_epoch']!=event.execution_epoch
            or command['expected_epoch']!=event.execution_epoch-1
            or proof['created_at']>event.occurred_at):
        raise ProjectionConflict('Initial permission activation lacks matching Task sandbox proof')
    from .durable_projection import _audit
    await _audit(db,event,'permission_mode_changed',{'from':'default','to':'bypass',
        'reason':'task_profile_full_access','actorUserId':actor,
        'agentToolPolicy':spec.tool_policy.model_dump(mode='json')})
    SecurityAuditService(db).record('task.full_access_enabled',actor_user_id=actor,
        metadata={'task_id':task.id,'conversation_id':event.conversation_id,'source':'task_profile',
            'workspace_isolation':proof['isolation'],'worker_id':proof['worker_id'],
            'execution_epoch':event.execution_epoch})
    await db.flush()
