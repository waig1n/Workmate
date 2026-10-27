"""Bounded Product display reads; the caller owns one consistent transaction."""
from sqlalchemy import select, text, func, cast, Text

from ..models import (
    Artifact, Conversation, Task, TaskArtifactDelivery, TaskEvent, TaskMessage, User,
)

APPROVAL_KINDS = ('hitl_request', 'hitl_decision', 'hitl_abandoned', 'hitl_resume_exhausted')
DISPLAY_KINDS = APPROVAL_KINDS + (
    'run_started', 'run_resumed', 'run_finished', 'task_failed', 'task_cancelled',
    'task_cancel_requested', 'execution_quarantined', 'tool_call', 'tool_result', 'tool_content',
    'permission_mode_changed', 'artifact_delivery', 'knowledge_sources', 'knowledge_retrieval',
)


async def visible_conversation(db, conversation_id, actor_id, token_version=None):
    query = select(Conversation).join(User, User.id == Conversation.user_id).where(
        Conversation.id == conversation_id, Conversation.user_id == actor_id,
        Conversation.deleted_at.is_(None), User.status == 'active',
    )
    if token_version is not None:
        query = query.where(User.token_version == token_version)
    return await db.scalar(query)


async def read_budget(db, conversation_id):
    # Calculate in PG before transferring JSON. Internal fields are counted
    # conservatively too; only public fields are serialized by the application.
    return (await db.execute(text('''
        SELECT coalesce(sum(n),0) AS rows, coalesce(sum(bytes),0) AS bytes FROM (
          SELECT count(*) AS n, coalesce(sum(octet_length(row_to_json(m)::text)),0) AS bytes
          FROM task_messages m WHERE m.conversation_id=:id
          UNION ALL
          SELECT count(*), coalesce(sum(octet_length(row_to_json(t)::text)),0)
          FROM tasks t WHERE t.conversation_id=:id
          UNION ALL
          SELECT count(*), coalesce(sum(octet_length(row_to_json(e)::text)),0)
          FROM task_events e WHERE e.conversation_id=:id AND e.event_type=ANY(CAST(:kinds AS text[]))
          UNION ALL
          SELECT count(*), coalesce(sum(octet_length(row_to_json(a)::text)
            +octet_length(row_to_json(d)::text)),0)
          FROM artifacts a JOIN task_artifact_deliveries d ON d.artifact_id=a.id
          WHERE a.conversation_id=:id
        ) budget
    '''), {'id': conversation_id, 'kinds': list(DISPLAY_KINDS)})).mappings().one()


async def messages(db, conversation_id):
    return list((await db.scalars(select(TaskMessage).where(
        TaskMessage.conversation_id == conversation_id,
    ).order_by(TaskMessage.created_at, TaskMessage.id))).all())


async def message_bytes(db, conversation_id, message_id):
    return await db.scalar(select(func.octet_length(cast(TaskMessage.content, Text))).where(
        TaskMessage.conversation_id == conversation_id, TaskMessage.id == message_id))


async def message(db, conversation_id, message_id):
    return await db.scalar(select(TaskMessage).where(
        TaskMessage.conversation_id == conversation_id, TaskMessage.id == message_id))


async def tasks(db, conversation_id):
    return list((await db.scalars(select(Task).where(
        Task.conversation_id == conversation_id,
    ).order_by(Task.created_at, Task.id))).all())


async def task(db, conversation_id, task_id):
    return await db.scalar(select(Task).where(
        Task.conversation_id == conversation_id, Task.id == task_id))


async def display_events(db, conversation_id):
    return list((await db.scalars(select(TaskEvent).where(
        TaskEvent.conversation_id == conversation_id, TaskEvent.event_type.in_(DISPLAY_KINDS),
    ).order_by(TaskEvent.id))).all())


async def approval_events(db, task_id):
    return list((await db.scalars(select(TaskEvent).where(
        TaskEvent.task_id == task_id, TaskEvent.event_type.in_(APPROVAL_KINDS),
    ).order_by(TaskEvent.id))).all())


async def artifacts(db, conversation_id):
    return list((await db.execute(select(Artifact, TaskArtifactDelivery).join(
        TaskArtifactDelivery, TaskArtifactDelivery.artifact_id == Artifact.id,
    ).where(Artifact.conversation_id == conversation_id).order_by(
        TaskArtifactDelivery.delivered_at, TaskArtifactDelivery.task_id, Artifact.id,
    ))).all())


async def active_binding(db, conversation_id):
    # Do not load Session.snapshot or Snapshot.payload: Core state is private
    # and may be large. The sole JSON value here is explicit display evidence.
    return (await db.execute(text('''
        SELECT c.active_product_task_id AS task_id, t.run_id, t.status AS task_status,
          s.id AS session_id, s.active_task_id AS session_task_id, s.owner_id,
          s.reply_id, s.execution_epoch, s.status, s.state_version, s.runtime_seq,
          coalesce(s.lease_until>clock_timestamp(),false) AS lease_live,
          e.session_id AS execution_session_id, e.reply_id AS execution_reply_id,
          e.execution_epoch AS execution_epoch_copy, e.status AS execution_status,
          e.state_version AS execution_state_version, e.snapshot_id AS execution_snapshot_id,
          s.snapshot_id, b.id AS boundary_id, b.task_id AS boundary_task_id,
          b.reply_id AS boundary_reply_id, b.execution_epoch AS boundary_epoch,
          b.state_version AS boundary_version, b.runtime_seq AS boundary_seq,
          b.status AS boundary_status, r.payload->>'type' AS boundary_type,
          r.payload->'display_empty' AS display_empty,
          r.payload->>'status' AS event_boundary_status,
          r.payload->>'reply_id' AS event_reply_id,
          EXISTS(SELECT 1 FROM runtime_commands k WHERE k.session_id=s.id
            AND k.task_id=t.id AND k.conversation_id=c.id AND k.kind='StartTask'
            AND k.status IN ('accepted','claimed')) AS start_pending,
          EXISTS(SELECT 1 FROM runtime_commands k WHERE k.session_id=s.id
            AND k.task_id=t.id AND k.conversation_id=c.id AND k.kind='StopTask'
            AND k.status IN ('accepted','claimed') AND k.expected_epoch=s.execution_epoch
            AND k.reply_id IS NULL) AS unstarted_stop_pending
        FROM conversations c
        LEFT JOIN tasks t ON t.id=c.active_product_task_id AND t.conversation_id=c.id
        LEFT JOIN runtime_sessions s ON s.conversation_id=c.id AND s.superseded_at IS NULL
        LEFT JOIN runtime_task_executions e ON e.task_id=t.id
        LEFT JOIN runtime_snapshots b ON b.id=s.snapshot_id AND b.session_id=s.id
        LEFT JOIN runtime_events r ON r.session_id=s.id AND r.task_id=t.id
          AND r.runtime_seq=b.runtime_seq AND r.execution_epoch=b.execution_epoch
        WHERE c.id=:id
    '''), {'id': conversation_id})).mappings().one()
