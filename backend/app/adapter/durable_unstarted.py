"""Product no-start terminal projection without Reply, usage or assistant text."""
from workmate_product.projections.store import ProjectedEvent, ProjectionResult, ProjectionConflict
from workmate_product.persistence.models import Task


async def finalize_unstarted(db,event,state):
    from .durable_projection import _audit
    task=await db.get(Task,event.product_task_id,populate_existing=True)
    if (task is None or task.conversation_id!=event.conversation_id or task.run_id is not None
            or event.reply_id is not None or event.event_type!='EXECUTION_NOT_STARTED'
            or task.status!=('created' if event.payload.get('status')=='failed' else 'cancelling') or state):
        raise ProjectionConflict('Unstarted terminal result cannot replace an executed Product Task')
    failed=event.payload.get('status')=='failed'
    task.status='failed' if failed else 'cancelled'
    if failed:
        reason=event.payload.get('reason')
        messages={'sandbox_verification_failed':'Sandbox isolation could not be verified.',
                  'model_unavailable':'The selected model is unavailable on the execution worker.'}
        if reason not in messages:
            raise ProjectionConflict('Unsupported preparation failure reason')
        message=messages[reason]
        task.error={'code':reason,'message':message}
        await _audit(db,event,'task_failed',{'reason':reason,'executionStarted':False,
            'startCommandId':event.payload['start_command_id']})
        public=[{'type':'RUN_ERROR','message':message,
                 'code':reason},
            {'type':'CUSTOM','name':'TASK_COMPLETED','value':{'reason':reason,
                'taskStatus':'failed','executionStarted':False}}]
    else:
        from workmate_product.projections.unstarted import unstarted_stop_reason
        reason = await unstarted_stop_reason(db, event)
        await _audit(db,event,'task_cancelled',{'reason':reason,'executionStarted':False,
            'stopCommandId':event.payload['stop_command_id']})
        public=[{'type':'RUN_CANCELLED','threadId':event.conversation_id},
            {'type':'CUSTOM','name':'TASK_COMPLETED','value':{'reason':reason,
                'taskStatus':'cancelled','executionStarted':False}}]
    return ProjectionResult({'terminal_control_event_id':event.runtime_event_id},[
        ProjectedEvent(key=str(i),event_type=item['type'],payload=item) for i,item in enumerate(public)])
