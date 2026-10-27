"""Display message persistence; callers own all locks and transaction boundaries."""
from sqlalchemy import select

from workmate_contracts.realtime import MessageSnapshot, decode_cursor, encode_cursor
from ..models import TaskMessage


class SnapshotConflict(ValueError):
    """A display cut disagrees with already committed content or coverage."""


def from_message(row):
    return MessageSnapshot(message_id=row.id,conversation_id=row.conversation_id,task_id=row.task_id,
        reply_id=row.snapshot_reply_id,execution_epoch=row.snapshot_execution_epoch,
        snapshot_version=row.snapshot_version,blocks=row.content['blocks'],
        covered_cursor=decode_cursor(row.covered_cursor) if row.covered_cursor else None,
        business_seq=row.snapshot_business_seq,phase=row.snapshot_phase,
        incomplete=row.snapshot_incomplete,process_end=row.content.get('snapshot_process_end'))


def _advance(previous,current,*,unanchored=False):
    if previous.incomplete and not current.incomplete:
        raise SnapshotConflict('Appending content cannot clear an incomplete message')
    if (current.snapshot_version<=previous.snapshot_version
            or current.execution_epoch<previous.execution_epoch
            or current.business_seq<previous.business_seq
            or previous.phase in {'final','interrupted'}):
        raise SnapshotConflict('Message snapshot cannot regress or reopen a terminal message')
    if (len(current.blocks)<len(previous.blocks)
            or current.reply_id!=previous.reply_id):
        raise SnapshotConflict('Message snapshot lost its original blocks or Reply')
    for old,new in zip(previous.blocks,current.blocks):
        if (old.block_id!=new.block_id or old.kind!=new.kind
                or new.content_version<old.content_version
                or not new.text.startswith(old.text)
                or (new.text!=old.text and new.content_version==old.content_version)
                or (old.closed and new!=old)):
            raise SnapshotConflict('Message block version or content changed inconsistently')
    if previous.covered_cursor is not None:
        position=current.covered_cursor
        if position is None and not unanchored:
            raise SnapshotConflict('Message snapshot cannot lose its covered position')
        if position is not None and position.generation==previous.covered_cursor.generation:
            before=tuple(map(int,previous.covered_cursor.stream_id.split('-')))
            after=tuple(map(int,position.stream_id.split('-')))
            if after<before:
                raise SnapshotConflict('Covered position moved backwards')


async def write_message(db, snapshot: MessageSnapshot, *, unanchored=False):
    if type(unanchored) is not bool or (unanchored and snapshot.covered_cursor is not None):
        raise SnapshotConflict('Unanchored mode requires an absent covered cursor')
    row=await db.scalar(select(TaskMessage).where(TaskMessage.id==snapshot.message_id)
                       .with_for_update().execution_options(populate_existing=True))
    if row is not None:
        if (row.role!='assistant' or row.task_id!=snapshot.task_id
                or row.conversation_id!=snapshot.conversation_id or row.snapshot_version==0):
            raise SnapshotConflict('Stable message identity is already used by different content')
        previous=from_message(row)
        if previous==snapshot:
            return previous
        _advance(previous,snapshot,unanchored=unanchored)
    else:
        row=TaskMessage(id=snapshot.message_id,conversation_id=snapshot.conversation_id,
                        task_id=snapshot.task_id,role='assistant')
        db.add(row)
    raw_text=''.join(block.text for block in snapshot.blocks if block.kind=='text')
    final=snapshot.phase in {'final','interrupted'}
    text=raw_text.strip() if final else raw_text
    process_end=snapshot.process_end or 0
    if final:
        process_end=max(0,process_end-(len(raw_text)-len(raw_text.lstrip())))
    row.content={'text':text,'blocks':[block.model_dump(mode='json') for block in snapshot.blocks],
        'presentation':{'version':1,'process_end':min(len(text),process_end)},
        'snapshot_process_end':snapshot.process_end}
    row.snapshot_version=snapshot.snapshot_version
    row.snapshot_reply_id=snapshot.reply_id
    row.snapshot_execution_epoch=snapshot.execution_epoch
    row.covered_cursor=encode_cursor(snapshot.covered_cursor) if snapshot.covered_cursor else None
    row.snapshot_business_seq=snapshot.business_seq
    row.snapshot_phase=snapshot.phase
    row.snapshot_incomplete=snapshot.incomplete
    await db.flush()
    return snapshot
