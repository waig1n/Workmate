"""Same-owner running Stop, bound to the actual in-memory execution command."""
from dataclasses import dataclass
import json
from workmate_contracts.realtime import MessageSnapshotReference

from workmate_runtime.transport.stop_authority import admitted_stop_actor
from workmate_runtime.transport.postgres import ExecutionOwner, ExecutionStore, LostOwnership
from workmate_runtime.transport.stopping import StopStore


def _matches_terminal_status(end, status, boundary):
    reason = end.get('finished_reason')
    if 'execution_failure' not in boundary:
        return {'completed':'completed','error':'failed','interrupted':'interrupted',
                'exceed_max_iters':'interrupted'}.get(reason) == status
    failure = boundary['execution_failure']
    cleanup = boundary.get('resource_cleanup')
    if (status != 'failed'
            or reason not in {'completed','error','interrupted','exceed_max_iters'}
            or failure != {'code':'realtime_unavailable','finished_reason':reason}
            or not isinstance(cleanup,dict)
            or cleanup.get('core_drained') is not True
            or cleanup.get('resources_quiescent') is not True):
        return False
    if 'display_snapshot' in boundary:
        if 'display_empty' in boundary:
            return False
        try:
            MessageSnapshotReference.model_validate(boundary['display_snapshot'])
        except ValueError:
            return False
        return True
    return boundary.get('display_empty') is True


@dataclass(frozen=True)
class RunningStopClaim:
    owner: ExecutionOwner
    command_id: str
    execution_command_id: str
    reply_id: str
    state_version: int
    runtime_seq: int
    snapshot_id: str | None
    node_id: str


