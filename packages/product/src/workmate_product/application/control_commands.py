"""Transactional control intent admission; no engine dispatch or HTTP policy.

Caller supplies an authenticated actor and (for Resume) a current access check.
The check must cover Agent, capability and RAG authority within this transaction,
without commits or external I/O. Account-disable cleanup has a separate entry
that requires the durable receipt of a root-authorized administrative action.
"""
import hashlib
import json
from workmate_product.coordination import stage_wakeup
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from workmate_product.persistence.repositories import control_admission as persistence
from workmate_contracts import Command
from workmate_contracts.controls import ResumePayload, StopPayload
from workmate_product.application.commands import AcceptedCommand, CommandConflict, CommandRejected
from workmate_product.projections.store import ProjectedEvent, append_business_events
from .task_capacity import TaskCapacityPolicy
from ..persistence.repositories.task_capacity import lock_capacity


def _transaction(db):
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise CommandRejected('Control admission requires a caller-owned PostgreSQL transaction')


def _digest(command, actor):
    return hashlib.sha256(json.dumps({'command': command.content_digest(), 'actor': actor},
                                    sort_keys=True).encode()).hexdigest()


async def _owner(db, command, actor):
    # Admission lock order: Agent -> Conversation -> Task -> Session -> execution.
    # Keep the candidate read and rejection before acquiring the ordered locks.
    candidate = await persistence.conversation_agent(db, id=command.conversation_id)
    if candidate is None:
        raise CommandRejected('Conversation unavailable')
    agent = await persistence.lock_agent(db, id=candidate['agent_id'])
    conversation = await persistence.lock_conversation(db, id=command.conversation_id)
    status = await persistence.actor_status(db, actor)
    if (conversation is None or conversation['deleted_at'] is not None or conversation['agent_id'] != candidate['agent_id']
            or conversation['user_id'] != actor or status != 'active'):
        raise CommandRejected('Conversation unavailable to this actor')
    return agent, conversation


def _duplicate(existing, digest):
    if existing['content_digest'] != digest:
        raise CommandConflict('Command or decision identity already has different content')
    return AcceptedCommand(existing['id'], existing['task_id'], existing['status'], True)


async def _binding(db, command, conversation, *, stopping=False):
    task = await persistence.lock_task(db, id=command.product_task_id)
    session = await persistence.lock_session(db, id=command.runtime_session_id)
    execution = await persistence.lock_execution(db, id=command.product_task_id)
    if (not task or not session or not execution or task['conversation_id'] != conversation['id']
            or session['conversation_id'] != conversation['id'] or execution['session_id'] != session['id']
            or conversation['active_product_task_id'] != task['id']):
        raise CommandRejected('Task/Session ownership is stale')
    if stopping:
        if execution['status'] == 'failed' and execution['reply_id'] is None:
            raise CommandRejected('Task failed before execution; terminal projection is pending')
        # An admitted Start has not yet acquired the Runtime gate. The Session
        # may still retain the previous Task's Reply and terminal snapshot.
        unclaimed = (task['status'] in {'created', 'cancelling'} and execution['status'] == 'accepted'
                     and execution['execution_epoch'] == 0 and execution['reply_id'] is None
                     and session['active_task_id'] is None
                     and session['status'] in {'accepted', 'completed', 'failed', 'cancelled', 'interrupted'})
        active = (session['active_task_id'] == task['id']
                  and execution['execution_epoch'] == session['execution_epoch']
                  and execution['reply_id'] == session['reply_id'])
        if not (unclaimed or active):
            raise CommandRejected('Stop target is not the admitted or active Task')
    elif (session['active_task_id'] != task['id']
            or any(row['execution_epoch'] != command.expected_epoch for row in (session, execution))
            or any(row['reply_id'] != command.reply_id for row in (session, execution))
            or task['run_id'] != command.reply_id):
        raise CommandRejected('Task/Session/Reply ownership or epoch is stale')
    return task, session, execution


