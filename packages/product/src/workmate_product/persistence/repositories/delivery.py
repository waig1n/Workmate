"""Delivery facts using the caller's PostgreSQL transaction.

Caller locks Agent/Conversation before Task/projection and Session/execution.
These operations neither commit nor open transactions; Artifact insertion flushes
at the original point before audit/replay. Gate release follows receipt storage.
"""
import json
from sqlalchemy import text, select
from workmate_product.persistence.models import Artifact


async def append_delivery_audit(db,cid,tid,kind,payload):
    await db.execute(text('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
        SELECT CAST(:tid AS varchar),CAST(:cid AS varchar),coalesce(max(seq),0)+1,CAST(:kind AS varchar),CAST(:payload AS json) FROM task_events WHERE task_id=:tid'''),
        {'tid':tid,'cid':cid,'kind':kind,'payload':json.dumps(payload)})


async def lock_task(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM tasks WHERE id=:id FOR UPDATE'), params)).mappings().first()


async def read_capture(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM runtime_delivery_captures WHERE task_id=:id'), params)).mappings().first()


async def lock_projection(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM task_projection_states WHERE task_id=:id FOR UPDATE'), params)).mappings().first()


async def lock_session(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM runtime_sessions WHERE id=:id FOR UPDATE'), params)).mappings().first()


async def lock_execution(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM runtime_task_executions WHERE task_id=:id FOR UPDATE'), params)).mappings().first()


async def read_snapshot(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM runtime_snapshots WHERE id=:id'), params)).mappings().first()


async def has_unsettled_commands(db, *, id):
    params = {'id': id}
    return await db.scalar(text("SELECT 1 FROM runtime_commands WHERE task_id=:id\n                AND status IN ('accepted','claimed','indeterminate') LIMIT 1"), params)


async def record_closure(db, *, task_id, conversation_id, session_id, state):
    """Store the validated receipt, then release Product and Runtime gates.

    Caller must hold the Conversation, Task, projection and Session locks and
    have verified terminal execution and settled commands. These three writes
    join that transaction; no commit or new transaction occurs here.
    """
    await db.execute(text('UPDATE task_projection_states SET state=CAST(:state AS jsonb) WHERE task_id=:id'),
        {'state': json.dumps(state), 'id': task_id})
    await db.execute(text('UPDATE conversations SET active_product_task_id=NULL WHERE id=:id'),
        {'id': conversation_id})
    await db.execute(text('UPDATE runtime_sessions SET active_task_id=NULL,owner_id=NULL,lease_until=NULL WHERE id=:id'),
        {'id': session_id})


async def latest_artifact(db, *, cid, name):
    # Caller holds the Conversation lock, serializing revision allocation.
    return await db.scalar(select(Artifact).where(
        Artifact.conversation_id == cid, Artifact.name == name,
    ).order_by(Artifact.version.desc(), Artifact.created_at.desc(), Artifact.id.desc()).limit(1))


async def read_runtime_event(db, *, id):
    params = {'id': id}
    return (await db.execute(text('SELECT * FROM runtime_events WHERE id=:id'), params)).mappings().first()


async def insert_artifact(db, artifact):
    """Flush before producing its created_at and audit/replay facts."""
    db.add(artifact)
    await db.flush()


async def insert_delivery(db, delivery):
    db.add(delivery)
    await db.flush()
