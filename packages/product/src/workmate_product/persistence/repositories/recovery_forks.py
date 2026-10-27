"""Recovery reads and locks; caller owns the transaction."""
from sqlalchemy import select, text
from ..models import Conversation, Task, TaskMessage, RuntimeSession, Artifact
from ..recovery_forks import RecoveryFork


async def fork_for(db, conversation_id):
    return await db.scalar(select(RecoveryFork).join(RuntimeSession,RuntimeSession.id==RecoveryFork.target_session_id).where(
        RecoveryFork.conversation_id==conversation_id,RuntimeSession.superseded_at.is_(None)))


async def request_for(db, actor, key):
    return await db.scalar(select(RecoveryFork).where(RecoveryFork.actor_user_id==actor, RecoveryFork.request_key==key))


async def lock_request(db, actor, key):
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key':f'recovery:{actor}:{key}'})


async def source(db, conversation_id, user_id, *, lock=False):
    query=select(Conversation).where(Conversation.id==conversation_id,
        Conversation.user_id==user_id, Conversation.deleted_at.is_(None))
    if lock:
        query=query.with_for_update().execution_options(populate_existing=True)
    conv = await db.scalar(query)
    if conv is None:
        return None
    task_query=select(Task).where(Task.id==conv.active_product_task_id)
    task=await db.scalar(task_query.with_for_update().execution_options(populate_existing=True) if lock else task_query) if conv.active_product_task_id else None
    session_query=select(RuntimeSession).where(RuntimeSession.conversation_id==conv.id,RuntimeSession.superseded_at.is_(None))
    session=await db.scalar(session_query.with_for_update().execution_options(populate_existing=True) if lock else session_query)
    return conv,task,session


async def checkpoint_for(db, conversation_id, task):
    from ..checkpoint_models import WorkspaceCheckpoint
    return await db.scalar(select(WorkspaceCheckpoint).join(Task, Task.id==WorkspaceCheckpoint.task_id).where(
        WorkspaceCheckpoint.conversation_id==conversation_id, WorkspaceCheckpoint.status=='available',
        WorkspaceCheckpoint.session_id==select(RuntimeSession.id).where(RuntimeSession.conversation_id==conversation_id,RuntimeSession.superseded_at.is_(None)).scalar_subquery(),
        Task.created_at<=task.created_at).order_by(WorkspaceCheckpoint.created_at.desc()).limit(1))


async def messages_for(db, conversation_id, task):
    return list((await db.scalars(select(TaskMessage).join(Task,Task.id==TaskMessage.task_id).where(
        TaskMessage.conversation_id==conversation_id,Task.created_at<=task.created_at)
        .order_by(TaskMessage.created_at,TaskMessage.id))).all())


async def artifacts_for(db, conversation_id, task):
    return list((await db.scalars(select(Artifact).join(Task,Task.id==Artifact.task_id).where(
        Artifact.conversation_id==conversation_id,Task.created_at<=task.created_at)
        .order_by(Artifact.created_at,Artifact.id))).all())


async def fork_manifest_digest(db, task_id):
    return await db.scalar(text("SELECT payload->>'recovery_manifest_sha256' FROM runtime_execution_specs WHERE task_id=:id"),{'id':task_id})


async def replace_current(db, conversation, old_session, new_session, *, target_node):
    from sqlalchemy import func
    old_session.superseded_at=func.now()
    await db.flush()
    db.add(RuntimeSession(id=new_session,conversation_id=conversation.id,storage_node_id=target_node))
    conversation.active_product_task_id=None
    await db.flush()
    await db.execute(text("""INSERT INTO runtime_projection_cursors(conversation_id,session_id,last_runtime_seq,version)
        VALUES (:cid,:sid,0,1) ON CONFLICT(conversation_id) DO UPDATE
        SET session_id=EXCLUDED.session_id,last_runtime_seq=0,version=runtime_projection_cursors.version+1"""),
        {'cid':conversation.id,'sid':new_session})


async def has_queued(db, conversation_id):
    return await db.scalar(select(Task.id).where(Task.conversation_id==conversation_id,Task.status=='created').limit(1))
