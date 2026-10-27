"""Trusted Product admission facts, independent of Web and engine implementations.

These short caller-owned transactions never execute tools. A returned context is
assembly input, not a substitute for rechecking authority at each execution gate.
Command payloads and legacy App credential identifiers are never read here.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from functools import wraps
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .authorization import AccessDecisionService
from ..config.knowledge import KnowledgeConfig
from ..persistence.models import (
    Agent, Conversation, RuntimeCommand, RuntimeExecutionSpec,
    RuntimeSession, RuntimeTaskExecution, Task, TaskMessage, User,
)


class ExecutionContextRejected(ValueError):
    """Trusted identity, current authority or persisted admission facts are invalid."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class SelectedModel(_Frozen):
    """Deployment descriptor only. Resolve named environment variables in Worker.

    Product admission constructs this from validated deployment configuration;
    it is not a browser/API request schema. No credential value or resolved URL
    is accepted. Changes to this descriptor require a new Task.
    """
    profile_name: str = Field(min_length=1,max_length=64)
    model_type: Literal['openai','dashscope']
    model: str = Field(min_length=1,max_length=255)
    api_key_env: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    base_url_env: str | None = Field(default=None,pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    media_profile: str = 'text'
    model_input_types: tuple[str,...] = ('text/plain',)
    pdf_strategy: Literal['extract_text','native'] = 'extract_text'
    supports_thinking: bool = False
    context_size: int = Field(default=32768,gt=0)
    max_output_tokens: int = Field(default=8192,gt=0)


class ToolPolicy(_Frozen):
    denied_groups: tuple[str,...] = ()
    denied_tools: tuple[str,...] = ()


class SkillArtifactReference(_Frozen):
    capability_id: str
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class MCPConfigurationReference(_Frozen):
    capability_id: str
    secret_version: str = Field(pattern=r'^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')


class ExecutionSpec(_Frozen):
    schema_version: Literal[1] = 1
    origin: Literal['user','schedule','channel'] = 'user'
    channel_inbox_id: str | None = Field(default=None, min_length=1)
    schedule_occurrence_id: str | None = Field(default=None, min_length=1)
    recovery_fork_id: str | None = None
    recovery_context_text: str | None = Field(default=None, max_length=262144)
    recovery_manifest_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    task_id: str
    session_id: str
    conversation_id: str
    actor_user_id: str
    agent_id: str
    approval_profile: Literal['standard','strict','read_only','full_access','unattended']
    tool_policy: ToolPolicy
    platform_tool_profile: str
    capability_ids: tuple[str,...]
    knowledge_base_ids: tuple[str,...]
    selected_model: SelectedModel
    skill_artifacts: tuple[SkillArtifactReference,...] = ()
    mcp_configurations: tuple[MCPConfigurationReference,...] = ()

    @model_validator(mode='after')
    def validate_schedule_source(self):
        if ((self.origin == 'channel') != (self.channel_inbox_id is not None)
                or (self.origin == 'channel' and self.approval_profile != 'standard')):
            raise ValueError('Channel execution requires an inbox and standard policy')
        scheduled = self.origin == 'schedule'
        if (scheduled != (self.schedule_occurrence_id is not None)
                or scheduled != (self.approval_profile == 'unattended')):
            raise ValueError('Schedule execution requires an occurrence and unattended policy')
        return self


@dataclass(frozen=True)
class ProductInputMessage:
    id: str
    content: dict


@dataclass(frozen=True)
class CapabilityReference:
    id: str
    capability_type: str
    scope_id: str
    name: str


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    session_id: str
    conversation_id: str
    actor_user_id: str
    agent_id: str
    agent_scope_id: str
    system_prompt: str
    platform_tool_profile: str
    current_tool_policy: ToolPolicy
    effective_tool_policy: ToolPolicy
    current_model_profile: str | None
    knowledge_config: KnowledgeConfig
    capabilities: tuple[CapabilityReference,...]
    input_messages: tuple[ProductInputMessage,...]
    initial_spec: ExecutionSpec
    permission_version: str | None = None
    effective_approval_profile: str | None = None


def _policy(value):
    parsed = ToolPolicy.model_validate(value or {})
    return ToolPolicy(denied_groups=tuple(sorted(set(parsed.denied_groups))),
                      denied_tools=tuple(sorted(set(parsed.denied_tools))))


def _digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _transaction(db):
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise ExecutionContextRejected('A caller-owned PostgreSQL transaction is required')


def _fresh_identity_map(function):
    @wraps(function)
    async def joined(db, *args, **kwargs):
        _transaction(db)
        await db.flush()
        # RBAC uses get() for memberships/scopes. Reusing caller-cached ORM
        # objects could hide committed revocation. Join the same connection,
        # locks and transaction with a fresh identity map; close in rollback_only
        # mode does not commit or roll back the caller's external transaction.
        async with AsyncSession(bind=await db.connection(),expire_on_commit=False,
                                join_transaction_mode='rollback_only', info=dict(db.info)) as fresh:
            await fresh.begin()
            return await function(fresh,*args,**kwargs)
    return joined


async def _lock(db, model, identity, *, read=False):
    return await db.scalar(select(model).where(model.id==identity).with_for_update(read=read)
                           .execution_options(populate_existing=True))


async def _facts(db, task_id, session_id, *, knowledge_enabled):
    _transaction(db)
    # Discovery never grants authority: all relationships are rechecked after
    # acquiring locks in Agent -> Conversation -> Task -> Session order.
    candidate = (await db.execute(select(Task.conversation_id,Conversation.agent_id)
        .join(Conversation,Conversation.id==Task.conversation_id)
        .where(Task.id==task_id))).first()
    if candidate is None:
        raise ExecutionContextRejected('Task is unavailable')
    agent = await _lock(db,Agent,candidate.agent_id,read=True)
    conversation = await _lock(db,Conversation,candidate.conversation_id)
    task = await _lock(db,Task,task_id)
    session = await _lock(db,RuntimeSession,session_id)
    if (agent is None or conversation is None or task is None or session is None
            or agent.deleted_at is not None or conversation.deleted_at is not None or conversation.agent_id != agent.id
            or session.superseded_at is not None
            or task.conversation_id != conversation.id or session.conversation_id != conversation.id
            or conversation.active_product_task_id != task.id):
        raise ExecutionContextRejected('Execution Product identity or active gate is unavailable')
    execution = await db.scalar(select(RuntimeTaskExecution).where(
        RuntimeTaskExecution.task_id==task_id).with_for_update().execution_options(populate_existing=True))
    if execution is None or execution.session_id != session.id:
        raise ExecutionContextRejected('Task execution binding is unavailable')
    user = await db.get(User,conversation.user_id,populate_existing=True)
    access = AccessDecisionService(db,knowledge_enabled=knowledge_enabled)
    if user is None or user.status != 'active' or not await access.can_use_agent(user.id,agent.id):
        raise ExecutionContextRejected('Current Agent access is denied')
    if await access.unavailable_capability_ids(user.id,agent.id):
        raise ExecutionContextRejected('Current capability or knowledge binding is unavailable')
    capabilities = await access.resolve_accessible_capabilities(user.id,agent.id)
    return agent,conversation,task,session,execution,capabilities


def _stored_spec(row):
    if row is None:
        raise ExecutionContextRejected('Initial execution spec is missing')
    if row.schema_version != 1 or _digest(row.payload) != row.digest:
        raise ExecutionContextRejected('Initial execution spec integrity is invalid')
    try:
        spec = ExecutionSpec.model_validate(row.payload)
    except ValueError as error:
        raise ExecutionContextRejected('Initial execution spec format is invalid') from error
    if spec.task_id != row.task_id or spec.session_id != row.session_id:
        raise ExecutionContextRejected('Initial execution spec binding is invalid')
    return spec


async def _schedule_source(db, task, conversation):
    """Check the accepted occurrence, never the mutable current Schedule.

    An owner may pause, edit or delete a plan after admission; those operations
    cannot rewrite authority or input of an already accepted execution.
    """
    source = {'origin': task.origin, 'schedule_occurrence_id': task.schedule_occurrence_id,
              'channel_inbox_id': task.channel_inbox_id}
    if task.origin == 'channel':
        from ..persistence.models import ChannelInbox
        inbox = await db.get(ChannelInbox, task.channel_inbox_id, populate_existing=True)
        if (inbox is None or task.schedule_occurrence_id is not None
                or task.approval_profile != 'standard'
                or inbox.task_id != task.id or inbox.conversation_id != conversation.id
                or inbox.user_id != conversation.user_id or task.conversation_id != conversation.id):
            raise ExecutionContextRejected('Channel source identity is invalid')
        return source
    if task.channel_inbox_id is not None:
        raise ExecutionContextRejected('Non-channel task cannot carry channel source')
    if task.origin == 'user':
        if task.schedule_occurrence_id is not None or task.approval_profile == 'unattended':
            raise ExecutionContextRejected('Schedule source is invalid for a user task')
        return source
    if (task.origin != 'schedule' or not task.schedule_occurrence_id
            or task.approval_profile != 'unattended'):
        raise ExecutionContextRejected('Schedule source or unattended policy is invalid')
    from ..persistence.models import ScheduleOccurrence
    occurrence = await db.get(ScheduleOccurrence, task.schedule_occurrence_id, populate_existing=True)
    snapshot = occurrence.snapshot if occurrence is not None else None
    if (occurrence is None or not isinstance(snapshot, dict)
            or occurrence.outcome != 'task_created'
            or occurrence.task_id != task.id or occurrence.conversation_id != conversation.id
            or task.conversation_id != conversation.id
            or snapshot.get('owner_user_id') != conversation.user_id
            or snapshot.get('agent_id') != conversation.agent_id
            or type(snapshot.get('version')) is not int
            or snapshot['version'] != occurrence.schedule_version
            or snapshot.get('approval_profile') != 'unattended'
            or snapshot.get('model_profile') != conversation.model_profile
            or not isinstance(snapshot.get('prompt'), str) or not snapshot['prompt'].strip()):
        raise ExecutionContextRejected('Schedule occurrence identity or snapshot is invalid')
    messages = (await db.scalars(select(TaskMessage).where(
        TaskMessage.task_id == task.id, TaskMessage.conversation_id == conversation.id,
        TaskMessage.role == 'user'))).all()
    if (len(messages) != 1 or not isinstance(messages[0].content, dict)
            or messages[0].content.get('text') != snapshot['prompt']
            or messages[0].content.get('attachments')):
        raise ExecutionContextRejected('Schedule input differs from its accepted occurrence')
    return source


@_fresh_identity_map
async def record_execution_spec(db, *, task_id, session_id, actor_user_id,
                                selected_model: SelectedModel, knowledge_enabled=False):
    """Freeze admission facts after accept_start and before its transaction commits.

    Caller is trusted Product admission, with a deployment-selected descriptor,
    never raw command JSON. No commit, dispatch or secret lookup occurs here.
    """
    if not isinstance(selected_model,SelectedModel):
        raise ExecutionContextRejected('A validated deployment model descriptor is required')
    selected_model = SelectedModel.model_validate(selected_model.model_dump())
    agent,conversation,task,session,execution,capabilities = await _facts(
        db,task_id,session_id,knowledge_enabled=knowledge_enabled)
    source = await _schedule_source(db, task, conversation)
    command = await db.scalar(select(RuntimeCommand).where(RuntimeCommand.task_id==task_id,
        RuntimeCommand.kind=='StartTask').with_for_update().execution_options(populate_existing=True))
    if (task.status != 'created' or execution.status != 'accepted' or execution.execution_epoch != 0
            or session.active_task_id is not None
            or session.status not in {'accepted','completed','failed','cancelled','interrupted'}
            or command is None or command.status != 'accepted'
            or command.claimed_by is not None or command.claimed_epoch is not None):
        raise ExecutionContextRejected('Execution spec must be recorded before the Task is claimed')
    if (actor_user_id != conversation.user_id or command.actor_user_id != actor_user_id
            or command.session_id != session_id or command.conversation_id != conversation.id
            or command.expected_epoch != session.execution_epoch):
        raise ExecutionContextRejected('Execution admission owner or epoch is invalid')
    if conversation.model_profile != selected_model.profile_name:
        raise ExecutionContextRejected('Selected model differs from Product Conversation selection')
    from .recovery_context import recovery_spec_fields
    recovery = await recovery_spec_fields(db, conversation, session, task_id=task_id)
    existing = await db.get(RuntimeExecutionSpec,task_id,populate_existing=True)
    if existing is not None:
        spec = _stored_spec(existing)
        if (spec.session_id != session_id or spec.conversation_id != conversation.id
                or spec.origin != source['origin']
                or spec.schedule_occurrence_id != source['schedule_occurrence_id']
                or spec.channel_inbox_id != source['channel_inbox_id']
                or spec.selected_model != selected_model or spec.actor_user_id != actor_user_id
                or spec.agent_id != agent.id or spec.approval_profile != task.approval_profile
                or spec.platform_tool_profile != agent.platform_tool_profile
                or spec.recovery_fork_id != recovery.get('recovery_fork_id')
                or spec.recovery_manifest_sha256 != recovery.get('recovery_manifest_sha256')
            or spec.recovery_context_text != recovery.get('recovery_context_text')):
            raise ExecutionContextRejected('Initial execution spec conflicts with admission')
        return spec
    from .skill_repository import ready_skill_artifacts
    ready = await ready_skill_artifacts(db,capabilities)
    from ..mcp.repository import ready_mcp_revisions
    mcps = await ready_mcp_revisions(db,capabilities)
    spec = ExecutionSpec(**recovery,**source,task_id=task.id,session_id=session.id,conversation_id=conversation.id,
        actor_user_id=conversation.user_id,agent_id=agent.id,approval_profile=task.approval_profile,
        tool_policy=_policy(agent.tool_policy),platform_tool_profile=agent.platform_tool_profile,capability_ids=tuple(sorted(c.id for c in capabilities)),
        knowledge_base_ids=tuple(sorted(KnowledgeConfig.model_validate(agent.knowledge_config or {}).knowledge_base_ids)),
        selected_model=selected_model,skill_artifacts=tuple(
            SkillArtifactReference(capability_id=key,digest=value) for key,value in sorted(ready.items())),
        mcp_configurations=tuple(MCPConfigurationReference(capability_id=key,secret_version=value)
                                 for key,value in sorted(mcps.items())))
    payload = spec.model_dump(mode='json')
    db.add(RuntimeExecutionSpec(task_id=task.id,session_id=session.id,schema_version=1,
                               payload=payload,digest=_digest(payload)))
    await db.flush()
    return spec


@_fresh_identity_map
async def read_execution_context(db, *, session_id, task_id, worker_id, execution_epoch,
                                 storage_node_id, knowledge_enabled=False, allow_pending_recovery=False):
    """Resolve current assembly inputs from a trusted claimed owner tuple.

    The original capability/KB ceiling never widens after admission. Removing an
    original binding fails assembly; added bindings are excluded from this Task.
    A Stop cleanup path must not call this authorization-dependent assembly API.
    """
    if (type(execution_epoch) is not int or execution_epoch < 1
            or any(not isinstance(v,str) or not v.strip() for v in
                   (session_id,task_id,worker_id,storage_node_id))):
        raise ExecutionContextRejected('A complete claimed execution owner is required')
    agent,conversation,task,session,execution,capabilities = await _facts(
        db,task_id,session_id,knowledge_enabled=knowledge_enabled)
    source = await _schedule_source(db, task, conversation)
    now = await db.scalar(text('SELECT clock_timestamp()'))
    if (session.owner_id != worker_id or session.execution_epoch != execution_epoch
            or execution.execution_epoch != execution_epoch or session.storage_node_id != storage_node_id
            or session.active_task_id != task_id or session.lease_until is None or session.lease_until <= now
            or session.status not in {'preparing','running','awaiting_interaction'}
            or execution.status != session.status or execution.state_version != session.state_version
            or execution.reply_id != session.reply_id or task.status not in {'created','running','awaiting_permission'}):
        raise ExecutionContextRejected('Claimed execution owner, lease or state is unavailable')
    from .recovery_context import recovery_spec_fields
    recovery = await recovery_spec_fields(db, conversation, session, task_id=task_id)
    spec = _stored_spec(await db.get(RuntimeExecutionSpec,task_id,populate_existing=True))
    if (spec.session_id != session_id or spec.conversation_id != conversation.id
            or spec.origin != source['origin']
            or spec.schedule_occurrence_id != source['schedule_occurrence_id']
            or spec.channel_inbox_id != source['channel_inbox_id']
            or (spec.origin == 'schedule' and spec.selected_model.profile_name != conversation.model_profile)
            or spec.actor_user_id != conversation.user_id or spec.agent_id != agent.id
            or spec.approval_profile != task.approval_profile
            or spec.platform_tool_profile != agent.platform_tool_profile
            or spec.recovery_fork_id != recovery.get('recovery_fork_id')
            or spec.recovery_manifest_sha256 != recovery.get('recovery_manifest_sha256')
            or spec.recovery_context_text != recovery.get('recovery_context_text')):
        raise ExecutionContextRejected('Initial execution spec identity or approval profile changed')
    knowledge = KnowledgeConfig.model_validate(agent.knowledge_config or {})
    if (not set(spec.capability_ids).issubset(c.id for c in capabilities)
            or not set(spec.knowledge_base_ids).issubset(knowledge.knowledge_base_ids)):
        raise ExecutionContextRejected('An original capability or knowledge binding was removed')
    from .skill_repository import ready_skill_artifacts
    from ..persistence.models import SkillArtifactRecord
    pinned_capabilities = [c for c in capabilities if c.id in spec.capability_ids]
    ready = await ready_skill_artifacts(db,pinned_capabilities)
    pinned = {ref.capability_id:ref.digest for ref in spec.skill_artifacts}
    if len(pinned) != len(spec.skill_artifacts) or set(pinned) != set(ready):
        raise ExecutionContextRejected('Initial Skill artifact descriptors are missing or inconsistent')
    if pinned:
        available = set((await db.scalars(select(SkillArtifactRecord.digest).where(
            SkillArtifactRecord.digest.in_(set(pinned.values())),SkillArtifactRecord.format_version==1))).all())
        if available != set(pinned.values()):
            raise ExecutionContextRejected('An original Skill artifact is unavailable')
    from ..mcp.repository import ready_mcp_revisions
    from ..persistence.models import MCPSecretRevision
    ready_mcps = await ready_mcp_revisions(db,pinned_capabilities)
    pinned_mcps = {ref.capability_id:ref.secret_version for ref in spec.mcp_configurations}
    if len(pinned_mcps) != len(spec.mcp_configurations) or set(pinned_mcps) != set(ready_mcps):
        raise ExecutionContextRejected('Initial MCP configuration descriptors are missing or inconsistent')
    if pinned_mcps:
        from ..mcp.secret_store import revision_scope_matches
        installations = {cap.id:cap for cap in pinned_capabilities}
        rows = (await db.execute(select(MCPSecretRevision.id,MCPSecretRevision.capability_id,MCPSecretRevision.scope_id)
            .where(MCPSecretRevision.id.in_(set(pinned_mcps.values()))))).all()
        available = {capability_id:version for version,capability_id,scope_id in rows
                     if capability_id in installations and revision_scope_matches(
                         capability_id=capability_id,requested_scope=installations[capability_id].scope_id,
                         revision_scope=scope_id,installation=installations[capability_id])}
        if available != pinned_mcps:
            raise ExecutionContextRejected('An original MCP configuration is unavailable')
    knowledge = knowledge.model_copy(update={'knowledge_base_ids':list(spec.knowledge_base_ids)},deep=True)
    current = _policy(agent.tool_policy)
    effective = ToolPolicy(denied_groups=tuple(sorted(set(current.denied_groups)|set(spec.tool_policy.denied_groups))),
                           denied_tools=tuple(sorted(set(current.denied_tools)|set(spec.tool_policy.denied_tools))))
    messages = (await db.scalars(select(TaskMessage).where(TaskMessage.task_id==task_id,
        TaskMessage.conversation_id==conversation.id,TaskMessage.role=='user')
        .order_by(TaskMessage.created_at,TaskMessage.id))).all()
    from .full_access import effective_permission_profile
    profile,permission_version=await effective_permission_profile(db,task,session,spec,allow_pending_recovery=allow_pending_recovery)
    return ExecutionContext(task_id=task_id,session_id=session_id,conversation_id=conversation.id,
        actor_user_id=conversation.user_id,agent_id=agent.id,agent_scope_id=agent.scope_id,
        system_prompt=agent.system_prompt,platform_tool_profile=agent.platform_tool_profile,
        current_tool_policy=current,effective_tool_policy=effective,current_model_profile=conversation.model_profile,
        knowledge_config=knowledge,capabilities=tuple(CapabilityReference(c.id,c.capability_type,c.scope_id,
            c.name) for c in sorted(capabilities,key=lambda c:c.id)
            if c.id in spec.capability_ids),
        input_messages=tuple(ProductInputMessage(m.id,deepcopy(m.content)) for m in messages),initial_spec=spec,
        permission_version=permission_version,effective_approval_profile=profile)
