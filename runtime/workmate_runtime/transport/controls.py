"""Same-owner permission control primitives, never lease takeover or tool replay."""
from dataclasses import dataclass
import json

from workmate_contracts.controls import ResumePayload
from workmate_contracts.external import ExternalResumePayload
from workmate_runtime.transport.postgres import ExecutionBoundary, ExecutionOwner, ExecutionStore, LostOwnership
from .suspension import ColdClaimRejected, claim_released_owner, read_recovery_bridge


async def read_resume_binding(connection, task_id):
    """Read the active identity on the caller's already locked transport connection."""
    return await connection.fetchrow('''SELECT s.id,s.owner_id,s.execution_epoch
        FROM runtime_sessions s JOIN runtime_task_executions e ON e.session_id=s.id
        WHERE e.task_id=$1 AND s.active_task_id=$1 AND s.superseded_at IS NULL''',task_id)


@dataclass(frozen=True)
class ResumeClaim:
    owner: ExecutionOwner
    command_id: str
    reply_id: str
    payload: ResumePayload | ExternalResumePayload
    boundary: ExecutionBoundary
    original_calls: tuple[dict, ...]


class ResumeStore:
    """Trusted Worker service; authorization callback uses this short transaction.

    authorize_resume(connection, actor_id, agent_id, task_id) must recheck current
    Product Agent/capability/RAG access, return exactly True, and never commit or
    perform external I/O. It is required at claim AND immediately before Core.
    """

    def __init__(self, connection):
        self.db = connection

    task_statuses = {'awaiting_permission'}
    payload_type = ResumePayload

    async def _load(self, command_id):
        db = self.db
        candidate = await db.fetchrow('''SELECT c.*,v.agent_id FROM runtime_commands c
            JOIN conversations v ON v.id=c.conversation_id WHERE c.id=$1''',command_id)
        if not candidate:
            return None
        agent = await db.fetchrow('SELECT * FROM agents WHERE id=$1 FOR SHARE',candidate['agent_id'])
        conversation = await db.fetchrow('SELECT * FROM conversations WHERE id=$1 FOR UPDATE',candidate['conversation_id'])
        task = await db.fetchrow('SELECT * FROM tasks WHERE id=$1 FOR UPDATE',candidate['task_id'])
        session = await db.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL FOR UPDATE',candidate['session_id'])
        execution = await db.fetchrow('SELECT * FROM runtime_task_executions WHERE task_id=$1 FOR UPDATE',candidate['task_id'])
        command = await db.fetchrow('SELECT * FROM runtime_commands WHERE id=$1 FOR UPDATE',command_id)
        if not all((agent,conversation,task,session,execution,command)):
            return None
        return agent,conversation,task,session,execution,command

    async def _validate(self, rows, worker_id, node_id, authorize, *, claimed):
        agent,conversation,task,session,execution,command = rows
        suspended = await self.db.fetchval('SELECT 1 FROM runtime_suspensions WHERE snapshot_id=$1',session['snapshot_id'])
        bridge = await read_recovery_bridge(self.db,ExecutionOwner(session['id'],task['id'],worker_id,session['execution_epoch']),_pending_claim=not claimed) if suspended else None
        if suspended and (self.payload_type is not ResumePayload or not bridge or bridge['resume_command_id'] != command['id']):
            return None
        source_epoch = bridge['source_epoch'] if bridge else session['execution_epoch']
        if bridge and claimed:
            from .full_access import cold_permission_proof_matches
            if not await cold_permission_proof_matches(self.db,session,task):
                return None
        if (command['kind'] != 'ResumeInteraction' or command['protocol_version'] != 1
                or command['status'] != ('claimed' if claimed else 'accepted')
                or command['session_id'] != session['id'] or command['task_id'] != task['id']
                or conversation['deleted_at'] is not None or command['conversation_id'] != conversation['id'] or agent['deleted_at'] is not None
                or conversation['agent_id'] != agent['id'] or task['conversation_id'] != conversation['id']
                or conversation['user_id'] != command['actor_user_id']
                or conversation['active_product_task_id'] != task['id']
                or session['conversation_id'] != conversation['id'] or session['active_task_id'] != task['id']
                or session['owner_id'] != worker_id or session['storage_node_id'] != node_id
                or execution['session_id'] != session['id'] or task['status'] not in self.task_statuses
                or command['expected_epoch'] != source_epoch
                or not command['reply_id'] or not command['interaction_id'] or not command['request_version']
                or any(row['status'] != 'awaiting_interaction'
                       or row['execution_epoch'] != session['execution_epoch']
                       or row['reply_id'] != command['reply_id'] for row in (session,execution))
                or (claimed and (command['claimed_by'] != worker_id
                                  or command['claimed_epoch'] != session['execution_epoch']))):
            return None
        payload = self.payload_type.model_validate(json.loads(command['payload']))
        if payload.full_access:
            from .full_access import full_access_matches
            if not await full_access_matches(self.db,command,payload,session,task,require_current=claimed):
                return None
        if any(row['snapshot_id'] != payload.snapshot_id
            or row['state_version'] != payload.state_version for row in (session,execution)):
            return None
        snapshot = await self.db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',payload.snapshot_id)
        if (not snapshot or snapshot['session_id'] != session['id'] or snapshot['task_id'] != task['id']
                or snapshot['state_version'] != payload.state_version or snapshot['reply_id'] != command['reply_id']
                or snapshot['execution_epoch'] != source_epoch
                or snapshot['status'] != 'awaiting_interaction' or snapshot['runtime_seq'] != session['runtime_seq']):
            return None
        if await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1 AND id<>$2
                AND kind='ResumeInteraction' AND status IN ('claimed','indeterminate')""", task['id'],command['id']):
            return None
        originals = await self._decision_matches(command,payload,snapshot)
        if originals is None:
            return None
        if (await self.db.fetchval('SELECT status FROM users WHERE id=$1',command['actor_user_id']) != 'active'
                or await authorize(self.db,command['actor_user_id'],agent['id'],task['id']) is not True
                or not await self.db.fetchval('SELECT lease_until > clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',session['id'])):
            return None
        return ResumeClaim(ExecutionOwner(session['id'],task['id'],worker_id,session['execution_epoch']),
            command['id'],command['reply_id'],payload,
            ExecutionBoundary(snapshot['state_version'],snapshot['runtime_seq'],snapshot['status'],
                              snapshot['reply_id'],json.loads(snapshot['payload']),snapshot['id']),originals)

    async def _decision_matches(self, command, payload, snapshot):
        saved = await self.db.fetchrow('SELECT * FROM task_projection_states WHERE task_id=$1',command['task_id'])
        if (not saved or saved['session_id'] != command['session_id'] or saved['reply_id'] != command['reply_id']
                or saved['format_version'] != 1 or saved['last_runtime_seq'] != snapshot['runtime_seq']):
            return None
        state = json.loads(saved['state'])
        record = state.get('interactions',{}).get(command['interaction_id'])
        pending = next((p for p in state.get('pending_interactions',[]) if p['interaction_id']==command['interaction_id']),None)
        ids = {d.tool_call_id for d in payload.decisions}
        if (state.get('interaction_boundary_id') != payload.snapshot_id or not record or not pending
                or record['reply_id'] != command['reply_id']
                or any(p['kind'] != 'permission' or p['request_version'] != command['request_version'] for p in (record,pending))
                or not ids <= set(pending['pending_tool_call_ids'])):
            return None
        rows = await self.db.fetch('''SELECT event_type,payload FROM task_events WHERE task_id=$1
            AND event_type IN ('hitl_request','hitl_decision','hitl_abandoned','hitl_resume_exhausted')''',command['task_id'])
        originals, decision = None, False
        expected = [{'toolCallId':d.tool_call_id,'approved':d.approved} for d in payload.decisions]
        ids = {d.tool_call_id for d in payload.decisions}
        for row in rows:
            item = json.loads(row['payload'])
            if row['event_type'] in {'hitl_abandoned','hitl_resume_exhausted'} and item.get('decisionVersion') == command['request_version']:
                return None
            if row['event_type'] == 'hitl_request':
                if item.get('decisionVersion') == command['request_version'] and item.get('replyId') == command['reply_id']:
                    calls = item.get('toolCalls',[])
                    if (not ids <= {c['id'] for c in calls}
                            or json.dumps(calls,sort_keys=True) != json.dumps(record.get('tool_calls'),sort_keys=True)):
                        return None
                    originals = tuple(c for c in calls if c['id'] in ids)
            if row['event_type'] == 'hitl_decision':
                if item.get('decisionVersion') == payload.decision_version:
                    # JSON spelling comparison distinguishes true from numeric 1.
                    if (item.get('requestVersion') != command['request_version']
                            or item.get('actorUserId') != command['actor_user_id'] or item.get('scope') != ('task' if payload.full_access else 'current_batch')
                            or (payload.full_access and item.get('permissionMode')!='bypass')
                            or json.dumps(item.get('decisions'),sort_keys=True) != json.dumps(expected,sort_keys=True)):
                        return None
                    decision = True
                elif ids & {d.get('toolCallId') for d in item.get('decisions',[])}:
                    return None
        return originals if decision else None

    async def claim_resume(self, command_id, worker_id, node_id, *, authorize_resume, lease_seconds=30):
        try:
            async with self.db.transaction():
                rows = await self._load(command_id)
                if not rows:
                    return None
                if rows[3]['owner_id'] is None:
                    if self.payload_type is not ResumePayload or not await claim_released_owner(
                            self.db,rows[3],rows[-1],worker_id,node_id,lease_seconds=lease_seconds):
                        return None
                    rows = await self._load(command_id)
                claim = await self._validate(rows,worker_id,node_id,authorize_resume,claimed=False)
                if claim is None:
                    raise ColdClaimRejected()
                await self.db.execute("""UPDATE runtime_commands SET status='claimed',claimed_by=$2,
                    claimed_epoch=$3,claimed_at=clock_timestamp() WHERE id=$1""",command_id,worker_id,claim.owner.execution_epoch)
                if not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',claim.owner.session_id):
                    raise ColdClaimRejected()
                return claim
        except ColdClaimRejected:
            return None

    async def begin_resume(self, claim, node_id, *, authorize_resume, event):
        """Save same-Reply resume signal before any Core effect; only once."""
        async with self.db.transaction():
            rows = await self._load(claim.command_id)
            current = await self._validate(rows,claim.owner.worker_id,node_id,authorize_resume,claimed=True) if rows else None
            if current != claim:
                raise LostOwnership('Resume command, authority or exact boundary changed during preparation')
            if (event.get('type') != 'REPLY_START' or event.get('reply_id') != claim.reply_id
                    or event.get('session_id') != claim.owner.session_id):
                raise ValueError('Resume signal must retain the original Session/Reply')
            await self.db.execute("UPDATE runtime_sessions SET status='running' WHERE id=$1",claim.owner.session_id)
            await self.db.execute("UPDATE runtime_task_executions SET status='running' WHERE task_id=$1",claim.owner.task_id)
            # Nested savepoint shares our locks; failed outbox/lease check also
            # rolls back running. Core itself emits no start for permission resume.
            return await ExecutionStore(self.db).append_events(claim.owner,claim.reply_id,[event])
