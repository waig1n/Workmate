"""Product Task admission to the sole durable Worker execution chain.

Only database facts and validated deployment descriptors are used here. No App
HTTP, workspace reads, tool dispatch or process-local queue participates.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from workmate_contracts import Command
from workmate_product.application.commands import accept_start
from workmate_product.persistence.repositories.start_admission import lock_admission
from workmate_product.application.execution_context import SelectedModel, record_execution_spec
from workmate_product.application.authorization import AccessDecisionService
from workmate_product.persistence.models import RuntimeCommand, RuntimeSession

from ..config_file import models_file_config
from workmate_product.persistence.models import (
    Agent,
    ApprovalProfile,
    Conversation,
    Task,
    TaskMessage,
    TaskStatus,
    User,
)
from .runtime_errors import ModelConfigError
from .task_admission import task_admission
from .usage import snapshot_usage_pricing, snapshot_usage_scopes

_MODEL_INPUT_TYPES = {'text':'text/plain', 'image':'image/*', 'audio':'audio/*', 'video':'video/*'}


def selected_model(profile_name):
    """Freeze public model capabilities and secret *names*, never secret values."""
    catalog = models_file_config()
    name = profile_name or catalog.default
    profile = catalog.profiles.get(name)
    if profile is None:
        raise ModelConfigError(f'Chat model profile is not configured: {name!r}')
    media = catalog.media_profiles[profile.media_profile]
    inputs = [_MODEL_INPUT_TYPES[value] for value in media.model_inputs]
    if media.pdf_strategy == 'native':
        inputs.append('application/pdf')
    return SelectedModel(profile_name=name,
        **profile.model_dump(exclude={'dimensions'}),
        model_input_types=tuple(inputs), pdf_strategy=media.pdf_strategy)


class TaskAdmission:
    def __init__(self, sessions, *, storage_node_id, knowledge_enabled=False, node_directory=None):
        if (not isinstance(storage_node_id,str) or not storage_node_id.strip()
                or len(storage_node_id)>128):
            raise ValueError('An explicit trusted storage node binding is required')
        self.sessions = sessions
        self.storage_node_id = storage_node_id
        self.knowledge_enabled = knowledge_enabled
        self.node_directory = node_directory

    async def _authorize(self, db, actor_id, agent_id):
        # HTTP dependencies may have cached RBAC rows before waiting on the
        # admission lock. Recheck with a fresh identity map in this same locked
        # transaction so a committed revocation cannot survive in ORM caches.
        await db.flush()
        async with AsyncSession(bind=await db.connection(),expire_on_commit=False,
                                join_transaction_mode='rollback_only') as fresh:
            await fresh.begin()
            actor = await fresh.get(User, actor_id)
            access = AccessDecisionService(fresh, knowledge_enabled=self.knowledge_enabled)
            return (actor is not None and actor.status == 'active'
                and await access.can_use_agent(actor_id,agent_id)
                and not await access.unavailable_capability_ids(actor_id,agent_id))

    async def _lock_conversation(self, db, conversation_id):
        if db.bind.dialect.name != 'postgresql':
            raise ValueError('Task admission requires PostgreSQL')
        await lock_admission(db)
        agent_id = await db.scalar(select(Conversation.agent_id).where(Conversation.id==conversation_id))
        agent = await db.scalar(select(Agent).where(Agent.id==agent_id).with_for_update(read=True)
            .execution_options(populate_existing=True))
        conversation = await db.scalar(select(Conversation).where(Conversation.id==conversation_id)
            .with_for_update().execution_options(populate_existing=True))
        if (agent is None or agent.deleted_at is not None or conversation is None
                or conversation.deleted_at is not None or conversation.agent_id != agent.id):
            raise HTTPException(404, detail='Conversation or Agent is unavailable')
        return conversation

    async def _storage_node(self, db, conversation, *, bind=False):
        from workmate_product.persistence.repositories.recovery_forks import fork_for
        from workmate_product.application.node_placement import select_storage_node, NodePlacementUnavailable
        fork = await fork_for(db, conversation.id)
        if fork is not None:
            if self.node_directory is None or not self.node_directory.available(fork.target_node_id):
                raise NodePlacementUnavailable('Recovery storage node is unavailable')
            return fork.target_node_id
        if self.node_directory is None:
            return self.storage_node_id
        return await select_storage_node(db, conversation.user_id, conversation.agent_id,
            self.node_directory, bind=bind)

    async def create_task(self, db, user, conversation, message, title=None, *,
                          task_id=None, attachments=None, approval_profile='standard'):
        actor_id, conversation_id = user.id, conversation.id
        profile = ApprovalProfile(approval_profile).value
        if profile == 'unattended':
            raise HTTPException(422, detail='Unattended execution is reserved for schedules')
        async with task_admission(db, actor_id):
            task, queued = await self.stage_task(db, user, conversation, message, title,
                task_id=task_id, attachments=attachments, approval_profile=profile)
            await db.commit()
        await db.refresh(task)
        return task, queued

    async def stage_task(self, db, user, conversation, message, title=None, *,
                         task_id=None, attachments=None, approval_profile='standard', occurrence=None, channel_inbox=None):
        """Stage Task and Start in the caller transaction; no commit or rollback."""
        actor_id, conversation_id = user.id, conversation.id
        profile = ApprovalProfile(approval_profile).value
        if channel_inbox is not None and (occurrence is not None or profile != 'standard'
                or channel_inbox.user_id != actor_id or channel_inbox.conversation_id != conversation_id
                or channel_inbox.task_id is not None):
            raise ValueError('Channel admission source is invalid')
        if (profile == 'unattended') != (occurrence is not None):
            raise ValueError('Scheduled execution requires an occurrence')
        conversation = await self._lock_conversation(db,conversation_id)
        if conversation.user_id != actor_id:
            raise HTTPException(404, detail='Conversation not found')
        from workmate_product.application.execution_quarantine import conversation_quarantined,READ_ONLY_REASON
        if await conversation_quarantined(db,conversation):
            raise HTTPException(409,detail=READ_ONLY_REASON)
        if not await self._authorize(db,actor_id,conversation.agent_id):
            raise HTTPException(403, detail='Current Agent access is denied')
        if self.node_directory is not None:
            from workmate_product.application.node_placement import select_storage_node, NodePlacementUnavailable
            try:
                await self._storage_node(db, conversation)
            except NodePlacementUnavailable as error:
                raise HTTPException(503, detail=str(error), headers={'Retry-After': '5'}) from error
        # Validate even queued submissions. Admission will freeze the then
        # current descriptor again; model switching is forbidden while queued.
        model = selected_model(conversation.model_profile)
        conversation.model_profile = model.profile_name
        task = Task(id=task_id or str(uuid.uuid4()),conversation_id=conversation.id,
            title=title or message[:80],status=TaskStatus.CREATED,approval_profile=profile,
            origin='schedule' if occurrence else ('channel' if channel_inbox is not None else 'user'),
            channel_inbox_id=channel_inbox.id if channel_inbox is not None else None,
            schedule_occurrence_id=occurrence.id if occurrence else None)
        db.add(task)
        db.add(TaskMessage(id=str(uuid.uuid4()),task_id=task.id,conversation_id=conversation.id,
            role='user',content={'text':message,**({'attachments':attachments} if attachments else {})}))
        await db.flush()
        if occurrence is not None:
            occurrence.task_id = task.id
            occurrence.conversation_id = conversation.id
            await db.flush()
        if channel_inbox is not None:
            channel_inbox.task_id = task.id
            await db.flush()
        await snapshot_usage_scopes(db,task,actor_id)
        accepted = await self.admit_next(db,conversation.id)
        queued = accepted is None or accepted.product_task_id != task.id
        return task, queued

    async def admit_next(self, db, conversation_id):
        """Called inside the projection service transaction; never commits."""
        conversation = await self._lock_conversation(db,conversation_id)
        if conversation.active_product_task_id is not None:
            return None
        task = await db.scalar(select(Task).where(Task.conversation_id==conversation.id,
            Task.status==TaskStatus.CREATED,
            ~exists(select(RuntimeCommand.id).where(RuntimeCommand.task_id==Task.id,
                RuntimeCommand.kind=='StartTask'))).order_by(Task.created_at,Task.id).limit(1)
            .with_for_update().execution_options(populate_existing=True))
        if task is None:
            return None
        session = await db.scalar(select(RuntimeSession).where(
            RuntimeSession.conversation_id==conversation.id, RuntimeSession.superseded_at.is_(None)).with_for_update()
            .execution_options(populate_existing=True))
        if session is not None and (session.active_task_id is not None or session.status not in
                {'accepted','completed','failed','cancelled','interrupted'}):
            return None
        if not await self._authorize(db,conversation.user_id,conversation.agent_id):
            raise HTTPException(403, detail='Current Agent access is denied')
        model = selected_model(conversation.model_profile)
        conversation.model_profile = model.profile_name
        snapshot_usage_pricing(task,conversation)
        await db.flush()
        command = Command(command_id=str(uuid.uuid4()),kind='StartTask',
            conversation_id=conversation.id,product_task_id=task.id,
            runtime_session_id=session.id if session else str(uuid.uuid4()),
            expected_epoch=session.execution_epoch if session else 0)
        node = session.storage_node_id if session else self.storage_node_id
        if self.node_directory is not None:
            from workmate_product.application.node_placement import select_storage_node, NodePlacementUnavailable
            try:
                node = await self._storage_node(db, conversation, bind=True)
            except NodePlacementUnavailable:
                return None
        accepted = await accept_start(db,command,actor_user_id=conversation.user_id,
            storage_node_id=node,
            authorize_agent=self._authorize)
        await record_execution_spec(db,task_id=task.id,session_id=command.runtime_session_id,
            actor_user_id=conversation.user_id,selected_model=model,knowledge_enabled=self.knowledge_enabled)
        await db.flush()
        return accepted