async def _replay(db, command, name, value):
    await append_business_events(db, command.conversation_id, operation_key=command.command_id,
        task_id=command.product_task_id, reply_id=command.reply_id,
        items=[ProjectedEvent(key='control', event_type='CUSTOM',
            payload={'type': 'CUSTOM', 'name': name, 'value': value})])


async def _pending(db, command, payload, session, execution):
    if any(row['status'] != 'awaiting_interaction' or row['snapshot_id'] != payload.snapshot_id
           or row['state_version'] != payload.state_version for row in (session, execution)):
        raise CommandRejected('Execution is not parked at the requested snapshot')
    snapshot = await persistence.snapshot(db, id=payload.snapshot_id)
    saved = await persistence.lock_projection(db, id=command.product_task_id)
    cursor = await persistence.projection_cursor(db, id=command.conversation_id)
    if (not snapshot or snapshot['session_id'] != session['id'] or snapshot['task_id'] != command.product_task_id
            or snapshot['reply_id'] != command.reply_id or snapshot['execution_epoch'] != command.expected_epoch
            or snapshot['state_version'] != payload.state_version or snapshot['status'] != 'awaiting_interaction'
            or not saved or saved['session_id'] != session['id'] or saved['reply_id'] != command.reply_id
            or saved['format_version'] != 1 or saved['last_runtime_seq'] != snapshot['runtime_seq']
            or session['runtime_seq'] != snapshot['runtime_seq']
            or not cursor or cursor['session_id'] != session['id']
            or cursor['last_runtime_seq'] != snapshot['runtime_seq']):
        raise CommandRejected('Projection is not at the current immutable boundary')
    state = saved['state']
    record = state.get('interactions', {}).get(command.interaction_id)
    pending = next((p for p in state.get('pending_interactions', [])
                    if p['interaction_id'] == command.interaction_id), None)
    if (state.get('interaction_boundary_id') != payload.snapshot_id or not record or not pending
            or record['reply_id'] != command.reply_id
            or any(p['kind'] != 'permission' or p['request_version'] != command.request_version for p in (record, pending))):
        raise CommandRejected('Permission request is stale or unavailable')
    history = await persistence.task_history(db, command)
    requested, closed, decided = set(), set(), set()
    for row in history:
        item = row['payload']
        if row['event_type'] == 'hitl_request':
            requested.add(item.get('decisionVersion'))
        elif row['event_type'] in {'hitl_abandoned', 'hitl_resume_exhausted'}:
            closed.add(item.get('decisionVersion'))
        elif row['event_type'] == 'hitl_decision':
            decided.update(d['toolCallId'] for d in item.get('decisions', []))
    ids = {d.tool_call_id for d in payload.decisions}
    original_ids = {call['id'] for call in record['tool_calls']}
    if (command.request_version not in requested or command.request_version in closed
            or not ids <= set(pending['pending_tool_call_ids']) & original_ids or ids & decided):
        raise CommandRejected('Decision contains unavailable or already decided tool calls')
    return state, sorted(decided & original_ids | ids)


