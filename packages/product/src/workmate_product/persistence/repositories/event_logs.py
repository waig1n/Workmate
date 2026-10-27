"""PG log-generation fencing on caller-owned transactions; no transport I/O."""
from dataclasses import dataclass
import uuid

from sqlalchemy import text
from ...projections.store import lock_product_conversation


class LogUnavailable(ValueError):
    """A removed Conversation or an unresolved rotation cannot expose a live log."""


@dataclass(frozen=True)
class LogState:
    conversation_id: str
    generation: str
    version: int
    status: str
    token: str
    incarnation: str | None
    live: bool

    @property
    def identity(self):
        if self.status!='active' or self.incarnation is None:
            raise LogUnavailable('Log has not been activated')
        return self.conversation_id,self.generation,self.incarnation


def _transaction(db):
    if not db.in_transaction() or db.bind.dialect.name!='postgresql':
        raise RuntimeError('Event logs require a caller-owned PostgreSQL transaction')


def _state(row):
    return LogState(row['conversation_id'],row['generation'],row['version'],row['status'],
                    row['initialization_token'],row['incarnation'],bool(row['live']))


def _same(state,expected):
    if isinstance(expected,tuple):
        return state.status=='active' and state.identity==expected
    return (state.conversation_id,state.generation,state.version,state.token,state.status)==(
        expected.conversation_id,expected.generation,expected.version,expected.token,expected.status)


async def _row(db,conversation_id,*,lock=False):
    return (await db.execute(text('''SELECT *,lease_until>clock_timestamp() AS live
        FROM conversation_event_logs WHERE conversation_id=:cid'''+(' FOR UPDATE' if lock else '')),
        {'cid':conversation_id})).mappings().first()


async def read(db,conversation_id):
    _transaction(db)
    row=(await db.execute(text('''SELECT logs.*,logs.lease_until>clock_timestamp() AS live
        FROM conversations AS conversation
        LEFT JOIN conversation_event_logs AS logs ON logs.conversation_id=conversation.id
        WHERE conversation.id=:cid AND conversation.deleted_at IS NULL'''),
        {'cid':conversation_id})).mappings().first()
    if row is None:
        raise LogUnavailable('Conversation is unavailable')
    return _state(row) if row['conversation_id'] is not None else None


async def prepare(db,conversation_id,*,lease_seconds,expected=None):
    _transaction(db)
    if type(lease_seconds) is not int or not 3<=lease_seconds<=300:
        raise ValueError('Log initialization lease must be from 3 to 300 seconds')
    conversation=await lock_product_conversation(db,conversation_id)
    if conversation['deleted_at'] is not None:
        raise LogUnavailable('Conversation is unavailable')
    row=await _row(db,conversation_id,lock=True)
    current=_state(row) if row else None
    if current is not None:
        if expected is not None and not _same(current,expected):
            return current
        if expected is None and (current.status=='active' or current.live):
            return current
    values={'cid':conversation_id,'generation':str(uuid.uuid4()),'token':str(uuid.uuid4()),
            'version':current.version+1 if current else 1,'seconds':lease_seconds}
    row=(await db.execute(text('''INSERT INTO conversation_event_logs
        (conversation_id,generation,version,status,initialization_token,lease_until)
        VALUES (:cid,:generation,:version,'initializing',:token,
            clock_timestamp()+make_interval(secs=>:seconds))
        ON CONFLICT (conversation_id) DO UPDATE SET generation=EXCLUDED.generation,
            version=EXCLUDED.version,status='initializing',initialization_token=EXCLUDED.initialization_token,
            lease_until=EXCLUDED.lease_until,incarnation=NULL,updated_at=clock_timestamp()
        RETURNING *,true AS live'''),values)).mappings().one()
    return _state(row)


async def activate(db,state,identity):
    _transaction(db)
    conversation_id,generation,incarnation=identity
    if (state.conversation_id,state.generation)!=(conversation_id,generation):
        return False
    conversation=await lock_product_conversation(db,state.conversation_id)
    if conversation['deleted_at'] is not None:
        return False
    row=await _row(db,state.conversation_id,lock=True)
    if row is None:
        return False
    current=_state(row)
    if (current.generation,current.version,current.token)!=(state.generation,state.version,state.token):
        return False
    if current.status=='active':
        return current.identity==identity
    if not current.live:
        return False
    result=await db.execute(text('''UPDATE conversation_event_logs SET status='active',incarnation=:incarnation,
        lease_until=NULL,updated_at=clock_timestamp() WHERE conversation_id=:cid
        AND status='initializing' AND lease_until>clock_timestamp()'''),
        {'cid':conversation_id,'incarnation':incarnation})
    return result.rowcount==1
