"""Validate durable control evidence for an execution without a Core Reply."""
from sqlalchemy import text
from workmate_contracts.controls import StopPayload


async def unstarted_stop_reason(db, event):
    """Resolve the reason only from this event's completed, authorized Stop."""
    from .store import ProjectionConflict
    stop = (await db.execute(text('''SELECT * FROM runtime_commands WHERE id=:id
        AND task_id=:task AND session_id=:session AND conversation_id=:conversation
        AND kind='StopTask' AND status='completed' AND protocol_version=1
        AND claimed_epoch=:epoch'''), {'id': event.payload.get('stop_command_id'),
            'task': event.product_task_id, 'session': event.runtime_session_id,
            'conversation': event.conversation_id, 'epoch': event.execution_epoch})).mappings().first()
    if stop is None or not stop['claimed_by'] or not stop['claimed_at']:
        raise ProjectionConflict('Unstarted execution has no matching completed Stop')
    try:
        reason = StopPayload.model_validate(stop['payload']).reason
    except (TypeError, ValueError) as error:
        raise ProjectionConflict('Unstarted Stop has an invalid reason') from error
    owner = await db.scalar(text('SELECT user_id FROM conversations WHERE id=:id'),
                            {'id': event.conversation_id})
    if reason == 'user_requested':
        if stop['actor_user_id'] != owner:
            raise ProjectionConflict('Unstarted Stop actor is not its owner')
    elif not await db.scalar(text('''SELECT 1 FROM task_events
        WHERE task_id=:task AND conversation_id=:conversation AND event_type='task_cancel_requested'
        AND payload->>'commandId'=:command AND payload->>'actorUserId'=:actor
        AND payload->>'targetUserId'=:owner AND payload->>'reason'='account_disabled' LIMIT 1'''),
        {'task': event.product_task_id, 'conversation': event.conversation_id,
         'command': stop['id'], 'actor': stop['actor_user_id'], 'owner': owner}):
        raise ProjectionConflict('Unstarted administrator Stop lacks its admission receipt')
    return reason


async def validate_unstarted(db,event,execution):
    from .store import ProjectionConflict
    raw=event.payload
    if raw.get("status") == "failed":
        return await _validate_failed(db,event,execution)
    if (event.reply_id is not None or raw.get('status')!='cancelled'
            or raw.get('resources_quiescent') is not True or raw.get('preparation') not in {'unclaimed','joined'}
            or execution['status']!='cancelled' or execution['snapshot_id'] is not None
            or execution['reply_id'] is not None or execution['execution_epoch']!=event.execution_epoch
            or type(raw.get('state_version')) is not int or raw['state_version']!=execution['state_version']
            or raw.get('input_snapshot_id')!=execution['input_snapshot_id']):
        raise ProjectionConflict('Unstarted execution lacks exact cancellation evidence')
    records=(await db.execute(text('SELECT * FROM runtime_commands WHERE id IN (:start,:stop)'),
        {'start':raw.get('start_command_id'),'stop':raw.get('stop_command_id')})).mappings().all()
    commands={r['id']:r for r in records}
    start=commands.get(raw.get('start_command_id'))
    stop=commands.get(raw.get('stop_command_id'))
    if (not start or not stop or start['id']==stop['id']
            or any(c['session_id']!=event.runtime_session_id or c['task_id']!=event.product_task_id
                or c['conversation_id']!=event.conversation_id or c['protocol_version']!=1 for c in (start,stop))
            or start['kind']!='StartTask' or stop['kind']!='StopTask' or stop['status']!='completed'
            or not stop['claimed_by'] or not stop['claimed_at'] or stop['claimed_epoch']!=event.execution_epoch):
        raise ProjectionConflict('Unstarted execution has no matching completed Stop')
    actor=await db.scalar(text('SELECT user_id FROM conversations WHERE id=:id'),{'id':event.conversation_id})
    if start['actor_user_id']!=actor or start['expected_epoch']!=event.execution_epoch-1:
        raise ProjectionConflict('Unstarted control actor or admission epoch disagrees')
    await unstarted_stop_reason(db, event)
    if raw['preparation']=='unclaimed':
        valid=(start['status']=='rejected' and all(start[k] is None for k in ('claimed_by','claimed_epoch','claimed_at'))
            and start['expected_epoch']==event.execution_epoch-1 and stop['expected_epoch']==start['expected_epoch'])
    else:
        valid=(start['status']=='completed' and start['claimed_by']==stop['claimed_by']
            and start['claimed_epoch']==event.execution_epoch and start['claimed_at'] is not None
            and stop['expected_epoch']==event.execution_epoch)
    if not valid or await db.scalar(text('SELECT 1 FROM runtime_events WHERE task_id=:tid AND id<>:id LIMIT 1'),
            {'tid':event.product_task_id,'id':event.runtime_event_id}):
        raise ProjectionConflict('Execution was already started or control history disagrees')


