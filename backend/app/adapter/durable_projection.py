"""Staged live Product projection, driven by the durable consumer transaction.

No file/network I/O, independent commits or dequeue callbacks. Terminal and HITL
finalization must be provided by the new transactional lifecycle service; the old
EventConsumer cannot be invoked here because it commits and performs external I/O.
"""

from sqlalchemy import func, select

from workmate_product.projections.store import ProjectedEvent, ProjectionResult, ProjectionConflict
from workmate_product.persistence.models import Task, TaskEvent
from workmate_product.projections.knowledge import KNOWLEDGE_CUSTOM_EVENTS, compact_event_payload
from .skill_projection import is_skill_payload, compact_skill_payload
from workmate_product.projections.translator import translate_event, translation_from_json, translation_to_json


class ProjectionDeferred(RuntimeError):
    """Required lifecycle handler is not installed; leave the source cursor intact."""


_TERMINAL = {'completed','failed','cancelled','interrupted'}
_DISPLAY_BODY_TYPES = frozenset(f'{kind}_BLOCK_{action}'
    for kind in ('TEXT','THINKING') for action in ('START','DELTA','END'))


async def _audit(db,event,event_type,payload):
    # consume_next already holds this Task row lock, matching existing allocators.
    seq=(await db.scalar(select(func.max(TaskEvent.seq)).where(TaskEvent.task_id==event.product_task_id)) or 0)+1
    if is_skill_payload(payload):
        payload=compact_skill_payload(payload)
    browser_payload=compact_event_payload(event_type,payload)
    if browser_payload is not payload:
        payload={**payload,'browser_projection':browser_payload}
    db.add(TaskEvent(task_id=event.product_task_id,conversation_id=event.conversation_id,
                     seq=seq,event_type=event_type,payload=payload,created_at=event.occurred_at))
    await db.flush()


