"""SQL for trusted node evidence, using only the caller's transaction.

Application owns authority verification, savepoints, lock ordering and proof
validation. Reads must remain between the same validations: even database time
is sampled at the proof boundary, never eagerly prefetched. No ownership transfer
or execution dispatch occurs here.
"""
from sqlalchemy import text


async def task_conversation(db, *, id):
    result = await db.execute(text('SELECT conversation_id FROM tasks WHERE id=:id'), {'id': id})
    return result.mappings().first()


async def conversation_agent(db, *, id):
    result = await db.execute(text('SELECT agent_id FROM conversations WHERE id=:id'), {'id': id})
    return result.mappings().first()


async def lock_execution_owner(db, *, agent_id, conversation_id, task_id):
    """Lock Agent → Conversation → Task → Session → execution, in that order."""
    result = await db.execute(text('SELECT id FROM agents WHERE id=:id FOR SHARE'), {'id': agent_id})
    result.mappings().first()
    result = await db.execute(text('SELECT * FROM conversations WHERE id=:id FOR UPDATE'), {'id': conversation_id})
    conversation = result.mappings().first()
    result = await db.execute(text('SELECT * FROM tasks WHERE id=:id FOR UPDATE'), {'id': task_id})
    task = result.mappings().first()
    result = await db.execute(text('SELECT * FROM runtime_sessions WHERE id=(SELECT session_id FROM runtime_task_executions WHERE task_id=:id) FOR UPDATE'), {'id': task_id})
    session = result.mappings().first()
    result = await db.execute(text('SELECT * FROM runtime_task_executions WHERE task_id=:id FOR UPDATE'), {'id': task_id})
    execution = result.mappings().first()
    return conversation, task, session, execution


async def owner_is_live(db, params):
    return await db.scalar(text('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=:id'), params)


async def record_incarnation(db, params):
    """Insert once and return the durable identity for Application comparison."""
    await db.execute(text("""INSERT INTO runtime_worker_incarnations(worker_id,payload,digest)
            VALUES (:id,CAST(:payload AS jsonb),:digest) ON CONFLICT(worker_id) DO NOTHING"""), params)
    result = await db.execute(text('SELECT digest FROM runtime_worker_incarnations WHERE worker_id=:id'), {'id': params['id']})
    return result.mappings().first()


async def worker_incarnation(db, *, id):
    result = await db.execute(text('SELECT * FROM runtime_worker_incarnations WHERE worker_id=:id'), {'id': id})
    return result.mappings().first()


async def lock_resource_ledger(db, *, tid, epoch):
    result = await db.execute(text('SELECT * FROM runtime_resource_ledgers WHERE task_id=:tid AND execution_epoch=:epoch FOR UPDATE'), {'tid': tid, 'epoch': epoch})
    return result.mappings().first()


async def execution_has_started(db, params):
    return await db.scalar(text("""SELECT EXISTS(SELECT 1 FROM runtime_events WHERE task_id=:tid)
                    OR EXISTS(SELECT 1 FROM runtime_sandbox_attestations WHERE task_id=:tid)
                    OR EXISTS(SELECT 1 FROM runtime_delivery_baselines WHERE task_id=:tid)"""), params)


async def insert_resource_ledger(db, params):
    await db.execute(text("""INSERT INTO runtime_resource_ledgers
            (task_id,execution_epoch,worker_id,session_id,workspace,workspace_digest)
            VALUES (:tid,:epoch,:worker,:sid,CAST(:workspace AS jsonb),:digest)"""), params)


async def registration_conflict(db, *, tid, epoch, rid, seq):
    result = await db.execute(text("""SELECT digest FROM runtime_resource_registrations WHERE task_id=:tid
            AND execution_epoch=:epoch AND (resource_id=:rid OR registration_seq=:seq)"""), {'tid': tid, 'epoch': epoch, 'rid': rid, 'seq': seq})
    return result.mappings().first()


async def record_registration(db, params):
    """Append the verified registration and advance its already-locked ledger."""
    await db.execute(text("""INSERT INTO runtime_resource_registrations
            (task_id,execution_epoch,registration_seq,resource_id,payload,digest)
            VALUES (:tid,:epoch,:seq,:rid,CAST(:payload AS jsonb),:digest)"""), params)
    await db.execute(text('UPDATE runtime_resource_ledgers SET resource_seq=:seq WHERE task_id=:tid AND execution_epoch=:epoch'), {key: params[key] for key in ('seq', 'tid', 'epoch')})


