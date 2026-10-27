"""External receipt and continuation SQL; caller owns transaction and policy."""
import json
from sqlalchemy import text
from .control_admission import row


async def lock_task(db, *, id):
    return await row(db, 'SELECT * FROM tasks WHERE id=:id FOR UPDATE', id=id)


async def matching_receipt(db, *, id, tid, call, producer, job):
    return await row(db, '''SELECT * FROM runtime_external_receipts WHERE receipt_id=:id
                OR (task_id=:tid AND tool_call_id=:call) OR (producer_id=:producer AND job_id=:job)''', id=id, tid=tid, call=call, producer=producer, job=job)


async def projection_state(db, *, id):
    return await row(db, 'SELECT * FROM task_projection_states WHERE task_id=:id', id=id)


async def execution(db, *, id):
    return await row(db, 'SELECT * FROM runtime_task_executions WHERE task_id=:id', id=id)


async def insert_receipt(db, receipt):
    return await db.execute(text('''INSERT INTO runtime_external_receipts
                (receipt_id,task_id,session_id,reply_id,execution_epoch,tool_call_id,producer_id,job_id,payload,digest)
                VALUES (:id,:tid,:sid,:reply,:epoch,:call,:producer,:job,CAST(:payload AS jsonb),:digest)'''),
                dict(id=receipt.receipt_id,tid=receipt.task_id,sid=receipt.session_id,reply=receipt.reply_id,
                     epoch=receipt.execution_epoch,call=receipt.tool_call_id,producer=receipt.producer_id,
                     job=receipt.job_id,payload=receipt.model_dump_json(),digest=receipt.digest()))


async def append_receipt_audit(db, receipt):
    return await db.execute(text('''INSERT INTO task_events(task_id,conversation_id,seq,event_type,payload)
                SELECT CAST(:tid AS varchar),CAST(:cid AS varchar),coalesce(max(seq),0)+1,
                'external_result_received',CAST(:payload AS json) FROM task_events WHERE task_id=:tid'''),
                dict(tid=receipt.task_id,cid=receipt.conversation_id,payload=json.dumps({
                    'receiptId':receipt.receipt_id,'producerId':receipt.producer_id,'jobId':receipt.job_id,
                    'toolCallId':receipt.tool_call_id,'replyId':receipt.reply_id,'digest':receipt.digest()})))


async def command(db, *, id):
    return await row(db, 'SELECT * FROM runtime_commands WHERE id=:id', id=id)


async def outstanding_control(db, *, id, snapshot):
    return await row(db, '''SELECT id FROM runtime_commands WHERE task_id=:id AND
                ((kind='StopTask') OR (kind='ResumeInteraction' AND
                (status IN ('accepted','claimed','indeterminate') OR payload->>'snapshot_id'=:snapshot)))''', id=id, snapshot=snapshot)


async def snapshot(db, *, id):
    return await row(db, 'SELECT * FROM runtime_snapshots WHERE id=:id', id=id)


async def lock_projection(db, *, id):
    return await row(db, 'SELECT * FROM task_projection_states WHERE task_id=:id FOR UPDATE', id=id)


async def projection_cursor(db, *, id):
    return await row(db, 'SELECT * FROM runtime_projection_cursors WHERE conversation_id=:id', id=id)


async def receipt(db, *, id):
    return await row(db, 'SELECT * FROM runtime_external_receipts WHERE receipt_id=:id', id=id)
