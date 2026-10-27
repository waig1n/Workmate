"""Read-only startup classification. Observations never grant execution authority.

Even a matching process identity needs a verified live local controller registry
before attaching. Expiry, claimed commands and unknown cleanup never authorize
replay or takeover. This module performs one MVCC SELECT and no mutations.
"""
from dataclasses import dataclass
from .reconciliation_control import valid_unstarted_boundary, valid_released_suspension
import json


@dataclass(frozen=True)
class SessionObservation:
    session_id: str
    task_id: str | None
    category: str
    reason: str
    snapshot_id: str | None
    command_ids: tuple[str, ...]


_TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
_ACTIVE_PRODUCT = {'created', 'running', 'awaiting_permission', 'cancelling'}


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _classify(row, worker_id, storage_node_id):
    session = _decode(row['session'])
    conversation = _decode(row['conversation'])
    task = _decode(row['task'])
    execution = _decode(row['execution'])
    snapshot = _decode(row['boundary'])
    commands = _decode(row['commands'])
    ids = tuple(command['id'] for command in commands)

    def result(category, reason):
        return SessionObservation(session['id'], task['id'] if task else None,
                                  category, reason, session['snapshot_id'], ids)

    def invalid(reason):
        return result('blocked_inconsistent', reason)

    if not conversation or conversation['id'] != session['conversation_id']:
        return invalid('Missing Product Conversation')
    if row['orphan_execution']:
        return invalid('Another Task execution remains active without matching admission gates')
    active = session['active_task_id']
    admitted = conversation['active_product_task_id']
    if active and active != admitted:
        return invalid('Runtime and Product active Task gates disagree')
    target = active or admitted
    if target:
        if (not task or task['id'] != target or task['conversation_id'] != conversation['id']
                or not execution or execution['session_id'] != session['id']):
            return invalid('Task execution identity does not match Conversation/Session')
    elif task or execution:
        return invalid('Unexpected Task without an admission gate')

    # Validate all outstanding identities before considering ownership or affinity.
    for command in commands:
        if (command['conversation_id'] != conversation['id']
                or command['actor_user_id'] != conversation['user_id']
                or command['protocol_version'] != 1
                or (command['kind'] != 'PrepareSession' and command['task_id'] != target)):
            return invalid('Outstanding command has inconsistent Product identity')
    if any(command['status'] == 'indeterminate' for command in commands):
        return result('blocked_claimed', 'An indeterminate command requires explicit reconciliation')

    if active:
        pause = _decode(row.get('suspension'))
        if pause and pause['status'] in {'releasing','blocked'}:
            if (pause['status']=='releasing' and row['lease_valid']
                    and session['owner_id'] == pause['source_worker_id']
                    and session['execution_epoch'] == pause['source_epoch']
                    and session['owner_id'] != worker_id):
                return result('foreign_live', 'Another process is proving resource cleanup')
            return result('blocked_suspension', 'Cleanup has no complete committed release evidence')
        if pause and pause['status'] == 'released' and pause.get('resume_epoch') is None:
            if (any(c['status']=='claimed' for c in commands)
                    or not valid_released_suspension(pause,session,task,execution,snapshot)):
                return invalid('Released pause does not match exact safe boundary')
            if session['storage_node_id'] != storage_node_id:
                return result('foreign_storage', 'Paused Workspace is on another storage node')
            return result('released_permission', 'No live resources; a control may claim a new epoch')
        if (not session['owner_id'] or session['lease_until'] is None
                or session['execution_epoch'] < 1
                or execution['execution_epoch'] != session['execution_epoch']
                or execution['state_version'] != session['state_version']
                or execution['status'] != session['status']
                or execution['reply_id'] != session['reply_id']):
            return invalid('Owner or Task execution revision is inconsistent')
        if task['status'] not in _ACTIVE_PRODUCT | _TERMINAL:
            return invalid('Unknown Product Task state')
        if task['status'] in _TERMINAL and session['status'] not in _TERMINAL:
            return invalid('Product terminal conflicts with active execution')
        unstarted = valid_unstarted_boundary(row,session,conversation,task,execution)
        if session['status'] in {'awaiting_interaction'} | _TERMINAL and not unstarted:
            if (not snapshot or snapshot['task_id'] != target
                    or snapshot['session_id'] != session['id']
                    or snapshot['id'] != execution['snapshot_id']
                    or snapshot['reply_id'] != session['reply_id']
                    or snapshot['status'] != session['status']
                    or snapshot['state_version'] != session['state_version']
                    or (snapshot['execution_epoch'] != session['execution_epoch']
                        and not (pause and pause['status']=='released'
                            and pause.get('resume_epoch')==session['execution_epoch']
                            and pause.get('resume_worker_id')==session['owner_id']
                            and pause['source_epoch']==snapshot['execution_epoch']
                            and pause['source_epoch']+1==session['execution_epoch']))
                    or snapshot['runtime_seq'] != session['runtime_seq']):
                return invalid('Parked/terminal execution has no exact safe boundary')
        elif not unstarted and session['status'] not in {'preparing', 'running', 'stopping'}:
            return invalid('Active Task has no active or safe execution state')
        if any(command['status'] == 'claimed' and (
                command['claimed_by'] != session['owner_id']
                or command['claimed_epoch'] != session['execution_epoch']) for command in commands):
            return invalid('Claimed command is bound to another owner/epoch')
        if session['status'] in {'awaiting_interaction'} | _TERMINAL:
            if any(command['status'] == 'claimed' for command in commands):
                if (pause and pause.get('resume_epoch')==session['execution_epoch']
                        and row['lease_valid'] and session['owner_id'] != worker_id):
                    return result('foreign_live', 'Another process is preparing the claimed cold recovery')
                return result('blocked_claimed', 'Claimed command has not atomically settled at the safe boundary')
        if session['status'] in _TERMINAL:
            return result('terminal_delivery_pending', 'Safe terminal retains delivery/admission gates')
        if not row['lease_valid']:
            return result('blocked_expired', 'Expired ownership never proves old execution or tools stopped')
        if session['storage_node_id'] != storage_node_id:
            return result('foreign_storage', 'This Worker cannot access the Session storage')
        if session['owner_id'] != worker_id:
            return result('foreign_live', 'Another process has live ownership; observe only')
        if session['status'] == 'awaiting_interaction':
            return result('local_parked_unverified', 'Require verified local registry before supervising this owner')
        return result('local_execution_unverified', 'Require live local controller registry; never replay claimed work')

    # No Runtime execution gate: only a genuinely unclaimed admission can start.
    if session['owner_id'] or session['lease_until']:
        return invalid('Owner or lease remains without a Runtime active gate')
    if session['status'] not in {'accepted'} | _TERMINAL:
        return invalid('Ungated Session has an active execution state')
    if any(command['status'] == 'claimed' for command in commands):
        return result('blocked_claimed', 'Claimed command cannot be retried without execution evidence')
    if target:
        if (task['status'] not in {'created', 'cancelling'} or execution['status'] != 'accepted'
                or execution['execution_epoch'] != 0 or execution['state_version'] != 0
                or execution['reply_id'] or execution['snapshot_id']):
            return invalid('Unclaimed admission is not a fresh Task execution')
        starts = [c for c in commands if c['kind'] == 'StartTask']
        stops = [c for c in commands if c['kind'] == 'StopTask']
        if task['status'] == 'cancelling':
            return result('unstarted_stop_pending', 'Dedicated control settlement must verify never-claimed Start history')
        if len(starts) != 1 or stops or any(c['kind'] == 'ResumeInteraction' for c in commands):
            return invalid('Fresh admission lacks one unclaimed Start command')
        if starts[0]['expected_epoch'] != session['execution_epoch']:
            return invalid('Start observes a different Session epoch')
        if session['storage_node_id'] != storage_node_id:
            return result('foreign_storage', 'Admitted Workspace is on another storage node')
        return result('ready_unclaimed', 'Candidate only: transactional claim and authorization still required')
    if commands:
        return result('blocked_command', 'Non-execution command requires its own handler')
    return result('idle', 'No active Product admission or Runtime execution')


