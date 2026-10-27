"""Transactional Start admission, staged for the API/Worker execution chain."""
from workmate_product.coordination import stage_wakeup
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json

from ..persistence.repositories import start_admission as repository
from sqlalchemy.ext.asyncio import AsyncSession

from workmate_contracts import Command


class CommandRejected(ValueError):
    """Current Product authority, binding or execution state forbids admission."""


class CommandConflict(CommandRejected):
    """A stable command identity was reused for different content."""


@dataclass(frozen=True)
class AcceptedCommand:
    command_id: str
    product_task_id: str
    status: str
    duplicate: bool


async def accept_start(
    db: AsyncSession, command: Command, *, actor_user_id: str, storage_node_id: str,
    authorize_agent: Callable[[AsyncSession, str, str], Awaitable[bool]],
    prepare_task: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> AcceptedCommand:
    """Join the caller's PostgreSQL transaction; never commit it independently.

    actor_user_id is derived from authentication, never model/browser arguments.
    authorize_agent must run the shared/current Product access decision. For a new
    Task, prepare_task performs capacity checks and inserts Task, message, scope
    attribution and attachment facts AFTER locks and idempotency checks. It must
    not commit. Existing queued Tasks can omit it. Errors require caller rollback.

    Admission persists the command atomically; the Worker dispatches execution.
    """
    if command.kind != 'StartTask' or not command.product_task_id:
        raise CommandRejected('Only StartTask is accepted by this service')
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise CommandRejected('Start admission requires a caller-owned PostgreSQL transaction')
    if not storage_node_id or len(storage_node_id) > 128:
        raise CommandRejected('A valid storage node binding is required')
    digest = hashlib.sha256(json.dumps({
        'command': command.content_digest(), 'actor_user_id': actor_user_id,
        'storage_node_id': storage_node_id,
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    await repository.lock_admission(db)
    candidate = await repository.conversation_agent(db, command.conversation_id)
    if candidate is None:
        raise CommandRejected('Conversation is unavailable')
    agent = await repository.lock_agent(db, candidate['agent_id'])
    conversation = await repository.lock_conversation(db, command.conversation_id)
    if (agent is None or agent['deleted_at'] is not None or conversation is None
            or conversation['deleted_at'] is not None or conversation['agent_id'] != candidate['agent_id']
            or conversation['user_id'] != actor_user_id):
        raise CommandRejected('Conversation or Agent is unavailable to this actor')
    actor_status = await repository.actor_status(db, actor_user_id)
    if actor_status != 'active' or await authorize_agent(db, actor_user_id, agent['id']) is not True:
        raise CommandRejected('Current Agent access is denied')

    existing = await repository.existing_command(db, command.command_id)
    if existing is not None:
        if existing['content_digest'] != digest:
            raise CommandConflict('Command identity already has different content')
        return AcceptedCommand(existing['id'], existing['task_id'], existing['status'], True)
    if prepare_task is not None:
        await prepare_task(db)
    await db.flush()
    task = await repository.lock_task(db, command.product_task_id)
    if task is None or task['conversation_id'] != command.conversation_id or task['status'] != 'created':
        raise CommandRejected('Task is not eligible for first execution')
    if conversation['active_product_task_id'] is not None:
        raise CommandRejected('Previous Task has not cleared its Product delivery gate')
    previous_command = await repository.start_for_task(db, command.product_task_id)
    if previous_command is not None:
        raise CommandConflict('Task already has a Start command')

    session = await repository.lock_session(db, command.conversation_id)
    if session is None:
        if command.expected_epoch != 0:
            raise CommandRejected('New Session requires initial epoch')
        conflicting = await repository.session_identity(db, command.runtime_session_id)
        if conflicting is not None:
            raise CommandRejected('Runtime Session belongs to another Conversation')
        await repository.create_session(db, command, storage_node_id)
    elif (session['id'] != command.runtime_session_id or session['storage_node_id'] != storage_node_id
            or session['execution_epoch'] != command.expected_epoch or session['active_task_id'] is not None
            or session['status'] not in {'accepted','completed','failed','cancelled','interrupted'}):
        raise CommandRejected('Runtime Session is not available for this admission')

    await repository.persist_admission(db, command, actor_user_id, digest)
    stage_wakeup(db, "commands")
    return AcceptedCommand(command.command_id, command.product_task_id, 'accepted', False)
