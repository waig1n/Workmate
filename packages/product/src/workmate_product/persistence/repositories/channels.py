"""Channel SQL and locks within the caller-owned transaction."""
from datetime import timedelta
from sqlalchemy import select, func, or_, delete, update, exists
from ..models import (ChannelConnection as Connection, ChannelIdentity as Identity,
    ChannelBindingCode as Code, ChannelConversation as Mapping, ChannelInbox as Inbox,
    ChannelOutbox as Outbox, Scope, Agent, Conversation, Task, TaskMessage, Artifact, TaskArtifactDelivery)


async def now(db):
    return await db.scalar(select(func.clock_timestamp()))


async def channel_by_id(db, channel_id, *, lock=False):
    query = select(Connection).where(Connection.id == channel_id)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def root(db):
    return await db.scalar(select(Scope).where(Scope.kind == 'organization'))


async def connections(db, user_id=None, *, enabled=False):
    query = select(Connection).where(Connection.deleted_at.is_(None))
    if user_id is not None:
        query = query.where(or_(Connection.owner_user_id.is_(None), Connection.owner_user_id == user_id))
    if enabled:
        query = query.where(Connection.enabled.is_(True))
    return list((await db.scalars(query.order_by(Connection.created_at, Connection.id))).all())


async def identity(db, channel_id, external_user_id):
    return await db.scalar(select(Identity).where(Identity.channel_id == channel_id,
        Identity.external_user_id == external_user_id).execution_options(populate_existing=True))


async def identities(db, user_id):
    return list((await db.scalars(select(Identity).where(Identity.user_id == user_id))).all())


async def codes_count(db, user_id, instant):
    return await db.scalar(select(func.count()).select_from(Code).where(Code.user_id == user_id,
        Code.expires_at > instant))


async def expire_codes(db, user_id, channel_id, instant):
    await db.execute(update(Code).where(Code.user_id == user_id, Code.channel_id == channel_id,
        Code.consumed_at.is_(None)).values(consumed_at=instant))


async def code(db, digest):
    return await db.scalar(select(Code).where(Code.code_hash == digest).with_for_update())


async def inbox_existing(db, channel_id, message_id):
    return await db.scalar(select(Inbox).where(Inbox.channel_id == channel_id, Inbox.message_id == message_id))


async def pending_count(db, channel_id):
    return await db.scalar(select(func.count()).select_from(Inbox).where(Inbox.channel_id == channel_id,
        Inbox.status == 'pending'))


async def pending_deliveries(db, channel_id):
    return await db.scalar(select(func.count()).select_from(Outbox).where(
        Outbox.channel_id == channel_id, Outbox.status.in_(['pending','sending'])))


async def pending_ids(db, limit):
    return list((await db.scalars(select(Inbox.id).where(Inbox.status == 'pending')
        .order_by(Inbox.created_at, Inbox.id).limit(limit))).all())


async def locked_inbox(db, inbox_id):
    return await db.scalar(select(Inbox).where(Inbox.id == inbox_id).with_for_update()
        .execution_options(populate_existing=True))


async def binding_attempts(db, row, instant):
    return await db.scalar(select(func.count()).select_from(Inbox).where(
        Inbox.channel_id == row.channel_id, Inbox.sender_id == row.sender_id,
        Inbox.text.like('/bind%'), Inbox.created_at > instant - timedelta(minutes=5)))


async def mapping(db, channel_id, user_id, agent_id, chat_key):
    return await db.scalar(select(Mapping).where(Mapping.channel_id == channel_id,
        Mapping.user_id == user_id, Mapping.agent_id == agent_id, Mapping.chat_key == chat_key)
        .with_for_update().execution_options(populate_existing=True))


async def latest_task(db, conversation_id):
    return await db.scalar(select(Task).where(Task.conversation_id == conversation_id)
        .order_by(Task.created_at.desc(), Task.id.desc()).limit(1))


async def outbox_exists(db, key):
    return await db.scalar(select(Outbox.id).where(Outbox.dedupe_key == key))


async def projection_candidates(db, limit):
    # Observable state is independent of the Runtime delivery gate. Finals wait
    # until that task has released its Product delivery pointer.
    key = Inbox.id + ':' + Task.status + ':' + func.coalesce(Task.last_decision_version, '')
    delivered = exists(select(Outbox.id).where(or_(Outbox.dedupe_key == key, Outbox.boundary_key == key)))
    query = (select(Inbox.id).join(Task, Task.id == Inbox.task_id)
        .join(Conversation, Conversation.id == Task.conversation_id)
        .where(Inbox.status == 'processed', ~delivered,
            or_(Task.status.in_(['awaiting_permission','awaiting_external_result']),
                Task.status.in_(['completed','failed','cancelled','interrupted']) &
                or_(Conversation.active_product_task_id.is_(None), Conversation.active_product_task_id != Task.id)))
        .order_by(Inbox.created_at).limit(limit))
    return list((await db.scalars(query)).all())


