"""Late Stop completion from an exact terminal snapshot and resource evidence.

No execution ownership is acquired or renewed. A different process may settle
this command after the old lease expires because the matching immutable boundary
already proves complete drainage and resource quiescence. Delivery gates remain
Product-owned. Boundaries without explicit resource evidence require reconciliation.
"""
import json

from workmate_contracts import BoundaryEvidence,snapshot_digest
from workmate_runtime.transport.stop_authority import admitted_stop_actor
from workmate_runtime.transport.stopping import StopStore


_TERMINAL = {'completed','failed','interrupted'}
_REASONS = {'completed':'completed','error':'failed','interrupted':'interrupted',
            'exceed_max_iters':'interrupted'}


class TerminalStopStore:
    def __init__(self,connection):
        self.db=connection

    async def settle(self,command_id,worker_id,node_id):
        if any(not isinstance(v,str) or not v.strip() or len(v)>128
               for v in (command_id,worker_id,node_id)):
            raise ValueError('Command, Worker and storage node identities are required')
        async with self.db.transaction():
            rows=await StopStore(self.db)._locked(command_id)
            if rows is None:
                return False
            conversation,task,session,execution,command=rows
            if (command['kind']!='StopTask' or command['protocol_version']!=1
                    or command['task_id']!=task['id'] or command['session_id']!=session['id']
                    or command['conversation_id']!=conversation['id']
                    or task['conversation_id']!=conversation['id']
                    or session['conversation_id']!=conversation['id']
                    or execution['session_id']!=session['id'] or session['storage_node_id']!=node_id):
                return False
            if not await admitted_stop_actor(self.db, command, conversation):
                return False
            # An atomic completed command is its own receipt. A retry may arrive
            # after Product has released gates or admitted a later Task.
            if command['status']=='completed':
                return bool(command['claimed_by'] and command['claimed_epoch'] is not None)
            if (command['status']!='accepted' or command['claimed_by'] is not None
                    or command['claimed_epoch'] is not None
                    or task['status'] not in {'cancelling','cancelled'}
                    or conversation['active_product_task_id']!=task['id']
                    or session['active_task_id']!=task['id'] or not session['owner_id']
                    or session['execution_epoch']<1 or not session['reply_id']
                    or command['expected_epoch']!=session['execution_epoch']
                    or command['reply_id']!=session['reply_id']
                    or session['status'] not in _TERMINAL
                    or any(execution[k]!=session[k] for k in
                           ('status','execution_epoch','state_version','reply_id','snapshot_id'))):
                return False
            if await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE session_id=$1
                AND id<>$2 AND status IN ('accepted','claimed','indeterminate')""",session['id'],command_id):
                return False
            if not await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1
                AND session_id=$2 AND conversation_id=$3 AND actor_user_id=$4
                AND kind IN ('StartTask','ResumeInteraction') AND status='completed'
                AND claimed_by=$5 AND claimed_epoch=$6 AND protocol_version=1""",
                task['id'],session['id'],conversation['id'],conversation['user_id'],
                session['owner_id'],session['execution_epoch']):
                return False
            snapshot=await self.db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',session['snapshot_id'])
            if (not snapshot or snapshot['task_id']!=task['id'] or snapshot['session_id']!=session['id']
                    or any(snapshot[k]!=session[k] for k in
                           ('status','execution_epoch','state_version','runtime_seq','reply_id'))):
                return False
            boundary_row=await self.db.fetchrow('''SELECT payload FROM runtime_events
                WHERE session_id=$1 AND task_id=$2 AND execution_epoch=$3 AND runtime_seq=$4''',
                session['id'],task['id'],session['execution_epoch'],snapshot['runtime_seq'])
            # END is immediately before this terminal boundary, whether Core
            # events were persisted while running or buffered by Running Stop.
            end_row=await self.db.fetchrow('''SELECT payload FROM runtime_events
                WHERE session_id=$1 AND task_id=$2 AND execution_epoch=$3 AND runtime_seq=$4''',
                session['id'],task['id'],session['execution_epoch'],snapshot['runtime_seq']-1)
            if not boundary_row or not end_row:
                return False
            try:
                boundary=json.loads(boundary_row['payload'])
                end=json.loads(end_row['payload'])
                proof=boundary.get('resource_cleanup',{})
                evidence=BoundaryEvidence.model_validate(boundary.get('boundary_evidence'))
                valid=(boundary.get('type')=='EXECUTION_BOUNDARY'
                    and boundary.get('session_id',session['id'])==session['id']
                    and boundary.get('status')==snapshot['status']
                    and boundary.get('reply_id')==snapshot['reply_id']
                    and boundary.get('pending_tool_call_ids')==[]
                    and proof.get('core_drained') is True and proof.get('resources_quiescent') is True
                    and not evidence.pending_tools
                    and evidence.snapshot_sha256==snapshot_digest(json.loads(snapshot['payload']))
                    and end.get('type')=='REPLY_END' and end.get('reply_id')==snapshot['reply_id']
                    and end.get('session_id',session['id'])==session['id']
                    and _REASONS.get(end.get('finished_reason'))==snapshot['status'])
            except (ValueError,TypeError,AttributeError):
                return False
            if not valid:
                return False
            await self.db.execute("""UPDATE runtime_commands SET status='completed',claimed_by=$2,
                claimed_epoch=$3,claimed_at=clock_timestamp() WHERE id=$1""",
                command_id,worker_id,session['execution_epoch'])
            return True
