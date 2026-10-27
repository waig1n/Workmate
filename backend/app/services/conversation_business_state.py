"""Authorized Product reset views, materialized before Redis or SSE work."""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from functools import wraps

import anyio

from sqlalchemy import text

from workmate_product.config.realtime import RealtimePolicy
from workmate_product.persistence.repositories import conversation_display as repository
from workmate_product.persistence.repositories.message_snapshots import from_message
from workmate_product.projections.knowledge import compact_event_payload
from workmate_product.realtime.reset_builder import ActiveDisplay, ResetReadView
from workmate_product.projections.store import ProjectionConflict
from workmate_contracts.realtime import MessageSnapshotReference, message_id

from ..api.schemas import ArtifactOut, MessageOut, TaskEventOut
from .task_approval import pending_confirm, present_approval_history
from .task_presentation import present_tasks


class ConversationUnavailable(ProjectionConflict):
    """The Conversation is no longer available to this stream's actor."""


class ResetCapacityExceeded(ValueError):
    """A full reset exceeds its budget; no truncated snapshot was returned."""


class ResetStateConflict(ValueError):
    """The active Product Task and Runtime execution identity disagree."""


class DisplaySnapshotUnavailable(ValueError):
    """The notification's exact display version is absent or superseded."""


# Internal cleanup bound, not a query or request deadline.
_READ_CLEANUP_SECONDS = 5


def _cancel_read_once(method):
    """Relay request cancellation once, allowing the DB driver to cancel SQL."""
    @wraps(method)
    async def wrapped(*args, **kwargs):
        async def read():
            # The parent still cancels this task explicitly. Repeated AnyIO
            # cancellation must not interrupt asyncpg's own cancel request.
            with anyio.CancelScope(shield=True):
                return await method(*args, **kwargs)

        task = asyncio.create_task(read())
        try:
            return await asyncio.shield(task)
        except BaseException:
            with anyio.CancelScope(shield=True):
                task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if not task.cancelled():
                    task.result()  # Surface cleanup failure, never leave it detached.
            raise
    return wrapped


_QUIESCENT = {'awaiting_interaction', 'completed', 'failed', 'cancelled', 'interrupted'}
_ACTIVITY_KEYS = {
    'tool_call': ('toolCallId', 'tool'),
    'tool_content': ('toolCallId', 'kind', 'sha256', 'sizeBytes', 'preview', 'incomplete'),
    'tool_result': ('toolCallId', 'tool', 'status', 'duration_ms'),
    'run_started': ('replyId',), 'run_resumed': ('replyId',),
    'run_finished': ('reason', 'durationMs', 'steps', 'replyId'),
    'task_failed': ('reason', 'executionStarted'), 'task_cancelled': ('reason', 'executionStarted'),
    'task_cancel_requested': (), 'execution_quarantined': ('code',),
    'permission_mode_changed': ('from', 'to', 'permissionMode', 'reason', 'decisionVersion'),
    'artifact_delivery': ('source', 'warnings'),
}


def _active(binding):
    if binding['task_id'] is None:
        if binding['session_task_id'] is not None:
            raise ResetStateConflict('Runtime Task has no matching Product gate')
        return None
    if (binding['task_status'] in {'created', 'cancelling'} and binding['run_id'] is None
            and binding['session_id'] is not None
            and binding['session_task_id'] is None and binding['owner_id'] is None
            and binding['status'] in {'accepted', 'completed', 'failed', 'cancelled', 'interrupted'}
            and binding['execution_session_id'] == binding['session_id']
            and binding['execution_status'] == 'accepted' and binding['execution_epoch_copy'] == 0
            and binding['execution_reply_id'] is None
            and ((binding['task_status'] == 'created' and binding['start_pending'])
                 or (binding['task_status'] == 'cancelling' and binding['unstarted_stop_pending']))):
        return None
    if (binding['session_id'] is None or binding['session_task_id'] != binding['task_id']
            or binding['execution_session_id'] != binding['session_id']
            or binding['execution_epoch_copy'] != binding['execution_epoch']
            or binding['execution_reply_id'] != binding['reply_id']
            or binding['execution_status'] != binding['status']
            or binding['run_id'] not in (None, binding['reply_id'])):
        raise ResetStateConflict('Active Task/Session/Reply/epoch binding is inconsistent')
    if binding['reply_id'] is None:
        return None
    empty = (binding['display_empty'] is True
        and binding['status'] in _QUIESCENT
        and binding['execution_snapshot_id'] == binding['snapshot_id'] == binding['boundary_id']
        and binding['boundary_id'] is not None
        and binding['boundary_task_id'] == binding['task_id']
        and binding['boundary_reply_id'] == binding['reply_id'] == binding['event_reply_id']
        and binding['boundary_epoch'] == binding['execution_epoch']
        and binding['boundary_version'] == binding['state_version'] == binding['execution_state_version']
        and binding['boundary_seq'] == binding['runtime_seq']
        and binding['boundary_status'] == binding['status'] == binding['event_boundary_status']
        and binding['boundary_type'] == 'EXECUTION_BOUNDARY')
    refresh = (binding['task_status'] in {'created', 'running', 'cancelling'}
               and binding['status'] in {'preparing', 'running', 'stopping'}
               and binding['owner_id'] is not None and binding['lease_live'] is True)
    return ActiveDisplay(binding['task_id'], binding['reply_id'], binding['execution_epoch'],
                         refresh_possible=refresh, empty=empty)


