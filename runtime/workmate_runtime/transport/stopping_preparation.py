"""Trusted no-Reply cancellation boundary for never-started Task execution."""
from dataclasses import dataclass
import json
import uuid

from workmate_runtime.transport.stop_authority import admitted_stop_actor
from workmate_runtime.transport.postgres import ExecutionOwner, LostOwnership, _lease_seconds
from workmate_runtime.transport.stopping import StopStore


@dataclass(frozen=True)
class PreparationStopClaim:
    owner: ExecutionOwner
    command_id: str
    start_command_id: str
    unclaimed: bool
    state_version: int
    input_snapshot_id: str | None


class PreparationStopStore(StopStore):
    async def _state(self, rows, worker, node, *, finishing=False):
        conversation,task,session,execution,command=rows
        if (command['kind']!='StopTask' or command['protocol_version']!=1
                or command['status']!=('claimed' if finishing else 'accepted')
                or task['status']!='cancelling'
                or command['conversation_id']!=conversation['id'] or command['task_id']!=task['id']
                or command['session_id']!=session['id'] or session['conversation_id']!=conversation['id']
                or task['conversation_id']!=conversation['id'] or execution['session_id']!=session['id']
                or conversation['active_product_task_id']!=task['id'] or session['storage_node_id']!=node
                or execution['reply_id'] is not None or execution['snapshot_id'] is not None):
            return None
        if not await admitted_stop_actor(self.db, command, conversation):
            return None
        starts=await self.db.fetch("""SELECT * FROM runtime_commands WHERE task_id=$1
            AND kind='StartTask' ORDER BY id FOR UPDATE""",task['id'])
        if len(starts)!=1:
            return None
        start=starts[0]
        if (start['session_id']!=session['id'] or start['conversation_id']!=conversation['id']
                or start['actor_user_id']!=conversation['user_id'] or start['protocol_version']!=1):
            return None
        unclaimed=start['status']=='rejected' and all(start[k] is None for k in ('claimed_by','claimed_epoch','claimed_at'))
        if await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1 AND id<>$2
            AND id<>$3 AND status IN ('accepted','claimed','indeterminate')""",task['id'],command['id'],start['id']):
            return None
        if unclaimed and not finishing:
            if (session['active_task_id'] is not None or session['owner_id'] is not None
                    or session['lease_until'] is not None or session['status'] not in
                    {'accepted','completed','failed','cancelled','interrupted'}
                    or execution['status']!='accepted' or execution['execution_epoch']!=0
                    or execution['state_version']!=0 or execution['input_snapshot_id'] is not None
                    or command['expected_epoch']!=session['execution_epoch']
                    or start['expected_epoch']!=session['execution_epoch']):
                return None
        else:
            status='stopping' if finishing else 'preparing'
            if (session['owner_id']!=worker or session['active_task_id']!=task['id']
                    or session['reply_id'] is not None or session['execution_epoch']<1
                    or session['status']!=status or execution['status']!=status
                    or execution['execution_epoch']!=session['execution_epoch']
                    or execution['state_version']!=session['state_version']
                    or execution['input_snapshot_id']!=session['snapshot_id']
                    or not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',session['id'])
                    or (not unclaimed and (start['status']!='claimed' or start['claimed_by']!=worker
                        or start['claimed_epoch']!=session['execution_epoch']))
                    or (finishing and (command['claimed_by']!=worker or command['claimed_epoch']!=session['execution_epoch']))):
                return None
        if not (unclaimed and not finishing):
            if (start['expected_epoch']!=session['execution_epoch']-1
                    or command['expected_epoch']!=session['execution_epoch']-int(unclaimed)):
                return None
        if await self.db.fetchval('SELECT 1 FROM runtime_events WHERE task_id=$1 LIMIT 1',task['id']):
            return None
        return start,unclaimed

    async def claim(self, command_id, worker_id, node_id, *, lease_seconds, unclaimed_only=False):
        seconds=_lease_seconds(lease_seconds)
        if not isinstance(worker_id,str) or not worker_id.strip() or len(worker_id)>128:
            raise ValueError('A bounded Worker identity is required')
        async with self.db.transaction():
            rows=await self._locked(command_id)
            state=await self._state(rows,worker_id,node_id) if rows else None
            if state is None or (unclaimed_only and not state[1]):
                return None
            start,unclaimed=state
            _,task,session,_,_=rows
            epoch=session['execution_epoch']+int(unclaimed)
            await self.db.execute("""UPDATE runtime_sessions SET owner_id=$2,active_task_id=$3,
                execution_epoch=$4,status='stopping',reply_id=NULL,
                lease_until=CASE WHEN $5 THEN clock_timestamp()+($6::double precision*interval '1 second') ELSE lease_until END
                WHERE id=$1""",session['id'],worker_id,task['id'],epoch,unclaimed,seconds)
            await self.db.execute("""UPDATE runtime_task_executions SET status='stopping',execution_epoch=$2,
                state_version=$3,input_snapshot_id=$4 WHERE task_id=$1""",
                task['id'],epoch,session['state_version'],session['snapshot_id'])
            await self.db.execute("""UPDATE runtime_commands SET status='claimed',claimed_by=$2,
                claimed_epoch=$3,claimed_at=clock_timestamp() WHERE id=$1""",command_id,worker_id,epoch)
            return PreparationStopClaim(ExecutionOwner(session['id'],task['id'],worker_id,epoch),
                command_id,start['id'],unclaimed,session['state_version'],session['snapshot_id'])

    async def finish(self, claim, node_id, *, preparation_joined, resources_quiescent):
        """Only the controller with joined preparation and resource evidence calls this.

        Retains the prior Session snapshot verbatim. No Core invocation occurred,
        so there is no new Core state, Reply, metrics or tool result to invent.
        """
        if preparation_joined is not True or resources_quiescent is not True:
            raise ValueError('Joined preparation and scoped cleanup evidence are required')
        async with self.db.transaction():
            rows=await self._locked(claim.command_id)
            current=await self._state(rows,claim.owner.worker_id,node_id,finishing=True) if rows else None
            if (current is None or current[0]['id']!=claim.start_command_id or current[1]!=claim.unclaimed
                    or rows[2]['execution_epoch']!=claim.owner.execution_epoch
                    or rows[2]['state_version']!=claim.state_version or rows[2]['snapshot_id']!=claim.input_snapshot_id):
                raise LostOwnership('Preparation Stop authority changed before settlement')
            seq=await self.db.fetchval("""UPDATE runtime_sessions SET status='cancelled',runtime_seq=runtime_seq+1
                WHERE id=$1 RETURNING runtime_seq""",claim.owner.session_id)
            payload={'type':'EXECUTION_NOT_STARTED','session_id':claim.owner.session_id,'reply_id':None,
                'status':'cancelled','stop_command_id':claim.command_id,'start_command_id':claim.start_command_id,
                'preparation':'unclaimed' if claim.unclaimed else 'joined','resources_quiescent':True,
                'state_version':claim.state_version,'input_snapshot_id':claim.input_snapshot_id}
            await self.db.execute("""INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",str(uuid.uuid4()),claim.owner.session_id,seq,
                claim.owner.execution_epoch,claim.owner.task_id,json.dumps(payload))
            await self.db.execute("UPDATE runtime_task_executions SET status='cancelled' WHERE task_id=$1",claim.owner.task_id)
            await self.db.execute("UPDATE runtime_commands SET status='completed' WHERE id=$1",claim.command_id)
            if not claim.unclaimed:
                await self.db.execute("UPDATE runtime_commands SET status='completed' WHERE id=$1",claim.start_command_id)
            if not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',claim.owner.session_id):
                raise LostOwnership('Preparation Stop lease expired during settlement')
            from .postgres import stage_runtime_wakeup
            stage_runtime_wakeup(self.db)
            return claim.state_version,seq