async def final_text(db, task_id):
    rows = (await db.scalars(select(TaskMessage).where(TaskMessage.task_id == task_id,
        TaskMessage.role == 'assistant').order_by(TaskMessage.created_at))).all()
    parts = []
    for row in rows:
        if not isinstance(row.content, dict):
            continue
        text = row.content.get('text', '')
        end = (row.content.get('presentation') or {}).get('process_end', 0)
        if isinstance(text, str) and isinstance(end, int) and 0 <= end <= len(text):
            parts.append(text[end:])
    return '\n\n'.join(parts)


async def has_artifacts(db, task_id):
    return bool(await db.scalar(select(or_(
        exists(select(Artifact.id).where(Artifact.task_id == task_id)),
        exists(select(TaskArtifactDelivery.artifact_id).where(TaskArtifactDelivery.task_id == task_id))))))


async def claim_connection(db, channel_id, owner, seconds):
    row = await channel_by_id(db, channel_id, lock=True)
    instant = await now(db)
    if not row or row.deleted_at or not row.enabled:
        return None
    if row.lease_owner != owner and row.lease_until and row.lease_until > instant:
        return None
    if row.lease_owner != owner or not row.lease_until or row.lease_until <= instant:
        row.lease_epoch += 1
    row.lease_owner, row.lease_until = owner, instant + timedelta(seconds=seconds)
    await db.flush()
    return row


async def lease_valid(db, channel_id, owner, epoch, version):
    row = await channel_by_id(db, channel_id, lock=True)
    instant = await now(db)
    if (row and row.enabled and not row.deleted_at and row.lease_owner == owner
            and row.lease_epoch == epoch and row.version == version
            and row.lease_until and row.lease_until > instant):
        return row
    return None


async def next_outbox(db, channel_id, owner, lease_secs):
    instant = await now(db)
    await db.execute(update(Outbox).where(Outbox.channel_id == channel_id, Outbox.status == 'sending',
        Outbox.lease_until < instant).values(status='unknown', last_error='发送回执未知，请核实后重试'))
    row = await db.scalar(select(Outbox).where(Outbox.channel_id == channel_id,
        Outbox.status == 'pending', Outbox.next_attempt_at <= instant)
        .order_by(Outbox.created_at, Outbox.id).limit(1).with_for_update(skip_locked=True))
    if row:
        row.status, row.lease_owner = 'sending', owner
        row.lease_until = instant + timedelta(seconds=lease_secs)
        row.attempts += 1
    return row


async def delivery_page(db, channel_id, user_id, limit=20, offset=0):
    return list((await db.scalars(select(Outbox).where(Outbox.channel_id == channel_id,
        Outbox.user_id == user_id).order_by(Outbox.created_at.desc())
        .limit(limit).offset(offset))).all())


async def finish_delivery(db, row_id, owner, status, *, receipt=None, error=None,
                          revision=None, state=None, interval=1):
    row = await db.scalar(select(Outbox).where(Outbox.id == row_id).with_for_update()
                          .execution_options(populate_existing=True))
    if not row or row.status != 'sending' or row.lease_owner != owner:
        return
    row.status, row.last_error = status, error
    if status == 'sent':
        row.platform_message_id = receipt
        row.sent_at = await now(db)
        if state is not None:
            row.reply_state = {**row.reply_state, **state,
                'body_seen': bool(row.reply_state.get('body_seen') or state.get('body_seen'))}
        if revision is not None and revision != row.reply_revision:
            row.status = 'pending'
            row.next_attempt_at = max(row.next_attempt_at, row.sent_at + timedelta(seconds=interval))


async def reply_slot(db, inbox_id):
    return await db.scalar(select(Outbox).where(Outbox.inbox_id == inbox_id, Outbox.kind == 'reply')
        .with_for_update().execution_options(populate_existing=True))


async def live_replies(db, channel_id, limit, *, include_plain=False):
    return list((await db.scalars(select(Inbox).join(Task, Task.id == Inbox.task_id)
        .join(Outbox, (Outbox.inbox_id == Inbox.id) & (Outbox.kind == 'reply'))
        .where(Inbox.channel_id == channel_id, Inbox.chat_type == 'private', Inbox.status == 'processed',
               Task.status.in_(['created','running']), Outbox.status.in_(['pending','sending','sent']),
               or_(include_plain, func.coalesce(Outbox.reply_state['mode'].as_string(), '') != 'plain'))
        .order_by(Inbox.created_at, Inbox.id).limit(limit))).all())


async def release(db, channel_id, owner):
    await db.execute(update(Connection).where(Connection.id == channel_id,
        Connection.lease_owner == owner).values(lease_owner=None, lease_until=None, status='stopped'))


async def agent_shared_to_scope(db, agent_id, scope_id):
    from ..models import ResourceShare
    return bool(await db.scalar(select(ResourceShare.id).where(
        ResourceShare.agent_id == agent_id, ResourceShare.target_scope_id == scope_id).limit(1)))