async def resource_registration(db, *, tid, epoch, seq):
    result = await db.execute(text("""SELECT * FROM runtime_resource_registrations
            WHERE task_id=:tid AND execution_epoch=:epoch AND registration_seq=:seq"""), {'tid': tid, 'epoch': epoch, 'seq': seq})
    return result.mappings().first()


async def launch_resolution(db, *, tid, epoch, seq):
    result = await db.execute(text("""SELECT * FROM runtime_resource_launch_resolutions
            WHERE task_id=:tid AND execution_epoch=:epoch AND registration_seq=:seq"""), {'tid': tid, 'epoch': epoch, 'seq': seq})
    return result.mappings().first()


async def database_time(db):
    return await db.scalar(text('SELECT clock_timestamp()'))


async def insert_launch_resolution(db, params):
    await db.execute(text("""INSERT INTO runtime_resource_launch_resolutions
            (task_id,execution_epoch,registration_seq,payload,digest)
            VALUES (:tid,:epoch,:seq,CAST(:payload AS jsonb),:digest)"""), params)


async def parked_snapshot(db, *, id):
    result = await db.execute(text('SELECT * FROM runtime_snapshots WHERE id=:id'), {'id': id})
    return result.mappings().first()


async def boundary_event(db, *, sid, seq):
    result = await db.execute(text('SELECT * FROM runtime_events WHERE session_id=:sid AND runtime_seq=:seq'), {'sid': sid, 'seq': seq})
    return result.mappings().first()


async def projection_state(db, *, id):
    result = await db.execute(text('SELECT * FROM task_projection_states WHERE task_id=:id'), {'id': id})
    return result.mappings().first()


async def projection_cursor(db, *, id):
    result = await db.execute(text('SELECT * FROM runtime_projection_cursors WHERE conversation_id=:id'), {'id': id})
    return result.mappings().first()


async def has_unresolved_control(db, params):
    return await db.scalar(text("""SELECT EXISTS(SELECT 1 FROM runtime_commands WHERE task_id=:tid
        AND (status IN ('claimed','indeterminate') OR (kind='StopTask' AND status='accepted')))"""), params)


async def quiescence_receipt(db, *, id, tid, epoch):
    result = await db.execute(text("""SELECT * FROM runtime_node_quiescence_evidence
            WHERE evidence_id=:id OR (task_id=:tid AND execution_epoch=:epoch)"""), {'id': id, 'tid': tid, 'epoch': epoch})
    return result.mappings().first()


async def registered_resources(db, params):
    result = await db.execute(text("""SELECT * FROM runtime_resource_registrations
            WHERE task_id=:tid AND execution_epoch=:epoch ORDER BY registration_seq"""), params)

    return result.mappings().all()


async def launch_resolutions(db, params):
    result = await db.execute(text("""SELECT * FROM runtime_resource_launch_resolutions
            WHERE task_id=:tid AND execution_epoch=:epoch ORDER BY registration_seq"""), params)

    return result.mappings().all()


async def record_quiescence(db, *, ledger, receipt, audit):
    """Freeze the verified ledger with its immutable receipt and product audit."""
    await db.execute(text("""UPDATE runtime_resource_ledgers SET closed=true,
            boundary=CAST(:boundary AS jsonb),boundary_digest=:bdigest,ledger_digest=:digest
            WHERE task_id=:tid AND execution_epoch=:epoch"""), ledger)
    await db.execute(text("""INSERT INTO runtime_node_quiescence_evidence(evidence_id,task_id,execution_epoch,payload,digest)
            VALUES (:id,:tid,:epoch,CAST(:payload AS jsonb),:digest)"""), receipt)
    await db.execute(text("""INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
            SELECT CAST(:tid AS varchar),CAST(:cid AS varchar),coalesce(max(seq),0)+1,'runtime_quiescence_recorded',CAST(:payload AS json)
            FROM task_events WHERE task_id=:tid"""), audit)
