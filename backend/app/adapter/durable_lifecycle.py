"""Product terminal settlement inside the durable consumer's transaction.

Interactive executions require the trusted interaction_closer callback. Artifact
preparation and delivery are separate; terminal settlement never releases gates,
scans the workspace, dispatches another Task or commits independently.
"""
from datetime import datetime
from workmate_contracts.realtime import message_id

from sqlalchemy import select

from workmate_product.projections.store import ProjectedEvent, ProjectionConflict, ProjectionResult
from workmate_product.persistence.models import Task, TaskEvent, TaskMessage
from ..services.usage import finish_usage_cost
from .durable_projection import ProjectionDeferred, _audit, _TERMINAL
from workmate_product.projections.translator import translate_event, translation_from_json, translation_to_json
from .display_boundary import display_evidence


_STATUS_BY_REASON={'completed':'completed','error':'failed',
                   'interrupted':'interrupted','exceed_max_iters':'interrupted'}


def _execution_failure(event, boundary, reason):
    if 'execution_failure' not in event.payload:
        return None
    failure = event.payload['execution_failure']
    cleanup = event.payload.get('resource_cleanup')
    if (not isinstance(failure, dict) or set(failure) != {'code', 'finished_reason'}
            or failure['code'] != 'realtime_unavailable'
            or failure['finished_reason'] != reason or reason not in _STATUS_BY_REASON
            or event.payload.get('status') != 'failed' or boundary['status'] != 'failed'
            or not isinstance(cleanup, dict) or cleanup.get('core_drained') is not True
            or cleanup.get('resources_quiescent') is not True
            or not ('display_snapshot' in event.payload or 'display_empty' in event.payload)):
        raise ProjectionConflict('Realtime failure lacks matching cleanup and display evidence')
    return {'type': 'realtime_unavailable',
        'message': 'Realtime delivery became unavailable; the task was stopped safely.',
        'runtimeReason': reason}


async def finalize_terminal_boundary(db,event,state,boundary,*,interaction_closer=None):
    """Settle one safe terminal; caller holds Conversation/Task in a savepoint.

    consume_next validates the immutable boundary's source/Task/Session/epoch
    association. This service additionally checks the persisted Reply end and
    refuses parked executions; interactive history requires a trusted closer.
    It does not interpret Core's internal snapshot format or claim delivery closed.
    """
    if not boundary or boundary['status'] not in _TERMINAL:
        raise ProjectionDeferred('A terminal execution boundary is required')
    if event.payload.get('pending_tool_call_ids') != []:
        raise ProjectionConflict('Terminal boundary must explicitly contain no pending tools')
    task=await db.get(Task,event.product_task_id,populate_existing=True)
    if task is None or task.conversation_id!=event.conversation_id:
        raise ProjectionConflict('Product Task identity changed')
    if task.run_id and task.run_id!=event.reply_id:
        raise ProjectionConflict('Product Task is bound to another Reply')
    if task.status in _TERMINAL:
        return ProjectionResult(state,[])
    if interaction_closer is None and (task.status=='awaiting_permission' or await db.scalar(select(TaskEvent.id).where(
            TaskEvent.task_id==task.id,TaskEvent.event_type=='hitl_request').limit(1))):
        raise ProjectionDeferred('Interactive execution requires the durable HITL finalizer')
    end=state.get('pending_reply_end',{})
    reason=end.get('finished_reason')
    metrics=state.get('metrics')
    translation=translation_from_json(state.get('translation',{}))
    failure = _execution_failure(event,boundary,reason)
    expected_status = 'failed' if failure else _STATUS_BY_REASON.get(reason)
    if (end.get('reply_id')!=event.reply_id or expected_status!=boundary['status']
            or task.run_id!=event.reply_id or translation.active_reply_id!=event.reply_id
            or metrics is None):
        raise ProjectionConflict('Terminal boundary lacks matching persisted Reply evidence')
    try:
        started=datetime.fromisoformat(metrics['started_at'])
        ended=datetime.fromisoformat(state['pending_reply_end_at'])
        if started.tzinfo is None or ended.tzinfo is None:
            raise ValueError('Timestamps must carry a timezone')
        duration_ms=max(0,int((ended-started).total_seconds()*1000))
    except (KeyError,TypeError,ValueError) as error:
        raise ProjectionConflict('Terminal metrics require durable source timestamps') from error
    status=expected_status
    error=failure or end.get('error')
    gate='realtime_unavailable' if failure else None
    last=state.get('last_tool_result',{})
    if translation.last_tool_result_state!=last.get('status'):
        raise ProjectionConflict('Tool outcome and public translation state disagree')
    if status=='completed' and last.get('status') in {'error','interrupted','denied'}:
        status='failed'
        gate='terminal_tool_failure'
        error={'type':'incomplete_tool_execution',
            'message':'The Agent stopped after its final tool call failed.',
            'runtimeReason':reason,'toolCallId':last.get('id'),
            'tool':last.get('name'),'toolState':last['status']}
    if interaction_closer is not None:
        await interaction_closer(db,event,state,boundary)
    if task.status=='cancelling':
        status='cancelled'
    task.status=status
    task.error=error
    task.duration_ms=duration_ms
    task.steps=metrics['steps']
    task.model_name=metrics['model_name']
    task.token_usage=({'input_tokens':metrics['input_tokens'],'output_tokens':metrics['output_tokens'],
        **({'complete':False} if metrics['incomplete'] or metrics['pending_calls'] else {})}
        if metrics['input_tokens'] or metrics['output_tokens'] else None)
    finish_usage_cost(task,model_matches=not metrics['models'] or set(metrics['models'])=={task.usage_model_id})
    existing=await db.get(TaskMessage,message_id(task.id,event.reply_id),populate_existing=True)
    phases={'final'} if boundary['status']=='completed' else {'interrupted'}
    display=display_evidence(event,existing,phases=phases)
    if failure and existing is not None and (
            existing.snapshot_phase != 'interrupted' or not existing.snapshot_incomplete):
        raise ProjectionConflict('Realtime failure requires an incomplete interrupted display cut')
    if existing is not None and existing.snapshot_incomplete and status=='completed':
        raise ProjectionConflict('Incomplete interrupted text cannot prove successful completion')
    await _audit(db,event,'run_finished',{'reason':reason,'durationMs':duration_ms,'steps':task.steps,
        'synthetic':False,'taskStatus':status,'completionGate':gate})
    if status=='cancelled':
        await _audit(db,event,'task_cancelled',{'reason':reason,'synthetic':False})
    await _audit(db,event,'run_metrics',{'durationMs':duration_ms,'steps':task.steps,'model':task.model_name,
        'inputTokens':metrics['input_tokens'],'outputTokens':metrics['output_tokens']})
    # Preserve the established translator shape; cancellation is a Product intent
    # and must also override a concurrently completed Runtime reply in public SSE.
    public_reason = 'interrupted' if status=='cancelled' else 'error' if failure else reason
    public=translate_event({**end,'finished_reason':public_reason,
                           **({'error': error} if failure else {})},
                           event.conversation_id,translation)
    for item in public:
        item.update(display)
        if item.get('name')=='TASK_COMPLETED':
            item['value'].update(reason=reason,taskStatus=status,completionGate=gate)
    state['translation']=translation_to_json(translation)
    state['terminal_boundary_id']=boundary['id']
    from .durable_permission_reset import record_permission_reset
    await record_permission_reset(db,event,task)
    return ProjectionResult(state,[ProjectedEvent(key=str(i),event_type=item['type'],payload=item)
                                   for i,item in enumerate(public)])
