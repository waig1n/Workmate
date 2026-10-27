"""Atomic parked Stop claim and cleanup commit; no ordinary running write bypass."""
from dataclasses import dataclass
import json

from workmate_runtime.transport.stop_authority import admitted_stop_actor
from workmate_runtime.transport.postgres import ExecutionBoundary, ExecutionOwner, ExecutionStore, LostOwnership
from .suspension import ColdClaimRejected, claim_released_owner, read_recovery_bridge


@dataclass(frozen=True)
class ParkedStopClaim:
    owner: ExecutionOwner
    command_id: str
    boundary: ExecutionBoundary


class StopStore:
    def __init__(self, connection):
        self.db = connection

    async def _locked(self, command_id):
        db = self.db
        candidate = await db.fetchrow('''SELECT c.*,v.agent_id FROM runtime_commands c
            JOIN conversations v ON v.id=c.conversation_id WHERE c.id=$1''',command_id)
        if not candidate:
            return None
        # Ordering only: deleted Agent or revoked Agent-use access must not veto
        # a previously admitted owner's cancellation intent.
        await db.fetchval('SELECT id FROM agents WHERE id=$1 FOR SHARE',candidate['agent_id'])
        conversation = await db.fetchrow('SELECT * FROM conversations WHERE id=$1 FOR UPDATE',candidate['conversation_id'])
        task = await db.fetchrow('SELECT * FROM tasks WHERE id=$1 FOR UPDATE',candidate['task_id'])
        session = await db.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL FOR UPDATE',candidate['session_id'])
        execution = await db.fetchrow('SELECT * FROM runtime_task_executions WHERE task_id=$1 FOR UPDATE',candidate['task_id'])
        command = await db.fetchrow('SELECT * FROM runtime_commands WHERE id=$1 FOR UPDATE',command_id)
        if not all((conversation,task,session,execution,command)) or conversation['deleted_at'] is not None or conversation['agent_id'] != candidate['agent_id']:
            return None
        return conversation,task,session,execution,command

    async def _claim_state(self, rows, worker_id, node_id, *, finishing=False):
        conversation,task,session,execution,command = rows
        suspended = await self.db.fetchval('SELECT 1 FROM runtime_suspensions WHERE snapshot_id=$1',session['snapshot_id'])
        bridge = await read_recovery_bridge(self.db,ExecutionOwner(session['id'],task['id'],worker_id,session['execution_epoch']),_pending_claim=not finishing) if suspended else None
        if suspended and (not bridge or bridge['resume_command_id'] != command['id']):
            return None
        source_epoch = bridge['source_epoch'] if bridge else session['execution_epoch']
        status = 'stopping' if finishing else 'awaiting_interaction'
        if (command['kind'] != 'StopTask' or command['protocol_version'] != 1
                or command['status'] != ('claimed' if finishing else 'accepted')
                or task['status'] != 'cancelling'
                or command['conversation_id'] != conversation['id'] or command['task_id'] != task['id']
                or command['session_id'] != session['id'] or session['conversation_id'] != conversation['id']
                or task['conversation_id'] != conversation['id'] or execution['session_id'] != session['id']
                or conversation['active_product_task_id'] != task['id'] or session['active_task_id'] != task['id']
                or session['owner_id'] != worker_id or session['storage_node_id'] != node_id
                or not session['reply_id'] or session['execution_epoch'] < 1
                or any(execution[key] != session[key] for key in ('reply_id','execution_epoch','state_version','snapshot_id'))
                or any(row['status'] != status for row in (session,execution))
                or (finishing and (command['claimed_by'] != worker_id or command['claimed_epoch'] != session['execution_epoch']))):
            return None
        if not await admitted_stop_actor(self.db, command, conversation):
            return None
        if await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1 AND id<>$2
            AND kind IN ('ResumeInteraction','StopTask') AND status IN ('claimed','indeterminate')""",task['id'],command['id']):
            return None
        snapshot = await self.db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',session['snapshot_id'])
        if (not snapshot or snapshot['session_id'] != session['id'] or snapshot['task_id'] != task['id']
                or snapshot['reply_id'] != session['reply_id'] or snapshot['execution_epoch'] != source_epoch
                or snapshot['state_version'] != session['state_version'] or snapshot['runtime_seq'] != session['runtime_seq']
                or snapshot['status'] != 'awaiting_interaction'
                or not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',session['id'])):
            return None
        return ParkedStopClaim(ExecutionOwner(session['id'],task['id'],worker_id,session['execution_epoch']),
            command['id'],ExecutionBoundary(snapshot['state_version'],snapshot['runtime_seq'],snapshot['status'],
                snapshot['reply_id'],json.loads(snapshot['payload']),snapshot['id']))

    async def claim_parked(self, command_id, worker_id, node_id, *, lease_seconds=30):
        try:
            async with self.db.transaction():
                rows = await self._locked(command_id)
                if not rows:
                    return None
                if rows[2]['owner_id'] is None:
                    if not await claim_released_owner(self.db,rows[2],rows[-1],worker_id,node_id,lease_seconds=lease_seconds):
                        return None
                    rows = await self._locked(command_id)
                claim = await self._claim_state(rows,worker_id,node_id)
                if claim is None:
                    raise ColdClaimRejected()
                await self.db.execute("UPDATE runtime_sessions SET status='stopping' WHERE id=$1",claim.owner.session_id)
                await self.db.execute("UPDATE runtime_task_executions SET status='stopping' WHERE task_id=$1",claim.owner.task_id)
                await self.db.execute("""UPDATE runtime_commands SET status='claimed',claimed_by=$2,
                    claimed_epoch=$3,claimed_at=clock_timestamp() WHERE id=$1""",command_id,worker_id,claim.owner.execution_epoch)
                if not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',claim.owner.session_id):
                    raise ColdClaimRejected()
                return claim
        except ColdClaimRejected:
            return None

    async def finish(self, claim, node_id, *, snapshot, events, boundary):
        """Trusted drained-Core caller only. All cleanup records commit together.

        There is no append API for arbitrary stopping events. Caller must first
        prove every unfinished call closed and scoped resources quiescent.
        """
        if (not events or events[-1].get('type') != 'REPLY_END'
                or events[-1].get('finished_reason') != 'interrupted'
                or any(e.get('reply_id') != claim.boundary.reply_id or e.get('type') not in {
                    'TOOL_RESULT_START','TOOL_RESULT_TEXT_DELTA','TOOL_RESULT_END','REPLY_END'} for e in events)
                or any(e['type']=='TOOL_RESULT_END' and e.get('state')!='interrupted' for e in events)
                or boundary.get('type') != 'EXECUTION_BOUNDARY' or boundary.get('status') != 'interrupted'
                or boundary.get('reply_id') != claim.boundary.reply_id
                or boundary.get('pending_tool_call_ids') != []):
            raise ValueError('Only same-Reply interrupted cleanup can complete parked Stop')
        async with self.db.transaction():
            rows = await self._locked(claim.command_id)
            current = await self._claim_state(rows,claim.owner.worker_id,node_id,finishing=True) if rows else None
            if current != claim:
                raise LostOwnership('Stop ownership or original parked boundary changed during cleanup')
            result = await ExecutionStore(self.db).save_boundary(claim.owner,
                expected_state_version=claim.boundary.state_version,status='interrupted',reply_id=claim.boundary.reply_id,
                snapshot=snapshot,events=[*events,boundary])
            completed = await self.db.fetchval("""UPDATE runtime_commands SET status='completed'
                WHERE id=$1 AND kind='StopTask' AND status='claimed' AND claimed_by=$2 AND claimed_epoch=$3
                RETURNING id""",claim.command_id,claim.owner.worker_id,claim.owner.execution_epoch)
            if not completed or not await self.db.fetchval(
                'SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',claim.owner.session_id):
                raise LostOwnership('Stop claim or lease changed before cleanup could commit')
            return result