async def inspect_sessions(connection, *, worker_id: str, storage_node_id: str) -> tuple[SessionObservation, ...]:
    """Classify a consistent metadata view without exposing state or secret payloads.

    Do not use categories as authorization. In particular local_* classifications
    require a process-local execution registry not available to startup queries.
    One blocked Session does not prevent unrelated admitted Tasks from running.
    """
    if any(not isinstance(value, str) or not value or len(value) > 128
           for value in (worker_id, storage_node_id)):
        raise ValueError('Worker and storage node require nonempty bounded identities')
    rows = await connection.fetch('''
        SELECT to_jsonb(s)-'snapshot' AS session, to_jsonb(v) AS conversation,
            to_jsonb(t) AS task, to_jsonb(e) AS execution, to_jsonb(p) AS suspension,
            CASE WHEN b.id IS NULL THEN NULL ELSE jsonb_build_object(
                'id',b.id,'task_id',b.task_id,'session_id',b.session_id,'reply_id',b.reply_id,
                'status',b.status,'state_version',b.state_version,'execution_epoch',b.execution_epoch,
                'runtime_seq',b.runtime_seq) END AS boundary,
            (SELECT jsonb_build_object('event',to_jsonb(re),
                'start',to_jsonb(sc)-'payload'-'content_digest','stop',to_jsonb(st)-'payload'-'content_digest',
                'other_events',EXISTS(SELECT 1 FROM runtime_events other WHERE other.task_id=t.id AND other.id<>re.id),
                'other_commands',EXISTS(SELECT 1 FROM runtime_commands other WHERE other.task_id=t.id AND other.id<>sc.id))
                FROM runtime_events re
                LEFT JOIN runtime_commands sc ON sc.id=re.payload->>'start_command_id'
                LEFT JOIN runtime_commands st ON st.id=re.payload->>'stop_command_id'
                WHERE re.session_id=s.id AND re.task_id=t.id AND re.runtime_seq=s.runtime_seq
                    AND re.payload->>'type'='EXECUTION_NOT_STARTED') AS control_boundary,
            s.lease_until>clock_timestamp() AS lease_valid,
            EXISTS(SELECT 1 FROM runtime_task_executions orphan
                WHERE orphan.session_id=s.id AND orphan.status IN
                    ('preparing','running','awaiting_interaction','stopping')
                  AND orphan.task_id IS DISTINCT FROM coalesce(s.active_task_id,v.active_product_task_id))
                AS orphan_execution,
            coalesce((SELECT jsonb_agg(jsonb_build_object(
                'id',c.id,'task_id',c.task_id,'conversation_id',c.conversation_id,'kind',c.kind,
                'actor_user_id',c.actor_user_id,'protocol_version',c.protocol_version,
                'status',c.status,'expected_epoch',c.expected_epoch,
                'claimed_by',c.claimed_by,'claimed_epoch',c.claimed_epoch) ORDER BY c.id)
                FROM runtime_commands c WHERE c.session_id=s.id
                    AND c.status IN ('accepted','claimed','indeterminate')), '[]'::jsonb) AS commands
        FROM runtime_sessions s LEFT JOIN conversations v ON v.id=s.conversation_id
        LEFT JOIN tasks t ON t.id=coalesce(s.active_task_id,v.active_product_task_id)
        LEFT JOIN runtime_task_executions e ON e.task_id=t.id
        LEFT JOIN runtime_snapshots b ON b.id=s.snapshot_id
        LEFT JOIN runtime_suspensions p ON p.snapshot_id=s.snapshot_id
        WHERE s.superseded_at IS NULL
        ORDER BY s.id
    ''')
    return tuple(_classify(row, worker_id, storage_node_id) for row in rows)
