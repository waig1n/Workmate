"""Conversation model selection is a Product fact consumed by the next Task."""
from sqlalchemy import select
from .execution_context import _fresh_identity_map
from .authorization import AccessDecisionService
from .platform_types import PlatformToolError
from .security_audit import SecurityAuditService
from ..persistence.models import Agent, Conversation, Task, User, RuntimeSession, ACTIVE_TASK_STATUSES


@_fresh_identity_map
async def select_conversation_model(db, *, actor_id, conversation_id, profile_name,
                                    knowledge_enabled=False):
    # Match native admission: Agent -> Conversation -> Runtime Session. Discovery
    # only selects the lock target; ownership and current authority are rechecked.
    agent_id = await db.scalar(select(Conversation.agent_id).where(Conversation.id == conversation_id))
    await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update(read=True))
    conv = await db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
    user = await db.get(User, actor_id)
    if conv is None or conv.deleted_at is not None or conv.user_id != actor_id or conv.agent_id != agent_id:
        raise PlatformToolError(404, 'Conversation not found')
    access = AccessDecisionService(db, knowledge_enabled=knowledge_enabled)
    if user is None or user.status != 'active' or not await access.can_use_agent(actor_id, agent_id):
        raise PlatformToolError(403, '你已失去该智能体的使用权限，历史内容仍可查看')
    unavailable = await access.unavailable_capability_ids(actor_id, agent_id)
    if unavailable:
        raise PlatformToolError(409, {'code':'unavailable_capabilities', 'capability_ids':unavailable,
                                    'message':'智能体存在当前不可用的能力，请先解绑后再试'})
    active = await db.scalar(select(Task.id).where(Task.conversation_id == conversation_id,
        Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
    session = await db.scalar(select(RuntimeSession).where(
        RuntimeSession.conversation_id == conversation_id, RuntimeSession.superseded_at.is_(None)).with_for_update())
    if active or conv.active_product_task_id or (session and session.active_task_id):
        raise PlatformToolError(409, '任务运行中无法切换模型，请等待任务结束后再试')
    if conv.model_profile != profile_name:
        previous = conv.model_profile
        conv.model_profile = profile_name
        SecurityAuditService(db).record('conversation.model_change', actor_user_id=actor_id,
            scope_id=(await db.get(Agent, agent_id)).scope_id,
            metadata={'conversation_id':conversation_id, 'previous_profile':previous, 'model_profile':profile_name})
        await db.flush()
    return conv.id