def live_projector(*, finalize_boundary=None, stage_interaction=None):
    """Return a trusted callback for consume_next; no default terminal success.

    finalize_boundary(db,event,state,boundary) returns ProjectionResult in this
    transaction. A terminal settlement may precede Artifact delivery, but must
    retain both active Task gates until the separate delivery protocol closes.
    """
    async def project(db,event,state,boundary):
        task=await db.get(Task,event.product_task_id,populate_existing=True)
        if task is None or task.conversation_id != event.conversation_id:
            raise ProjectionConflict('Product Task identity changed')
        from workmate_product.application.execution_quarantine import is_quarantined
        if is_quarantined(task.error):
            raise ProjectionDeferred('Quarantined execution cannot advance Product projection')
        if event.event_type == 'EXECUTION_NOT_STARTED':
            from .durable_unstarted import finalize_unstarted
            return await finalize_unstarted(db,event,state)
        if event.event_type == 'EXECUTION_BOUNDARY':
            if finalize_boundary is None:
                raise ProjectionDeferred('Transactional boundary finalizer is not installed')
            return await finalize_boundary(db,event,state,boundary)
        translation=translation_from_json(state.get('translation',{}))
        if task.status in _TERMINAL:
            # Late source events must never revive or append to terminal Product Tasks.
            return ProjectionResult(state,[])
        if event.event_type in {'REQUIRE_USER_CONFIRM','REQUIRE_EXTERNAL_EXECUTION'}:
            if stage_interaction is None:
                raise ProjectionDeferred('Durable interaction projection is not installed')
            if not task.run_id or task.run_id != event.reply_id:
                raise ProjectionConflict('Interaction requires its original Product Reply')
            return await stage_interaction(db,event,state)
        if event.event_type in _DISPLAY_BODY_TYPES:
            raise ProjectionConflict('Ephemeral body must use the realtime display writer')
        raw=event.payload
        if event.event_type == 'REPLY_END':
            state['pending_reply_end']=raw
            state['pending_reply_end_at']=event.occurred_at.isoformat()
            return ProjectionResult(state,[])  # A streamed end is not yet a safe execution boundary.
        if task.run_id and task.run_id != event.reply_id:
            raise ProjectionConflict('Task is already bound to a different Reply')
        if event.event_type == 'REPLY_START':
            resumed=translation.active_reply_id == event.reply_id
            if not resumed:
                from .durable_initial_permission import project_initial_permission
                await project_initial_permission(db,event,task)
                state['metrics']={'started_at':event.occurred_at.isoformat(),
                    'steps':0,'model_name':None,'models':[],
                    'pending_calls':0,'incomplete':False,'input_tokens':0,'output_tokens':0}
            task.run_id=event.reply_id
            if task.status != 'cancelling':
                task.status='running'
            await _audit(db,event,'run_resumed' if resumed else 'run_started',{'replyId':event.reply_id})
        elif not task.run_id:
            raise ProjectionConflict('Product Reply start must be projected first')
        if event.event_type == 'TOOL_CALL_START':
            if 'metrics' in state:
                state['metrics']['steps']+=1
            state.setdefault('tool_names',{})[raw['tool_call_id']]=raw.get('tool_call_name')
            await _audit(db,event,'tool_call',{'toolCallId':raw['tool_call_id'],
                'tool':raw.get('tool_call_name'),'user':state['actor_user_id']})
        elif event.event_type == 'TOOL_CALL_DELTA':
            tool_id=raw.get('tool_call_id')
            if state.get('tool_names',{}).get(tool_id)=='search_knowledge':
                arguments=state.setdefault('knowledge_arguments',{})
                arguments[tool_id]=(arguments.get(tool_id,'')+raw.get('delta',''))[:16384]
        elif event.event_type == 'TOOL_CONTENT_REFERENCE':
            ref = raw['content_ref']
            await _audit(db,event,'tool_content',{'toolCallId':raw['tool_call_id'],
                'kind':ref['kind'],'sha256':ref['sha256'],'sizeBytes':ref['size_bytes'],
                'preview':ref['preview'],'incomplete':ref['incomplete']})
        elif event.event_type == 'TOOL_RESULT_END':
            state.setdefault('tool_results',{})[raw['tool_call_id']]=raw.get('state','success')
            state['last_tool_result']={'id':raw['tool_call_id'],
                'name':state.get('tool_names',{}).get(raw['tool_call_id']),
                'status':raw.get('state','success')}
            await _audit(db,event,'tool_result',{'toolCallId':raw['tool_call_id'],
                'tool':state.get('tool_names',{}).get(raw['tool_call_id']),
                'status':raw.get('state','success'),'user':state['actor_user_id'],
                **({'arguments':state.get('knowledge_arguments',{}).pop(raw['tool_call_id'],''),
                    'arguments_limit':16384}
                   if state.get('tool_names',{}).get(raw['tool_call_id'])=='search_knowledge' else {})})
        elif event.event_type == 'CUSTOM' and raw.get('name') in KNOWLEDGE_CUSTOM_EVENTS:
            value=raw.get('value')
            if not isinstance(value,dict) or value.get('reply_id') != event.reply_id:
                raise ProjectionConflict('Knowledge sources require their exact Reply identity')
            await _audit(db,event,raw['name'],value)
        elif event.event_type in {'MODEL_CALL_START','MODEL_CALL_END'}:
            _accumulate_usage(state,raw)
        if 'actor_user_id' not in state:
            from workmate_product.persistence.models import Conversation
            state['actor_user_id']=await db.scalar(select(Conversation.user_id).where(Conversation.id==event.conversation_id))
        public=translate_event(raw,event.conversation_id,translation)
        for item in public:
            if item['type'] == 'RUN_STARTED':
                # Product identity comes from the locked task, never raw Core data.
                item['taskId'] = task.id
        state['translation']=translation_to_json(translation)
        return ProjectionResult(state,[ProjectedEvent(key=str(index),event_type=item['type'],payload=item)
                                       for index,item in enumerate(public)])
    return project


def _accumulate_usage(state, raw):
    metrics=state.get('metrics')
    if metrics is None:
        raise ProjectionConflict('Missing durable metrics; source must be reprojected')
    if raw['type']=='MODEL_CALL_START':
        metrics['pending_calls']+=1
        model=raw.get('model_name')
        if model:
            metrics['model_name']=model
            if model not in metrics['models']:
                metrics['models'].append(model)
        return
    if metrics['pending_calls']==0:
        metrics['incomplete']=True
    metrics['pending_calls']=max(0,metrics['pending_calls']-1)
    tokens=[raw.get('input_tokens'),raw.get('output_tokens')]
    if any(value is not None and (type(value) is not int or value<0) for value in tokens):
        raise ProjectionConflict('Invalid model token counters')
    metrics['incomplete'] |= None in tokens or not any(tokens)
    metrics['input_tokens']+=tokens[0] or 0
    metrics['output_tokens']+=tokens[1] or 0