async def _accept_resume(db: AsyncSession, command: Command, *, actor_user_id: str,
                        authorize_resume: Callable[[AsyncSession, str, str], Awaitable[bool]],
                        upgrade=None, policy=TaskCapacityPolicy()) -> AcceptedCommand:
    """Persist current-batch decisions and command atomically; never resume here."""
    _transaction(db)
    if command.kind != 'ResumeInteraction':
        raise CommandRejected('Expected ResumeInteraction')
    payload = ResumePayload.model_validate(command.payload)
    digest = _digest(command, actor_user_id)
    async with db.begin_nested():
        await lock_capacity(db)
        agent, conversation = await _owner(db, command, actor_user_id)
        existing = await persistence.command(db, id=command.command_id)
        if existing:
            return _duplicate(existing, digest)
        existing = await persistence.resume_decision(db, id=command.product_task_id, version=payload.decision_version)
        if existing:
            return _duplicate(existing, digest)
        if (not agent or agent['deleted_at'] is not None
                or await authorize_resume(db, actor_user_id, agent['id']) is not True):
            raise CommandRejected('Current Agent/capability access is denied')
        if payload.full_access and upgrade is None:
            raise CommandRejected('Full Access requires its separate authorized upgrade service')
        task, session, execution = await _binding(db, command, conversation)
        if task['status'] != 'awaiting_permission':
            raise CommandRejected('Task is not awaiting permission')
        busy = await persistence.outstanding_resume(db, id=task['id'], snapshot=payload.snapshot_id)
        if busy:
            raise CommandRejected('A Resume command is already outstanding')
        state, decided = await _pending(db, command, payload, session, execution)
        if await policy.expired(db, task['id'], command.request_version):
            raise CommandRejected('Approval request expired; it can no longer be resumed')
        await policy.check(db, actor_user_id, resuming_task_id=task['id'])
        if upgrade is not None:
            await upgrade(db,command,task,session,record=False)
        await persistence.insert_command(db, command, actor_user_id, digest)
        stage_wakeup(db, "commands")
        if upgrade is not None:
            await upgrade(db,command,task,session,record=True)
        await persistence.append_audit(db, command, 'hitl_decision', {'decisionVersion': payload.decision_version,
            'requestVersion': command.request_version, 'actorUserId': actor_user_id, 'scope': 'task' if payload.full_access else 'current_batch',
            **({'permissionMode':'bypass'} if payload.full_access else {}),
            'decisions': [{'toolCallId': d.tool_call_id, 'approved': d.approved} for d in payload.decisions]})
        await persistence.set_decision_version(db, payload, task)
        record = state['interactions'][command.interaction_id]
        updated = {**state, 'interactions': {**state['interactions'], command.interaction_id:
                   {**record, 'published_decided_ids': decided}},
            'pending_interactions': [{**p, 'awaiting_resume': set(p['pending_tool_call_ids']) <= set(decided)}
                if p['interaction_id'] == command.interaction_id else p for p in state['pending_interactions']]}
        await persistence.save_interaction_state(db, task, updated)
        await _replay(db, command, 'HITL_DECISION_RECORDED',
                      {'decisionVersion': command.request_version, 'decidedToolCallIds': decided})
        return AcceptedCommand(command.command_id, task['id'], 'accepted', False)


async def accept_resume(db,command,*,actor_user_id,authorize_resume,policy=TaskCapacityPolicy()):
    return await _accept_resume(db,command,actor_user_id=actor_user_id,authorize_resume=authorize_resume,policy=policy)


async def accept_stop(db: AsyncSession, command: Command, *, actor_user_id: str) -> AcceptedCommand:
    """Admit Stop for an active bound Task, even after Agent access is revoked.

    Lease expiry is irrelevant to intent. Worker must separately prove cleanup;
    neither execution status nor either admission gate is cleared here.
    """
    _transaction(db)
    if command.kind != 'StopTask':
        raise CommandRejected('Expected StopTask')
    payload = StopPayload.model_validate(command.payload)
    if payload.reason != 'user_requested':
        raise CommandRejected('Administrator/policy Stop requires its dedicated authorization receipt')
    async with db.begin_nested():
        _, conversation = await _owner(db, command, actor_user_id)
        return await _accept_stop_locked(db, command, actor_user_id, conversation)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError('Missing disable timestamp')
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