async def _validate_failed(db,event,execution):
    from .store import ProjectionConflict
    raw=event.payload
    session=(await db.execute(text('SELECT * FROM runtime_sessions WHERE id=:id'),
        {'id':event.runtime_session_id})).mappings().first()
    conv=(await db.execute(text('SELECT * FROM conversations WHERE id=:id'),
        {'id':event.conversation_id})).mappings().first()
    start=(await db.execute(text('SELECT * FROM runtime_commands WHERE id=:id'),
        {'id':raw.get('start_command_id')})).mappings().first()
    if (event.reply_id is not None or raw.get('reason') not in {'sandbox_verification_failed','model_unavailable'}
            or raw.get('preparation')!='joined' or raw.get('resources_quiescent') is not True
            or 'stop_command_id' in raw or not isinstance(raw.get('worker_id'),str) or not raw['worker_id']
            or type(raw.get('state_version')) is not int
            or execution['status']!='failed' or execution['reply_id'] is not None
            or execution['snapshot_id'] is not None or execution['session_id']!=event.runtime_session_id
            or execution['execution_epoch']!=event.execution_epoch
            or execution['state_version']!=raw['state_version']
            or execution['input_snapshot_id']!=raw.get('input_snapshot_id')
            or not session or not conv or not start
            or conv['active_product_task_id']!=event.product_task_id
            or session['active_task_id']!=event.product_task_id or session['conversation_id']!=conv['id']
            or session['owner_id']!=raw['worker_id'] or session['status']!='failed'
            or session['reply_id'] is not None or session['execution_epoch']!=event.execution_epoch
            or session['runtime_seq']!=event.runtime_seq or session['state_version']!=raw['state_version']
            or session['snapshot_id']!=execution['input_snapshot_id']
            or start['kind']!='StartTask' or start['status']!='completed' or start['protocol_version']!=1
            or start['session_id']!=session['id'] or start['conversation_id']!=conv['id']
            or start['task_id']!=event.product_task_id or start['actor_user_id']!=conv['user_id']
            or start['claimed_by']!=raw['worker_id'] or start['claimed_epoch']!=event.execution_epoch
            or start['claimed_at'] is None or start['expected_epoch']!=event.execution_epoch-1):
        raise ProjectionConflict('Preparation failure lacks exact completed preparation evidence')
    if await db.scalar(text('SELECT 1 FROM runtime_events WHERE task_id=:tid AND id<>:id LIMIT 1'),
            {'tid':event.product_task_id,'id':event.runtime_event_id}):
        raise ProjectionConflict('Preparation failure cannot replace an execution that started')
    if await db.scalar(text("SELECT 1 FROM runtime_commands WHERE task_id=:tid AND id<>:id LIMIT 1"),
            {'tid':event.product_task_id,'id':start['id']}):
        raise ProjectionConflict('Preparation failure conflicts with another control command')
