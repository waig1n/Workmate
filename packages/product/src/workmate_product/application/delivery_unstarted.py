"""Empty delivery closure for proven cancellation or failure before Core started."""

from workmate_product.persistence.repositories import delivery as delivery_store
from workmate_contracts import RuntimeEvent
from workmate_product.projections.unstarted import validate_unstarted
from .delivery_metadata import DeliveryConflict


async def close_unstarted_delivery(db,conv,task,projection):
    if (not conv or not task or task['conversation_id']!=conv['id'] or task['status'] not in {'cancelled','failed'}
            or task['run_id'] is not None or projection['format_version']!=1 or projection['reply_id'] is not None):
        raise DeliveryConflict('No-start delivery identity changed')
    state=projection['state']
    event_id=state['terminal_control_event_id']
    receipt=state.get('delivery_closed')
    if receipt:
        if receipt!={'kind':'not_started','event_id':event_id,'artifact_ids':[]}:
            raise DeliveryConflict('No-start delivery receipt is inconsistent')
        return ()
    session=await delivery_store.lock_session(db, id=projection['session_id'])
    execution=await delivery_store.lock_execution(db, id=task['id'])
    row=await delivery_store.read_runtime_event(db, id=event_id)
    if (not session or not execution or not row or conv['active_product_task_id']!=task['id']
            or session['active_task_id']!=task['id'] or session['conversation_id']!=conv['id']
            or session['status']!=task['status'] or session['reply_id'] is not None
            or execution['session_id']!=session['id'] or execution['execution_epoch']!=session['execution_epoch']
            or execution['state_version']!=session['state_version'] or execution['input_snapshot_id']!=session['snapshot_id']
            or row['task_id']!=task['id'] or row['session_id']!=session['id']
            or row['execution_epoch']!=session['execution_epoch'] or row['runtime_seq']!=session['runtime_seq']
            or projection['last_runtime_seq']<row['runtime_seq']):
        raise DeliveryConflict('No-start terminal result is not the complete active execution boundary')
    raw=row['payload']
    if raw.get('status') != task['status']:
        raise DeliveryConflict('No-start delivery status differs from its execution event')
    event=RuntimeEvent(runtime_event_id=row['id'],conversation_id=conv['id'],runtime_session_id=session['id'],
        product_task_id=task['id'],reply_id=None,runtime_seq=row['runtime_seq'],execution_epoch=row['execution_epoch'],
        event_type=raw['type'],occurred_at=row['created_at'],payload=raw)
    if event.event_type!='EXECUTION_NOT_STARTED':
        raise DeliveryConflict('No-start receipt references an ordinary execution event')
    await validate_unstarted(db,event,execution)
    if await delivery_store.has_unsettled_commands(db, id=task['id']):
        raise DeliveryConflict('No-start control commands are not settled')
    await delivery_store.append_delivery_audit(db,conv['id'],task['id'],'artifact_delivery',{
        'source':'not_started','declaredFiles':[],'warnings':[]})
    updated={**state,'delivery_closed':{'kind':'not_started','event_id':event_id,'artifact_ids':[]}}
    await delivery_store.record_closure(db, task_id=task['id'], conversation_id=conv['id'],
        session_id=session['id'], state=updated)
    return ()
