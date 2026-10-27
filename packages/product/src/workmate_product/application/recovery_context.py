"""Freeze server-owned recovery lineage alongside the initial execution spec."""
from sqlalchemy import select
from workmate_contracts import snapshot_digest
from workmate_product.persistence.recovery_forks import RecoveryFork


async def recovery_spec_fields(db, conversation, session, *, task_id=None):
    from .execution_context import ExecutionContextRejected
    fork = await db.scalar(select(RecoveryFork).where(RecoveryFork.target_session_id == session.id))
    if fork is None:
        return {}
    if (session.superseded_at is not None or fork.conversation_id != conversation.id or fork.actor_user_id != conversation.user_id
            or fork.target_node_id != session.storage_node_id):
        raise ExecutionContextRejected('Recovery lineage differs from execution admission')
    return {'recovery_fork_id': fork.id, 'recovery_manifest_sha256': snapshot_digest(fork.manifest),
        'recovery_context_text':fork.context_text if task_id==fork.target_task_id else None}
