"""Validate immutable epoch ancestry without granting current execution rights."""
import json
from workmate_contracts import snapshot_digest


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def valid_release(row):
    """Check the complete release receipt before reusing historical authority."""
    if row is None or row['status'] != 'released':
        return False
    try:
        proof = _json(row['cleanup_receipt'])
        return (isinstance(proof,dict) and proof.get('schema_version') == 1
            and type(proof.get('schema_version')) is int
            and proof.get('resources_quiescent') is True and proof.get('workspace_closed') is True
            and type(proof.get('execution_epoch')) is int
            and all(proof.get(k) == v for k,v in {
                'task_id':row['task_id'],'session_id':row['session_id'],'node_id':row['storage_node_id'],
                'snapshot_id':row['snapshot_id'],'snapshot_digest':row['snapshot_digest'],
                'worker_id':row['source_worker_id'],'execution_epoch':row['source_epoch'],
                'resource_generation':row['resource_generation']}.items())
            and snapshot_digest(_json(row['snapshot_payload'])) == row['snapshot_digest'])
    except (ValueError,TypeError,KeyError):
        return False


def valid_lineage(rows, *, task_id, session_id, node_id, source_worker, source_epoch,
                  worker_id, execution_epoch, allow_pending_last=False):
    """Each exact, released boundary may bridge only one consecutive epoch."""
    worker, epoch = worker_id, execution_epoch
    while epoch > source_epoch:
        matches = [r for r in rows if r['resume_epoch'] == epoch and r['resume_worker_id'] == worker]
        if len(matches) != 1:
            return False
        row = matches[0]
        pending = (allow_pending_last and epoch == execution_epoch and worker == worker_id
            and row['command_status'] == 'accepted' and row['command_worker'] is None
            and row['command_epoch'] is None)
        if (not valid_release(row) or row['task_id'] != task_id or row['session_id'] != session_id
                or row['storage_node_id'] != node_id or row['source_epoch'] != epoch - 1
                or (not pending and (row['command_status'] not in {'claimed','completed'}
                    or row['command_worker'] != worker or row['command_epoch'] != epoch))
                or row['command_kind'] not in {'ResumeInteraction','StopTask'}
                or (row['command_kind']=='ResumeInteraction' and row['command_source_epoch'] != row['source_epoch'])
                or (row['command_kind']=='StopTask' and not 0 <= row['command_source_epoch'] <= row['source_epoch'])
                or row['command_task'] != task_id or row['command_session'] != session_id):
            return False
        worker, epoch = row['source_worker_id'], row['source_epoch']
    return worker == source_worker and epoch == source_epoch


LINEAGE_SELECT = '''SELECT p.*, b.payload AS snapshot_payload,
    c.kind AS command_kind,c.status AS command_status,c.claimed_by AS command_worker,c.claimed_epoch AS command_epoch,
    c.expected_epoch AS command_source_epoch,c.task_id AS command_task,c.session_id AS command_session
    FROM runtime_suspensions p JOIN runtime_snapshots b ON b.id=p.snapshot_id
    JOIN runtime_commands c ON c.id=p.resume_command_id'''