async def lock_disabled_user_conversation(db, *, conversation_id, actor_user_id,
                                          target_user_id, disable_event_id):
    """Validate the committed root-authorized disable, then lock its target.

    A disabled administrator may finish their own disable; later revocation
    must not strand safety cleanup. The audit receipt is tied to this precise
    disable cycle, not merely to an arbitrary historical administration event.
    """
    _transaction(db)
    await persistence.lock_organization_scopes(db)
    target = await persistence.lock_disabled_user(db, id=target_user_id)
    receipt = await persistence.disable_receipt(db, id=disable_event_id)
    try:
        valid_time = bool(target and receipt and
            _utc(target['disabled_at']) == _utc(receipt['metadata'].get('disabled_at')))
    except (TypeError, ValueError, AttributeError):
        valid_time = False
    if (not target or target['status'] != 'disabled' or target['disabled_by'] != actor_user_id
            or not receipt or receipt['event_type'] != 'user.disable'
            or receipt['actor_user_id'] != actor_user_id or receipt['target_user_id'] != target_user_id
            or not valid_time):
        raise CommandRejected('A matching current account-disable authorization receipt is required')
    candidate = await persistence.conversation_agent(db, id=conversation_id)
    if candidate is None:
        raise CommandRejected('Conversation unavailable')
    await persistence.lock_agent_identity(db, candidate)
    conversation = await persistence.lock_conversation(db, id=conversation_id)
    if (not conversation or conversation['user_id'] != target_user_id
            or conversation['agent_id'] != candidate['agent_id']):
        raise CommandRejected('Conversation does not belong to the disabled account')
    return conversation


async def accept_disabled_user_stop(db: AsyncSession, command: Command, *, actor_user_id: str,
                                   target_user_id: str, disable_event_id: str) -> AcceptedCommand:
    """Admit only cleanup arising from an already committed account disable."""
    _transaction(db)
    if command.kind != 'StopTask' or StopPayload.model_validate(command.payload).reason != 'account_disabled':
        raise CommandRejected('Expected account-disable StopTask')
    async with db.begin_nested():
        conversation = await lock_disabled_user_conversation(db,
            conversation_id=command.conversation_id, actor_user_id=actor_user_id,
            target_user_id=target_user_id, disable_event_id=disable_event_id)
        return await _accept_stop_locked(db, command, actor_user_id, conversation,
                                        disable_event_id=disable_event_id)


async def _accept_stop_locked(db, command, actor_user_id, conversation, *, disable_event_id=None,
                              expiry_version=None):
    payload = StopPayload.model_validate(command.payload)
    digest = _digest(command, actor_user_id)
    existing = await persistence.command(db, id=command.command_id)
    if existing:
        return _duplicate(existing, digest)
    task, session, execution = await _binding(db, command, conversation, stopping=True)
    existing = await persistence.stop_command(db, id=task['id'])
    if existing:
        if disable_event_id or expiry_version:
            # An already authorized owner/admin Stop is sufficient cleanup.
            # Never replace its actor or create competing Stop commands.
            return AcceptedCommand(existing['id'], existing['task_id'], existing['status'], True)
        return _duplicate(existing, digest)
    if task['status'] not in {'created', 'running', 'awaiting_permission', 'cancelling'}:
        raise CommandRejected('Task is not eligible for Stop')
    current = command.model_copy(update={'expected_epoch': session['execution_epoch'],
                                         'reply_id': execution['reply_id']})
    await persistence.insert_command(db, current, actor_user_id, digest)
    stage_wakeup(db, "commands")
    await persistence.reject_pending_commands(db, task)
    await persistence.mark_cancelling(db, task)
    audit = {'actorUserId': actor_user_id, 'previousStatus': task['status'], 'reason': payload.reason}
    if disable_event_id:
        audit.update(commandId=command.command_id, targetUserId=conversation['user_id'],
                     disableEventId=disable_event_id)
    if expiry_version:
        audit.update(commandId=command.command_id, targetUserId=conversation['user_id'],
                     requestVersion=expiry_version, initiatedBy='approval_expiry_policy')
    await persistence.append_audit(db, command, 'task_cancel_requested', audit)
    await _replay(db, current, 'TASK_CANCEL_REQUESTED', {'taskId': task['id'], 'status': 'cancelling'})
    return AcceptedCommand(command.command_id, task['id'], 'accepted', False)