class RunningStopStore:
    def __init__(self, connection):
        self.db = connection

    async def _validate(self, rows, owner, original_id, *, finishing):
        if rows is None:
            return None
        conv, task, session, execution, command = rows
        status = 'stopping' if finishing else 'running'
        if (command['kind'] != 'StopTask' or command['protocol_version'] != 1
                or command['status'] != ('claimed' if finishing else 'accepted')
                or command['conversation_id'] != conv['id'] or command['task_id'] != owner.task_id
                or command['session_id'] != owner.session_id
                or task['id'] != owner.task_id or task['conversation_id'] != conv['id']
                or task['status'] != 'cancelling' or conv['active_product_task_id'] != owner.task_id
                or session['id'] != owner.session_id or session['conversation_id'] != conv['id']
                or session['active_task_id'] != owner.task_id or session['owner_id'] != owner.worker_id
                or session['execution_epoch'] != owner.execution_epoch or not session['reply_id']
                or execution['session_id'] != owner.session_id
                or any(execution[k] != session[k] for k in ('reply_id','execution_epoch','state_version'))
                or any(row['status'] != status for row in (session,execution))
                or (finishing and (command['claimed_by'] != owner.worker_id
                                    or command['claimed_epoch'] != owner.execution_epoch))):
            return None
        if not await admitted_stop_actor(self.db, command, conv):
            return None
        original = await self.db.fetchrow('SELECT * FROM runtime_commands WHERE id=$1 FOR UPDATE', original_id)
        if (not original or original['kind'] not in {'StartTask','ResumeInteraction'}
                or original['protocol_version'] != 1 or original['status'] != 'claimed'
                or original['session_id'] != owner.session_id or original['task_id'] != owner.task_id
                or original['conversation_id'] != conv['id'] or original['claimed_by'] != owner.worker_id
                or original['claimed_epoch'] != owner.execution_epoch
                or (original['kind'] == 'ResumeInteraction' and (original['reply_id'] != session['reply_id']
                    or json.loads(original['payload']).get('state_version') != session['state_version']
                    or json.loads(original['payload']).get('snapshot_id') != session['snapshot_id']
                    or execution['snapshot_id'] != session['snapshot_id']))
                or (original['kind'] == 'StartTask' and (execution['snapshot_id'] is not None
                    or execution['input_snapshot_id'] != session['snapshot_id']))):
            return None
        if await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1
            AND id<>$2 AND id<>$3 AND status IN ('claimed','indeterminate')""",
            owner.task_id,command['id'],original_id):
            return None
        if not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id):
            return None
        return RunningStopClaim(owner,command['id'],original_id,session['reply_id'],
            session['state_version'],session['runtime_seq'],session['snapshot_id'],session['storage_node_id'])

    async def claim_pending(self, owner, execution_command_id, node_id, *, command_ids=None):
        """Called only by the active controller, serialized with its stream writer."""
        async with self.db.transaction():
            command_id = await self.db.fetchval("""SELECT id FROM runtime_commands
                WHERE session_id=$1 AND task_id=$2 AND kind='StopTask' AND status='accepted'
                AND ($3::varchar[] IS NULL OR id=ANY($3::varchar[]))
                ORDER BY created_at,id LIMIT 1""",owner.session_id,owner.task_id,command_ids)
            if command_id is None:
                return None
            rows = await StopStore(self.db)._locked(command_id)
            claim = await self._validate(rows,owner,execution_command_id,finishing=False)
            if claim is None or claim.node_id != node_id:
                return None
            await self.db.execute("UPDATE runtime_sessions SET status='stopping' WHERE id=$1",owner.session_id)
            await self.db.execute("UPDATE runtime_task_executions SET status='stopping' WHERE task_id=$1",owner.task_id)
            await self.db.execute("""UPDATE runtime_commands SET status='claimed',claimed_by=$2,
                claimed_epoch=$3,claimed_at=clock_timestamp() WHERE id=$1""",
                command_id,owner.worker_id,owner.execution_epoch)
            if not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id):
                raise LostOwnership('Lease expired during running Stop claim')
            return claim

    async def finish(self, claim, *, status, snapshot, events, boundary):
        if (status not in {'completed','failed','interrupted'}
                or (events and (events[-1].get('type') != 'REPLY_END'
                or not _matches_terminal_status(events[-1], status, boundary)
                or sum(e.get('type')=='REPLY_END' for e in events) != 1))
                or any(e.get('reply_id') != claim.reply_id for e in events)
                or boundary.get('status') != status or boundary.get('reply_id') != claim.reply_id
                or boundary.get('type') != 'EXECUTION_BOUNDARY' or boundary.get('pending_tool_call_ids') != []):
            raise ValueError('Running Stop requires the actual fully drained terminal evidence')
        async with self.db.transaction():
            rows = await StopStore(self.db)._locked(claim.command_id)
            current = await self._validate(rows,claim.owner,claim.execution_command_id,finishing=True)
            if current != claim:
                raise LostOwnership('Running Stop or stream boundary changed during cleanup')
            if not events:
                # A naturally drained END may already have committed before
                # Stop won the final boundary lock. Bind its exact sequence;
                # never invent or duplicate an END to finish that Stop.
                durable = await self.db.fetchval('''SELECT payload FROM runtime_events
                    WHERE session_id=$1 AND task_id=$2 AND execution_epoch=$3 AND runtime_seq=$4''',
                    claim.owner.session_id,claim.owner.task_id,claim.owner.execution_epoch,claim.runtime_seq)
                end = json.loads(durable) if durable else {}
                if (end.get('type') != 'REPLY_END' or end.get('reply_id') != claim.reply_id
                        or not _matches_terminal_status(end, status, boundary)):
                    raise LostOwnership('No matching durable END for the running Stop boundary')
            result = await ExecutionStore(self.db).save_boundary(claim.owner,
                expected_state_version=claim.state_version,status=status,reply_id=claim.reply_id,
                snapshot=snapshot,events=[*events,boundary],completed_command_id=claim.execution_command_id)
            done = await self.db.fetchval("""UPDATE runtime_commands SET status='completed'
                WHERE id=$1 AND status='claimed' AND kind='StopTask' AND claimed_by=$2
                    AND claimed_epoch=$3 RETURNING id""",claim.command_id,claim.owner.worker_id,claim.owner.execution_epoch)
            if not done or not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',claim.owner.session_id):
                raise LostOwnership('Lease or Stop claim changed before atomic completion')
            return result
