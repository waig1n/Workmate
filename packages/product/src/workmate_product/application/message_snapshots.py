"""Persist a captured display cut, never a later observed Stream high watermark."""
from sqlalchemy import text

from workmate_contracts.realtime import MessageSnapshot
from ..persistence.repositories.message_snapshots import SnapshotConflict, write_message
from ..projections.store import lock_product_conversation


class SnapshotRejected(SnapshotConflict):
    """The current owner no longer has permission to save this display cut."""


async def save_message_snapshot(db, snapshot: MessageSnapshot, *, owner_id: str, unanchored: bool=False):
    """Save body without a transport claim only in explicit unanchored mode."""
    if not db.in_transaction() or db.bind.dialect.name!='postgresql':
        raise SnapshotRejected('Requires a caller-owned PostgreSQL transaction')
    # Copy/validate before acquiring locks; frozen Pydantic models can still hold
    # mutable lists or be constructed with unvalidated model_copy updates.
    snapshot=MessageSnapshot.model_validate(snapshot.model_dump(mode='json'))
    if type(unanchored) is not bool or (unanchored and snapshot.covered_cursor is not None):
        raise SnapshotRejected('Unanchored mode requires an absent covered cursor')
    async with db.begin_nested():
        return await _save(db,snapshot,owner_id=owner_id,unanchored=unanchored)


async def _save(db,snapshot,*,owner_id,unanchored):
    conversation=await lock_product_conversation(db,snapshot.conversation_id)
    task=(await db.execute(text('SELECT * FROM tasks WHERE id=:id FOR UPDATE'),
                           {'id':snapshot.task_id})).mappings().first()
    session=(await db.execute(text('''SELECT *,lease_until>clock_timestamp() AS live
        FROM runtime_sessions WHERE conversation_id=:cid AND superseded_at IS NULL FOR UPDATE'''),
        {'cid':snapshot.conversation_id})).mappings().first()
    execution=(await db.execute(text('SELECT * FROM runtime_task_executions WHERE task_id=:id FOR UPDATE'),
                               {'id':snapshot.task_id})).mappings().first()
    if (not owner_id or not task or not session or not execution
            or conversation['deleted_at'] is not None
            or conversation['active_product_task_id']!=snapshot.task_id
            or task['conversation_id']!=snapshot.conversation_id
            # Runtime binds first; the asynchronous Product start projection
            # may not have filled Task.run_id yet, but must never conflict.
            or (task['run_id'] is not None and task['run_id']!=snapshot.reply_id)
            or session['active_task_id']!=snapshot.task_id or session['owner_id']!=owner_id
            or session['reply_id']!=snapshot.reply_id
            or not session['live'] or session['execution_epoch']!=snapshot.execution_epoch
            or execution['session_id']!=session['id']
            or execution['reply_id']!=snapshot.reply_id
            or execution['execution_epoch']!=snapshot.execution_epoch
            or snapshot.business_seq>conversation['business_seq']):
        raise SnapshotRejected('Display snapshot has no current Task/Reply execution authority')
    log=(await db.execute(text('SELECT * FROM conversation_event_logs WHERE conversation_id=:cid FOR SHARE'),
                          {'cid':snapshot.conversation_id})).mappings().first()
    if not unanchored and log is not None and (log['status']!='active' or snapshot.covered_cursor is None
                           or snapshot.covered_cursor.generation!=log['generation']):
        raise SnapshotRejected('Display snapshot belongs to an unconfirmed or superseded log generation')
    try:
        result=await write_message(db,snapshot,unanchored=unanchored)
    except SnapshotConflict as error:
        raise SnapshotRejected(str(error)) from error
    if not await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id'),
                           {'id':session['id']}):
        raise SnapshotRejected('Execution lease expired during display snapshot persistence')
    return result