def _message(row):
    content = row.content or {}
    public = {'text': content['text']} if 'text' in content else {}
    if isinstance(content.get('presentation'), dict):
        public['presentation'] = {key: content['presentation'][key] for key in ('version', 'process_end')
                                  if key in content['presentation']}
    if isinstance(content.get('attachments'), list):
        public['attachments'] = [{key: item[key] for key in ('name', 'path', 'mime_type', 'size')
            if key in item} for item in content['attachments'] if isinstance(item, dict)]
    return MessageOut.model_validate(row).model_copy(update={'content': public}).model_dump(mode='json')


def _event(row, payload):
    return TaskEventOut.model_validate(row).model_copy(update={'payload': payload}).model_dump(mode='json')


class ConversationBusinessState:
    def __init__(self, sessions, actor_id, *, policy=None, token_version=None):
        self.sessions, self.actor_id = sessions, actor_id
        self.policy = RealtimePolicy() if policy is None else policy
        self.token_version = token_version

    @asynccontextmanager
    async def _read_transaction(self):
        db = self.sessions()
        transaction = await db.begin()
        try:
            yield db
        finally:
            # AnyIO disconnect cancellation and explicit asyncio Task.cancel
            # can both reach a live SSE operation. Own and join cleanup under
            # both kinds of shielding; never shield queries/authorization.
            with anyio.CancelScope(shield=True):
                cleanup = asyncio.create_task(self._finish_read(db, transaction, sys.exc_info()))
                cancelled = None
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as error:
                        cancelled = error
                cleanup.result()
                if cancelled is not None:
                    raise cancelled

    async def _finish_read(self, db, transaction, error):
        try:
            # Explicit exits avoid sessionmaker.begin()'s detached exit tasks.
            with anyio.fail_after(_READ_CLEANUP_SECONDS):
                await transaction.__aexit__(*error)
                await db.close()
        except BaseException:
            # A timed-out/failed rollback cannot return a reusable connection.
            # asyncpg termination allows 2 seconds to close, then forces it.
            await db.invalidate()
            raise

    async def _authorize(self, db, conversation_id):
        conversation = await repository.visible_conversation(
            db, conversation_id, self.actor_id, self.token_version,
        )
        if conversation is None:
            raise ConversationUnavailable('Conversation not found')
        return conversation

    @_cancel_read_once
    async def authorize(self, conversation_id):
        async with self._read_transaction() as db:
            await self._authorize(db, conversation_id)

    @_cancel_read_once
    async def active_display(self, conversation_id):
        async with self._read_transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            await self._authorize(db, conversation_id)
            return _active(await repository.active_binding(db, conversation_id))

    @_cancel_read_once
    async def display_snapshot(self, conversation_id, reference, *, task_id, reply_id,
                               expected_approval=None):
        reference = MessageSnapshotReference.model_validate(reference)
        if reference.message_id != message_id(task_id, reply_id):
            raise DisplaySnapshotUnavailable('Notification identifies another display message')
        async with self._read_transaction() as db:
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            await self._authorize(db, conversation_id)
            size = await repository.message_bytes(db, conversation_id, reference.message_id)
            if size is not None and size > self.policy.reset_max_bytes:
                raise ResetCapacityExceeded('Display snapshot exceeds its read budget')
            row = await repository.message(db, conversation_id, reference.message_id)
            if row is None or row.snapshot_version == 0:
                raise DisplaySnapshotUnavailable('The referenced display snapshot is unavailable')
            snapshot = from_message(row)
            if (snapshot.task_id != task_id or snapshot.reply_id != reply_id
                    or MessageSnapshotReference.from_snapshot(snapshot) != reference):
                raise DisplaySnapshotUnavailable('The referenced display snapshot was superseded')
            if snapshot.phase == 'approval':
                binding = await repository.active_binding(db, conversation_id)
                active = _active(binding)
                if (binding['status'] != 'awaiting_interaction'
                        or binding['task_status'] != 'awaiting_permission' or active is None
                        or (active.task_id, active.reply_id, active.execution_epoch)
                        != (snapshot.task_id, snapshot.reply_id, snapshot.execution_epoch)):
                    raise DisplaySnapshotUnavailable('The approval execution is no longer pending')
                task = await repository.task(db, conversation_id, task_id)
                pending = (await pending_confirm(db, task)) if task is not None else None
                if pending is None or (expected_approval is not None and any(
                    pending.get(key) != expected_approval.get(key)
                    for key in ('taskId', 'decisionVersion', 'toolCalls')
                )):
                    raise DisplaySnapshotUnavailable('The approval decision or pending calls changed')
            elif expected_approval is not None:
                raise DisplaySnapshotUnavailable('An approval notification requires its approval cut')
            return snapshot

    @_cancel_read_once
    async def read_view(self, conversation_id):
        async with self._read_transaction() as db:
            # READ COMMITTED would mix the captured business_seq with later
            # approvals/messages. Set isolation before the first data read.
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
            conversation = await self._authorize(db, conversation_id)
            budget = await repository.read_budget(db, conversation_id)
            if budget['rows'] > self.policy.reset_max_rows or budget['bytes'] > self.policy.reset_max_bytes:
                raise ResetCapacityExceeded('Conversation reset exceeds its read budget')
            binding = await repository.active_binding(db, conversation_id)
            active = _active(binding)
            rows = await repository.messages(db, conversation_id)
            tasks = await repository.tasks(db, conversation_id)
            from workmate_product.application.execution_quarantine import is_quarantined
            uncertain = {task.id for task in tasks if is_quarantined(task.error)}
            snapshots = tuple(from_message(row) for row in rows if row.snapshot_version > 0)
            # A saved final display is complete even if execution quiescence was
            # never committed. Preserve that fact; Task/error reports uncertainty.
            snapshots = tuple(cut.model_copy(update={'incomplete': True})
                if cut.task_id in uncertain and cut.phase != 'final' else cut
                for cut in snapshots)
            events = await repository.display_events(db, conversation_id)
            artifact_rows = await repository.artifacts(db, conversation_id)
            business = {
                'messages': [_message(row) for row in rows if row.snapshot_version == 0],
                'tasks': [task.model_dump(mode='json') for task in await present_tasks(db, tasks)],
                'artifacts': [ArtifactOut.model_validate(artifact).model_copy(update={
                    'task_id': delivery.task_id, 'change_type': delivery.change_type,
                    'metadata': delivery.metadata_, 'created_at': delivery.delivered_at,
                }).model_dump(mode='json') for artifact, delivery in artifact_rows],
                'activity': [], 'knowledge': [], 'pending_confirm': None,
                'decided_confirm_tools': {}, 'closed_confirm_versions': [],
            }
            by_task = {}
            for event in events:
                if event.event_type in repository.APPROVAL_KINDS:
                    by_task.setdefault(event.task_id, []).append(event)
                elif event.event_type in {'knowledge_sources', 'knowledge_retrieval'}:
                    business['knowledge'].append(_event(event, compact_event_payload(
                        event.event_type, event.payload or {})))
                else:
                    keys = _ACTIVITY_KEYS[event.event_type]
                    business['activity'].append(_event(event,
                        {key: event.payload[key] for key in keys if key in (event.payload or {})}))
            closed = set()
            for task in tasks:
                pending, decisions, versions = present_approval_history(task, by_task.get(task.id, []))
                if pending is not None:
                    if pending['taskId'] != conversation.active_product_task_id:
                        raise ResetStateConflict('Approval belongs to a nonactive Product Task')
                    # Product projection may lag a resumed/stopping Runtime.
                    # Preserve the approval history without reviving its card.
                    if binding['status'] == 'awaiting_interaction':
                        business['pending_confirm'] = pending
                business['decided_confirm_tools'].update(decisions)
                closed.update(versions)
            business['closed_confirm_versions'] = sorted(closed)
            result = ResetReadView(conversation_id, conversation.business_seq, business, snapshots, active)
            size = len(json.dumps({'business_state': business,
                'snapshots': [snapshot.model_dump(mode='json') for snapshot in snapshots]},
                ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8'))
            if size > self.policy.reset_max_bytes:
                raise ResetCapacityExceeded('Conversation reset exceeds its output budget')
            return result
