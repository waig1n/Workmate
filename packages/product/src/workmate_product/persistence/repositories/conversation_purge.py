"""Content purge persistence; caller owns the transaction and storage exclusion."""
from sqlalchemy import select, delete, update, text, null
from ..models import (Conversation, ConversationRetirement, Task, TaskMessage, Artifact,
    TaskArtifactDelivery, TaskEvent, ToolExecutionReceipt, RuntimeSession, RuntimeTaskExecution,
    RuntimeCommand, RuntimeSnapshot, RuntimeExecutionSpec, RuntimeExternalReceipt, RuntimeSuspension,
    RuntimeDeliveryBaseline, RuntimeDeliveryCapture, RuntimeDeliverySeal, RuntimeEvent,
    ProductEvent, TaskProjectionState, ACTIVE_TASK_STATUSES, WorkspaceResidency, WorkspaceReleaseRequest,
    ConversationEventLog, RealtimeBusinessOutbox, WorkspaceCheckpoint)
from ..recovery_forks import RecoveryFork

# Shared across captures; exclusive for GC. Acquire BEFORE Product row locks.
ARTIFACT_STORAGE_LOCK = 706193248127


async def lock_purge(db, conversation_id):
    if not db.in_transaction() or db.bind.dialect.name != 'postgresql':
        raise RuntimeError('Purge requires a caller-owned PostgreSQL transaction')
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': ARTIFACT_STORAGE_LOCK})
    conv = await db.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
    row = await db.scalar(select(ConversationRetirement).where(
        ConversationRetirement.conversation_id == conversation_id).with_for_update())
    if conv is None or row is None or conv.deleted_at is None or row.completed_at is None:
        raise RuntimeError('Purge requires a completed retirement')
    if row.purged_at is not None:
        return conv, row
    session = await db.scalar(select(RuntimeSession).where(RuntimeSession.id == row.session_id).with_for_update()) if row.session_id else None
    historical = await db.scalar(select(RuntimeSession.id).where(
        RuntimeSession.conversation_id == conversation_id,
        RuntimeSession.id != row.session_id).limit(1))
    if historical is not None:
        raise RuntimeError('Purge requires retirement evidence for every historical Workspace')
    residency = await db.scalar(select(WorkspaceResidency).where(
        WorkspaceResidency.session_id == row.session_id).with_for_update()) if row.session_id else None
    if residency is not None and residency.status != 'released':
        raise RuntimeError('Purge requires released Workspace resources')
    unresolved_release = await db.scalar(select(WorkspaceReleaseRequest.id).where(
        WorkspaceReleaseRequest.session_id == row.session_id,
        WorkspaceReleaseRequest.status.not_in(('released','stale'))).limit(1))
    if unresolved_release is not None:
        raise RuntimeError('Purge requires resolved Workspace release evidence')
    active = await db.scalar(select(Task.id).where(Task.conversation_id == conversation_id,
        Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
    unresolved = await db.scalar(select(RuntimeCommand.id).where(RuntimeCommand.conversation_id == conversation_id,
        RuntimeCommand.status.in_(['accepted', 'claimed', 'indeterminate'])).limit(1))
    if (conv.active_product_task_id or active or unresolved or (session and (
            session.active_task_id or session.execution_epoch != row.execution_epoch
            or session.storage_node_id != row.storage_node_id))):
        raise RuntimeError('Purge requires closed execution and delivery gates')
    return conv, row


async def object_keys(db, conversation_id, *, others=False):
    comparison = '<>' if others else '='
    # Project only keys in PostgreSQL; never pull other conversations' full
    # delivery payloads into the deleting Worker's memory.
    query = text(f"""SELECT storage_key FROM artifacts WHERE conversation_id {comparison} :cid
        UNION SELECT item->>'storage_key' FROM runtime_delivery_seals d
        JOIN tasks t ON t.id=d.task_id,
        LATERAL jsonb_array_elements(COALESCE(d.payload->'capture'->'files','[]'::jsonb)) item
        WHERE t.conversation_id {comparison} :cid
        UNION SELECT item->>'storage_key' FROM runtime_delivery_captures d
        JOIN tasks t ON t.id=d.task_id,
        LATERAL jsonb_array_elements(COALESCE(d.payload->'files','[]'::jsonb)) item
        WHERE t.conversation_id {comparison} :cid
        UNION SELECT item->>'storage_key' FROM workspace_checkpoints c,
        LATERAL jsonb_array_elements(COALESCE(c.manifest->'files','[]'::jsonb)) item
        WHERE c.conversation_id {comparison} :cid
        UNION SELECT item->>'storage_key' FROM recovery_forks f,
        LATERAL jsonb_array_elements(COALESCE(f.manifest->'files','[]'::jsonb)) item
        WHERE f.conversation_id {comparison} :cid""")
    return set(await db.scalars(query, {'cid': conversation_id}))


async def delete_content(db, conversation_id, *, redact_audit):
    tasks = select(Task.id).where(Task.conversation_id == conversation_id)
    artifacts = select(Artifact.id).where(Artifact.conversation_id == conversation_id)
    sessions = select(RuntimeSession.id).where(RuntimeSession.conversation_id == conversation_id)
    residences = list(await db.scalars(select(WorkspaceResidency).where(
        WorkspaceResidency.session_id.in_(sessions)).with_for_update()))
    if any(row.status != 'released' for row in residences):
        raise RuntimeError('Purge cannot erase unresolved Workspace resources')
    unresolved_release = await db.scalar(select(WorkspaceReleaseRequest.id).where(
        WorkspaceReleaseRequest.session_id.in_(sessions),
        WorkspaceReleaseRequest.status.not_in(('released','stale'))).limit(1))
    if unresolved_release is not None:
        raise RuntimeError('Purge cannot erase unresolved Workspace release evidence')
    # Publication receipts acquire log SHARE before outbox UPDATE. Keep that
    # order here so a publisher can finish its receipt while purge waits.
    await db.execute(select(ConversationEventLog).where(
        ConversationEventLog.conversation_id == conversation_id).with_for_update())
    await db.execute(delete(RealtimeBusinessOutbox).where(
        RealtimeBusinessOutbox.conversation_id == conversation_id))
    await db.execute(delete(ConversationEventLog).where(
        ConversationEventLog.conversation_id == conversation_id))
    await db.execute(delete(WorkspaceReleaseRequest).where(WorkspaceReleaseRequest.session_id.in_(sessions)))
    await db.execute(delete(WorkspaceResidency).where(WorkspaceResidency.session_id.in_(sessions)))
    # Recovery forks own an independent copy of their manifest. Dropping the
    # source checkpoint must not invalidate or erase another conversation's files.
    checkpoints = select(WorkspaceCheckpoint.id).where(WorkspaceCheckpoint.conversation_id == conversation_id)
    await db.execute(update(RecoveryFork).where(RecoveryFork.checkpoint_id.in_(checkpoints)).values(checkpoint_id=None))
    await db.execute(update(RecoveryFork).where(RecoveryFork.conversation_id == conversation_id).values(manifest={}, context_text=''))
    await db.execute(delete(WorkspaceCheckpoint).where(WorkspaceCheckpoint.conversation_id == conversation_id))
    await db.execute(delete(TaskArtifactDelivery).where(TaskArtifactDelivery.artifact_id.in_(artifacts)))
    for model in (Artifact, TaskMessage, ProductEvent):
        await db.execute(delete(model).where(model.conversation_id == conversation_id))
    for model in (RuntimeSuspension, TaskProjectionState, RuntimeEvent, RuntimeDeliveryCapture, RuntimeDeliverySeal,
                  RuntimeDeliveryBaseline, RuntimeExternalReceipt, RuntimeExecutionSpec, ToolExecutionReceipt):
        await db.execute(delete(model).where(model.task_id.in_(tasks)))
    await db.execute(update(RuntimeTaskExecution).where(RuntimeTaskExecution.task_id.in_(tasks))
        .values(snapshot_id=None, input_snapshot_id=None))
    await db.execute(update(RuntimeSession).where(RuntimeSession.id.in_(sessions)).values(snapshot_id=None, snapshot=null()))
    await db.execute(delete(RuntimeSnapshot).where(RuntimeSnapshot.session_id.in_(sessions)))
    # Keep audit identities/status/timing, erase conversation text and tool
    # arguments/results rather than retaining a second copy in replay/audit.
    events = await db.scalars(select(TaskEvent).where(TaskEvent.conversation_id == conversation_id))
    for event in events:
        event.payload = redact_audit(event.payload or {})
    await db.execute(update(RuntimeCommand).where(RuntimeCommand.conversation_id == conversation_id)
        .values(payload={'purged': True}))
    await db.execute(update(Task).where(Task.conversation_id == conversation_id).values(
        title='Deleted conversation', error=null(), artifact_output_snapshot=null(),
        artifact_presentation_snapshot=None))
    await db.execute(update(Conversation).where(Conversation.id == conversation_id).values(title='Deleted conversation'))
