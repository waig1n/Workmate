"""Commit only a joined, quiescent preparation rejection before any Core Reply."""
import json
import uuid
from .postgres import LostOwnership
from .stopping import StopStore


class PreparationRejectionStore(StopStore):
    async def finish(self, owner, command_id, node_id, *, preparation_joined, resources_quiescent,
                     reason='sandbox_verification_failed'):
        if reason not in {'sandbox_verification_failed', 'model_unavailable'}:
            raise ValueError('Unsupported preparation rejection reason')
        if preparation_joined is not True or resources_quiescent is not True:
            raise ValueError('Joined preparation and scoped cleanup evidence are required')
        async with self.db.transaction():
            rows = await self._locked(command_id)
            if rows is None:
                raise LostOwnership('Preparation rejection identity unavailable')
            conv, task, session, execution, command = rows
            if (task['id'] != owner.task_id or task['status'] != 'created'
                    or task['conversation_id'] != conv['id'] or conv['active_product_task_id'] != task['id']
                    or session['id'] != owner.session_id or session['conversation_id'] != conv['id']
                    or session['active_task_id'] != task['id'] or session['owner_id'] != owner.worker_id
                    or session['storage_node_id'] != node_id or session['execution_epoch'] != owner.execution_epoch
                    or session['status'] != 'preparing' or session['reply_id'] is not None
                    or execution['session_id'] != session['id'] or execution['status'] != 'preparing'
                    or execution['execution_epoch'] != owner.execution_epoch or execution['reply_id'] is not None
                    or execution['snapshot_id'] is not None or execution['state_version'] != session['state_version']
                    or execution['input_snapshot_id'] != session['snapshot_id']
                    or command['kind'] != 'StartTask' or command['protocol_version'] != 1
                    or command['status'] != 'claimed' or command['claimed_by'] != owner.worker_id
                    or command['claimed_epoch'] != owner.execution_epoch or command['claimed_at'] is None
                    or command['expected_epoch'] != owner.execution_epoch - 1
                    or command['actor_user_id'] != conv['user_id'] or command['conversation_id'] != conv['id']
                    or command['session_id'] != session['id'] or command['task_id'] != task['id']
                    or not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',session['id'])
                    or await self.db.fetchval('SELECT 1 FROM runtime_events WHERE task_id=$1 LIMIT 1',task['id'])
                    or await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1 AND id<>$2
                        AND status IN ('accepted','claimed','indeterminate') LIMIT 1""",task['id'],command_id)):
                raise LostOwnership('Preparation rejection authority changed')
            seq = await self.db.fetchval("""UPDATE runtime_sessions SET status='failed',runtime_seq=runtime_seq+1
                WHERE id=$1 RETURNING runtime_seq""",session['id'])
            payload = {'type':'EXECUTION_NOT_STARTED','session_id':session['id'],'reply_id':None,
                'status':'failed','reason':reason,'start_command_id':command_id,
                'worker_id':owner.worker_id,'preparation':'joined','resources_quiescent':True,
                'state_version':session['state_version'],'input_snapshot_id':session['snapshot_id']}
            await self.db.execute('''INSERT INTO runtime_events(id,session_id,runtime_seq,execution_epoch,task_id,payload)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)''',str(uuid.uuid4()),session['id'],seq,
                owner.execution_epoch,task['id'],json.dumps(payload))
            await self.db.execute("UPDATE runtime_task_executions SET status='failed' WHERE task_id=$1",task['id'])
            await self.db.execute("UPDATE runtime_commands SET status='completed' WHERE id=$1",command_id)
            if not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',session['id']):
                raise LostOwnership('Preparation rejection lease expired during settlement')
            from .postgres import stage_runtime_wakeup
            stage_runtime_wakeup(self.db)
            return session['state_version'], seq
