"""Visible failure for uncertain execution; never releases or recovers Runtime."""
from sqlalchemy.exc import DBAPIError
from workmate_product.persistence.repositories import execution_quarantine as quarantine_store
from workmate_product.persistence.repositories.delivery import append_delivery_audit
from workmate_product.projections.store import lock_product_conversation,append_business_events,ProjectedEvent,ProjectionConflict

CODE='runtime_execution_quarantined'
READ_ONLY_REASON='任务执行异常。可在恢复条件满足时重新执行，已发生的外部操作不会撤销。'


class QuarantineConflict(ValueError):
    pass


def is_quarantined(error):
    return isinstance(error,dict) and error.get('code')==CODE


async def conversation_quarantined(db,conversation):
    if not conversation.active_product_task_id:
        return False
    error=await quarantine_store.read_task_error(db, task_id=conversation.active_product_task_id,
        conversation_id=conversation.id)
    return is_quarantined(error)


async def quarantine_execution(db,*,conversation_id,task_id,session_id,worker_id,execution_epoch,
                               storage_node_id,error_type='ExecutionAborted',cause_type=None,require_expired=False):
    """Caller-owned transaction; return True once, False for the same receipt.

    This is failure reporting, not resource cleanup. Owner/commands, both gates,
    snapshots and the Workspace crash latch remain untouched. No retry/dispatch.
    """
    if not db.in_transaction() or db.bind.dialect.name!='postgresql':
        raise QuarantineConflict('Quarantine requires a caller-owned PostgreSQL transaction')
    if (type(execution_epoch) is not int or execution_epoch<1
            or any(not isinstance(v,str) or not v or len(v)>128 for v in
                (conversation_id,task_id,session_id,worker_id,storage_node_id))
            or any(v is not None and (not isinstance(v,str) or not v.isidentifier() or len(v)>128)
                for v in (error_type,cause_type)) or error_type is None):
        raise QuarantineConflict('Exact execution identity and exception class names are required')
    identity=dict(session_id=session_id,worker_id=worker_id,execution_epoch=execution_epoch,
                  storage_node_id=storage_node_id)
    async with db.begin_nested():
        try:
            conv=await lock_product_conversation(db,conversation_id)
        except ProjectionConflict as exc:
            raise QuarantineConflict('Conversation is unavailable') from exc
        task, session, execution = await quarantine_store.lock_execution_context(
            db, task_id=task_id, session_id=session_id)
        if (not task or not session or not execution or task['conversation_id']!=conversation_id
                or conv['active_product_task_id']!=task_id or session['active_task_id']!=task_id
                or session['conversation_id']!=conversation_id or session['owner_id']!=worker_id
                or session['storage_node_id']!=storage_node_id or session['execution_epoch']!=execution_epoch
                or execution['session_id']!=session_id or execution['execution_epoch']!=execution_epoch
                or execution['status']!=session['status'] or execution['reply_id']!=session['reply_id']
                or execution['state_version']!=session['state_version']
                or task['run_id'] not in (None,session['reply_id'])
                or session['status'] not in {'preparing','running','awaiting_interaction','stopping'}):
            raise QuarantineConflict('Execution owner, node, epoch or admission gates changed')
        if require_expired and not await quarantine_store.lease_expired(db, session_id=session_id):
            return False  # A heartbeat won before we obtained the existing owner locks.
        if is_quarantined(task['error']):
            if task['status']!='failed' or task['error'].get('context',{}).get('execution')!=identity:
                raise QuarantineConflict('Existing quarantine receipt has different execution identity')
            return False
        if task['status'] not in {'created','running','awaiting_permission','cancelling'}:
            raise QuarantineConflict('Terminal Task cannot be replaced by quarantine')
        error={'code':CODE,'message':READ_ONLY_REASON,'context':{'resumable':False,
            'resources_quiescent':None,'execution':identity,'error_type':error_type,'cause_type':cause_type}}
        await quarantine_store.record_failure(db, task_id=task_id, error=error)
        await append_delivery_audit(db,conversation_id,task_id,'execution_quarantined',error)
        await append_business_events(db,conversation_id,operation_key='quarantine:'+task_id,
            task_id=task_id,reply_id=session['reply_id'],items=[ProjectedEvent(key='failure',event_type='RUN_ERROR',
                payload={'type':'RUN_ERROR','code':CODE,'message':READ_ONLY_REASON,
                    'taskId':task_id,'runId':session['reply_id'],'context':{'resumable':False}})])
        return True


async def report_expired_executions(sessions, *, limit=100):
    """Record unknown outcomes after owner expiry; retain every execution fence.

    Reused by the existing low-frequency Gateway repair loop. Expiry only proves
    lost authority, never stopped external tools; no takeover or retry is issued.
    """
    async with sessions() as db:
        candidates = await quarantine_store.expired_candidates(db, limit=limit)
    reported = 0
    for candidate in candidates:
        try:
            async with sessions.begin() as db:
                # A lost remote transaction must not stall unrelated failure
                # reports. This budget applies only to this candidate and is
                # reset by transaction exit, including failed lock acquisition.
                await quarantine_store.set_report_lock_timeout(db)
                reported += bool(await quarantine_execution(db, **candidate,
                    error_type='ExecutionOwnerExpired', require_expired=True))
        except DBAPIError as exc:
            # Catch only after the transaction has rolled back; SQL errors leave
            # it unusable. Connectivity and other database failures stay visible.
            if getattr(exc.orig, 'sqlstate', None) != '55P03':
                raise
            continue
        except QuarantineConflict:
            # Completion/release/another report won the lock. A later cycle reads
            # current durable facts; never overwrite a different owner or epoch.
            continue
    return reported
