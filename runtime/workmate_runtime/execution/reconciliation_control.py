"""Pure no-start terminal evidence validation for read-only startup inspection."""
import json


def valid_unstarted_boundary(row,session,conversation,task,execution):
    raw=row['control_boundary']
    evidence=json.loads(raw) if isinstance(raw,str) else raw
    if not evidence:
        return False
    event=evidence['event']
    payload=event['payload']
    if payload.get('status')=='failed':
        return _valid_failed(evidence,session,conversation,task,execution)
    start,stop=evidence['start'],evidence['stop']
    if (session['status']!='cancelled' or session['reply_id'] is not None
            or execution['snapshot_id'] is not None or execution['input_snapshot_id']!=session['snapshot_id']
            or event['session_id']!=session['id'] or event['task_id']!=task['id']
            or event['execution_epoch']!=session['execution_epoch'] or event['runtime_seq']!=session['runtime_seq']
            or payload.get('type')!='EXECUTION_NOT_STARTED' or payload.get('reply_id') is not None
            or payload.get('session_id')!=session['id'] or payload.get('status')!='cancelled'
            or payload.get('resources_quiescent') is not True
            or payload.get('state_version')!=session['state_version']
            or payload.get('input_snapshot_id')!=session['snapshot_id']
            or not start or not stop or start['id']==stop['id']
            or start['kind']!='StartTask' or stop['kind']!='StopTask' or stop['status']!='completed'
            or stop['claimed_epoch']!=session['execution_epoch'] or stop['claimed_by']!=session['owner_id']
            or not stop['claimed_at'] or evidence['other_events']):
        return False
    for command in (start,stop):
        if (command['session_id']!=session['id'] or command['task_id']!=task['id']
                or command['conversation_id']!=conversation['id'] or command['actor_user_id']!=conversation['user_id']
                or command['protocol_version']!=1):
            return False
    if start['expected_epoch']!=session['execution_epoch']-1:
        return False
    if payload.get('preparation')=='unclaimed':
        return (start['status']=='rejected' and stop['expected_epoch']==start['expected_epoch']
            and all(start[k] is None for k in ('claimed_by','claimed_epoch','claimed_at')))
    return (payload.get('preparation')=='joined' and start['status']=='completed'
        and start['claimed_epoch']==session['execution_epoch'] and start['claimed_by']==session['owner_id']
        and start['claimed_at'] is not None and stop['expected_epoch']==session['execution_epoch'])


def _valid_failed(evidence,session,conversation,task,execution):
    event=evidence['event']
    payload=event['payload']
    start=evidence['start']
    return (session['status']=='failed' and task['status'] in {'created','failed'}
        and session['reply_id'] is None and execution['snapshot_id'] is None
        and execution['input_snapshot_id']==session['snapshot_id']
        and event['session_id']==session['id'] and event['task_id']==task['id']
        and event['execution_epoch']==session['execution_epoch'] and event['runtime_seq']==session['runtime_seq']
        and payload.get('type')=='EXECUTION_NOT_STARTED' and payload.get('reply_id') is None
        and payload.get('session_id')==session['id']
        and payload.get('reason') in {'sandbox_verification_failed','model_unavailable'}
        and payload.get('preparation')=='joined' and payload.get('resources_quiescent') is True
        and 'stop_command_id' not in payload
        and type(payload.get('state_version')) is int and payload['state_version']==session['state_version']
        and payload.get('input_snapshot_id')==session['snapshot_id']
        and payload.get('worker_id')==session['owner_id']
        and bool(start) and start['kind']=='StartTask' and start['status']=='completed'
        and start['session_id']==session['id'] and start['task_id']==task['id']
        and start['conversation_id']==conversation['id'] and start['actor_user_id']==conversation['user_id']
        and start['protocol_version']==1 and start['expected_epoch']==session['execution_epoch']-1
        and start['claimed_by']==session['owner_id'] and start['claimed_epoch']==session['execution_epoch']
        and start['claimed_at'] is not None and not evidence['other_events'] and not evidence['other_commands'])


def valid_released_suspension(pause, session, task, execution, snapshot):
    """Read-only classification, never the authority for a cold claim."""
    from workmate_runtime.transport.suspension import CleanupReceipt
    if (not snapshot or pause['status'] != 'released' or session['owner_id'] is not None
            or session['lease_until'] is not None or task['status'] not in {'awaiting_permission','cancelling'}
            or any(item['status'] != 'awaiting_interaction' for item in (session,execution,snapshot))
            or any(pause[key] != value for key,value in {
                'snapshot_id':session['snapshot_id'],'session_id':session['id'],'task_id':task['id'],
                'reply_id':session['reply_id'],'source_epoch':session['execution_epoch'],
                'state_version':session['state_version'],'runtime_seq':session['runtime_seq'],
                'storage_node_id':session['storage_node_id']}.items())
            or any(snapshot[key] != session[key] for key in ('execution_epoch','state_version','runtime_seq','reply_id'))
            or snapshot['id'] != session['snapshot_id'] or snapshot['task_id'] != task['id']
            or snapshot['session_id'] != session['id']
            or any(execution[key] != session[key] for key in ('execution_epoch','state_version','reply_id','snapshot_id'))):
        return False
    try:
        proof=CleanupReceipt.model_validate(pause['cleanup_receipt'])
    except (ValueError,TypeError):
        return False
    return (proof.resources_quiescent and proof.workspace_closed
        and proof.session_id==session['id'] and proof.task_id==task['id']
        and proof.worker_id==pause['source_worker_id'] and proof.execution_epoch==pause['source_epoch']
        and proof.snapshot_id==pause['snapshot_id'] and proof.snapshot_digest==pause['snapshot_digest']
        and proof.node_id==pause['storage_node_id'] and proof.resource_generation==pause['resource_generation'])
