"""Hide closed or failed conversations without claiming resource cleanup."""
from datetime import datetime, timezone
from sqlalchemy import select
from .execution_context import _fresh_identity_map
from .platform_types import PlatformToolError
from .security_audit import SecurityAuditService
from ..persistence.models import (
    Agent, Conversation, ConversationRetirement, Task, User, RuntimeSession, RuntimeCommand,
    ACTIVE_TASK_STATUSES,
)


@_fresh_identity_map
async def request_retirement(db, *, actor_id, conversation_id):
    agent_id = await db.scalar(select(Conversation.agent_id).where(Conversation.id == conversation_id))
    await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update(read=True))
    conv = await db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
    actor = await db.get(User, actor_id)
    if conv is None or conv.user_id != actor_id or actor is None or actor.status != 'active':
        raise PlatformToolError(404, 'Conversation not found')
    existing = await db.get(ConversationRetirement, conversation_id)
    if existing:
        return conversation_id
    active = await db.scalar(select(Task.id).where(Task.conversation_id == conversation_id,
        Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
    sessions = list(await db.scalars(select(RuntimeSession).where(
        RuntimeSession.conversation_id == conversation_id).with_for_update()))
    session = next((item for item in sessions if item.superseded_at is None), None)
    # A missing current binding must not hide historical resource obligations.
    session = session or next(iter(sessions), None)
    # Failed Tasks may retain gates/commands after quarantine. Hiding their
    # history must not release those gates or certify the Workspace as stopped.
    pending_task_ids = list(await db.scalars(select(RuntimeCommand.task_id).where(
        RuntimeCommand.conversation_id == conversation_id, RuntimeCommand.status == 'accepted')))
    blocking_task_ids = set(pending_task_ids)
    if conv.active_product_task_id:
        blocking_task_ids.add(conv.active_product_task_id)
    blocking_task_ids.update(item.active_task_id for item in sessions if item.active_task_id)
    failed_task_ids = set(await db.scalars(select(Task.id).where(
        Task.conversation_id == conversation_id, Task.status == 'failed')))
    if active or not blocking_task_ids.issubset(failed_task_ids):
        raise PlatformToolError(409, '会话仍有未结束的任务，请等待任务结束或停止后再删除')
    now = datetime.now(timezone.utc)
    conv.deleted_at = now
    db.add(ConversationRetirement(conversation_id=conv.id,
        session_id=session.id if session else None, storage_node_id=session.storage_node_id if session else None,
        execution_epoch=session.execution_epoch if session else 0, requested_by=actor_id,
        completed_at=None if session else now, completed_by=None if session else 'product'))
    SecurityAuditService(db).record('conversation.delete', actor_user_id=actor_id,
        scope_id=(await db.get(Agent, agent_id)).scope_id,
        metadata={'conversation_id':conv.id, 'cleanup_pending':session is not None,
                  'execution_cleanup_deferred':bool(blocking_task_ids)})
    await db.flush()
    return conv.id
