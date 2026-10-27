"""Worker use cases bridging current Product authority and execution outcomes.

Failure reporting owns one short transaction. Resume authorization joins the
transport's already locked connection and never commits it independently.
"""
from workmate_product.application.execution_context import read_execution_context, ExecutionContextRejected
from workmate_product.application.commands import CommandRejected
from workmate_product.application.execution_quarantine import quarantine_execution, QuarantineConflict
from workmate_product.persistence.repositories.execution_quarantine import read_failure_binding
from workmate_runtime.transport.controls import read_resume_binding


async def report_worker_failure(sessions, *, command_id, session_id, task_id,
                                worker_id, storage_node_id, error_type, cause_type=None):
    async with sessions.begin() as db:
        # Resolve only this failing process's existing binding. Never substitute
        # a new owner's identity after a takeover or invent a replacement epoch.
        row = await read_failure_binding(db, command_id=command_id, session_id=session_id,
            task_id=task_id, worker_id=worker_id, storage_node_id=storage_node_id)
        if row is None:
            raise QuarantineConflict('Failed execution no longer has this Worker binding')
        return await quarantine_execution(db, conversation_id=row['conversation_id'],
            task_id=task_id, session_id=session_id, worker_id=worker_id,
            execution_epoch=row['execution_epoch'], storage_node_id=storage_node_id,
            error_type=error_type, cause_type=cause_type)


async def authorize_resume(connection,actor_id,agent_id,task_id,*,storage_node_id,knowledge_enabled):
    # ResumeStore already holds the Product-compatible identity locks.
    # Join that exact connection instead of deadlocking against them or
    # checking RBAC in a disconnected transaction.
    row=await read_resume_binding(connection,task_id)
    if row is None:
        return False
    async with connection.product_session() as db:
        try:
            context=await read_execution_context(db,session_id=row['id'],task_id=task_id,
                worker_id=row['owner_id'],execution_epoch=row['execution_epoch'],
                storage_node_id=storage_node_id,knowledge_enabled=knowledge_enabled,allow_pending_recovery=True)
            return context.actor_user_id==actor_id and context.agent_id==agent_id
        except (ExecutionContextRejected, CommandRejected):
            return False
