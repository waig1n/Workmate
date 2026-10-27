"""Exact permission suspension and released-boundary cold ownership; no unsafe replay."""
from dataclasses import dataclass
import json

from pydantic import BaseModel, ConfigDict, Field
from workmate_contracts import snapshot_digest
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.transport.postgres import ExecutionOwner, LostOwnership


@dataclass(frozen=True)
class SuspensionClaim:
    owner: ExecutionOwner
    node_id: str
    snapshot_id: str
    reply_id: str
    state_version: int
    runtime_seq: int
    resource_generation: str
    snapshot_digest: str


class CleanupReceipt(BaseModel):
    """Trusted lifecycle adapter evidence, not an API input or cryptographic proof."""
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: int = Field(ge=1, le=1)
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    execution_epoch: int = Field(ge=1)
    snapshot_id: str = Field(min_length=1)
    snapshot_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    node_id: str = Field(min_length=1)
    resource_generation: str = Field(min_length=1)
    resources_quiescent: bool
    workspace_closed: bool
    storage_proof: dict | None = None


class SuspensionStore:
    def __init__(self, connection):
        self.db = connection

    async def _locked(self, owner, snapshot_id):
        db = self.db
        candidate = await db.fetchrow('''SELECT t.conversation_id,c.agent_id FROM tasks t
            JOIN conversations c ON c.id=t.conversation_id WHERE t.id=$1''', owner.task_id)
        if not candidate:
            return None
        agent = await db.fetchrow('SELECT * FROM agents WHERE id=$1 FOR SHARE', candidate['agent_id'])
        conversation = await db.fetchrow('SELECT * FROM conversations WHERE id=$1 FOR UPDATE', candidate['conversation_id'])
        task = await db.fetchrow('SELECT * FROM tasks WHERE id=$1 FOR UPDATE', owner.task_id)
        session = await db.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL FOR UPDATE', owner.session_id)
        execution = await db.fetchrow('SELECT * FROM runtime_task_executions WHERE task_id=$1 FOR UPDATE', owner.task_id)
        suspension = await db.fetchrow('SELECT * FROM runtime_suspensions WHERE snapshot_id=$1 FOR UPDATE',snapshot_id)
        if not all((agent, conversation, task, session, execution)):
            return None
        return agent, conversation, task, session, execution, suspension

    async def _validate(self, rows, owner, node_id, snapshot_id, generation, *, releasing=False):
        agent, conversation, task, session, execution, _ = rows
        if (agent['deleted_at'] is not None or conversation['deleted_at'] is not None
                or conversation['agent_id'] != agent['id'] or task['conversation_id'] != conversation['id']
                or conversation['active_product_task_id'] != owner.task_id or task['status'] not in ({'awaiting_permission','cancelling'} if releasing else {'awaiting_permission'})
                or session['conversation_id'] != conversation['id'] or session['active_task_id'] != owner.task_id
                or session['owner_id'] != owner.worker_id or session['storage_node_id'] != node_id
                or session['execution_epoch'] != owner.execution_epoch or execution['session_id'] != owner.session_id
                or not session['reply_id'] or session['snapshot_id'] != snapshot_id
                or any(row['status'] != 'awaiting_interaction' for row in (session,execution))
                or any(execution[k] != session[k] for k in ('snapshot_id','state_version','reply_id','execution_epoch'))):
            return None
        if (not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id)
                or await self.db.fetchval("""SELECT 1 FROM runtime_commands WHERE task_id=$1
                    AND kind IN ('ResumeInteraction','StopTask') AND status=ANY($2::varchar[])""",owner.task_id,
                    ['claimed','indeterminate'] if releasing else ['accepted','claimed','indeterminate'])):
            return None
        snap = await self.db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1', snapshot_id)
        if (not snap or snap['session_id'] != owner.session_id or snap['task_id'] != owner.task_id
                or snap['status'] != 'awaiting_interaction'
                or any(snap[k] != session[k] for k in ('state_version','runtime_seq','reply_id','execution_epoch'))):
            return None
        try:
            payload = json.loads(snap['payload'])
            core = CoreSnapshot.model_validate(payload)
            state = core.to_state()
            if (state.session_id != owner.session_id or state.reply_id != session['reply_id']
                    or not state.context or state.context[-1].id != state.reply_id
                    or state.context[-1].role != 'assistant'):
                return None
            pending = state.get_awaiting_tool_calls(state.context[-1].name)
            if not pending or any(call.state != 'asking' for call in pending):
                return None
            for message in state.context:
                closed = {r.id for r in message.get_content_blocks('tool_result')}
                if any(c.state == 'submitted' and c.id not in closed for c in message.get_content_blocks('tool_call')):
                    return None
        except (ValueError, TypeError):
            return None
        return SuspensionClaim(owner,node_id,snapshot_id,session['reply_id'],session['state_version'],
            session['runtime_seq'],generation,snapshot_digest(payload))

    @staticmethod
    def _matches(row, claim):
        return row is not None and all(row[key] == value for key,value in {
            'snapshot_id':claim.snapshot_id,'session_id':claim.owner.session_id,'task_id':claim.owner.task_id,
            'reply_id':claim.reply_id,'storage_node_id':claim.node_id,'source_worker_id':claim.owner.worker_id,
            'source_epoch':claim.owner.execution_epoch,'state_version':claim.state_version,
            'runtime_seq':claim.runtime_seq,'resource_generation':claim.resource_generation,
            'snapshot_digest':claim.snapshot_digest}.items())

    async def begin(self, owner, node_id, snapshot_id, resource_generation):
        if (not isinstance(owner,ExecutionOwner) or type(owner.execution_epoch) is not int or owner.execution_epoch < 1
                or any(not isinstance(v,str) or not v.strip() for v in
                       (node_id,snapshot_id,resource_generation,owner.session_id,owner.task_id,owner.worker_id))):
            raise ValueError('Suspension requires complete owner, storage and resource identities')
        async with self.db.transaction():
            rows = await self._locked(owner,snapshot_id)
            if not rows or rows[-1] is not None:
                return None
            claim = await self._validate(rows,owner,node_id,snapshot_id,resource_generation)
            if claim is None:
                return None
            await self.db.execute('''INSERT INTO runtime_suspensions(snapshot_id,session_id,task_id,reply_id,
                storage_node_id,source_worker_id,source_epoch,state_version,runtime_seq,resource_generation,
                snapshot_digest,status) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'releasing')''',
                claim.snapshot_id,owner.session_id,owner.task_id,claim.reply_id,node_id,owner.worker_id,
                owner.execution_epoch,claim.state_version,claim.runtime_seq,resource_generation,claim.snapshot_digest)
            if not await self.db.fetchval(
                    'SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id):
                raise LostOwnership('Lease expired before suspension admission committed')
            return claim

    async def release(self, claim, cleanup_receipt):
        proof = CleanupReceipt.model_validate(cleanup_receipt)
        if (not proof.resources_quiescent or not proof.workspace_closed
                or any(getattr(proof,k) != value for k,value in {
                    'session_id':claim.owner.session_id,'task_id':claim.owner.task_id,'worker_id':claim.owner.worker_id,
                    'execution_epoch':claim.owner.execution_epoch,'snapshot_id':claim.snapshot_id,
                    'snapshot_digest':claim.snapshot_digest,'node_id':claim.node_id,
                    'resource_generation':claim.resource_generation}.items())):
            raise ValueError('Cleanup evidence must bind the exact suspension and prove full closure')
        async with self.db.transaction():
            rows = await self._locked(claim.owner,claim.snapshot_id)
            if rows and self._matches(rows[-1],claim) and rows[-1]['status'] == 'released':
                # A lost commit response is reconciled from the immutable source
                # identity and exact receipt, never by touching a later owner.
                stored = rows[-1]['cleanup_receipt']
                if json.dumps(json.loads(stored),sort_keys=True) != json.dumps(proof.model_dump(),sort_keys=True):
                    raise LostOwnership('Released suspension has different cleanup evidence')
                return
            current = await self._validate(rows,claim.owner,claim.node_id,claim.snapshot_id,claim.resource_generation,releasing=True) if rows else None
            if current != claim or not self._matches(rows[-1],claim) or rows[-1]['status'] != 'releasing':
                raise LostOwnership('Suspension boundary or control admission changed during cleanup')
            await self.db.execute("""UPDATE runtime_suspensions SET status='released',cleanup_receipt=$2::jsonb,
                released_at=clock_timestamp() WHERE snapshot_id=$1""",claim.snapshot_id,proof.model_dump_json())
            released = await self.db.fetchval('''UPDATE runtime_sessions SET owner_id=NULL,lease_until=NULL
                WHERE id=$1 AND lease_until>clock_timestamp() RETURNING id''',claim.owner.session_id)
            if not released:
                raise LostOwnership('Lease expired before suspension release committed')

    async def block(self, claim):
        """Latch cleanup failure without clearing the original execution ownership."""
        async with self.db.transaction():
            rows = await self._locked(claim.owner,claim.snapshot_id)
            if not rows or not self._matches(rows[-1],claim) or rows[-1]['status'] != 'releasing':
                raise LostOwnership('Suspension no longer matches cleanup attempt')
            await self.db.execute("UPDATE runtime_suspensions SET status='blocked' WHERE snapshot_id=$1",claim.snapshot_id)


class ColdClaimRejected(Exception):
    """Rollback provisional owner admission without changing user intent."""


def _released_snapshot_matches(row, snapshot):
    if (not row or row['status'] != 'released' or not snapshot
            or snapshot['status'] != 'awaiting_interaction'
            or any(snapshot[k] != row[v] for k,v in {
                'id':'snapshot_id','session_id':'session_id','task_id':'task_id','reply_id':'reply_id',
                'state_version':'state_version','runtime_seq':'runtime_seq','execution_epoch':'source_epoch'}.items())):
        return False
    try:
        payload = json.loads(snapshot['payload'])
        proof = CleanupReceipt.model_validate_json(row['cleanup_receipt'])
        state = CoreSnapshot.model_validate(payload).to_state()
        if (snapshot_digest(payload) != row['snapshot_digest'] or state.session_id != row['session_id']
                or state.reply_id != row['reply_id'] or not state.context or state.context[-1].id != state.reply_id):
            return False
        pending = state.get_awaiting_tool_calls(state.context[-1].name)
        if not pending or any(call.state != 'asking' for call in pending):
            return False
        for msg in state.context:
            closed = {r.id for r in msg.get_content_blocks('tool_result')}
            if any(c.state == 'submitted' and c.id not in closed for c in msg.get_content_blocks('tool_call')):
                return False
        return proof.resources_quiescent and proof.workspace_closed and all(getattr(proof,k) == row[v] for k,v in {
            'session_id':'session_id','task_id':'task_id','worker_id':'source_worker_id','execution_epoch':'source_epoch',
            'snapshot_id':'snapshot_id','snapshot_digest':'snapshot_digest','node_id':'storage_node_id',
            'resource_generation':'resource_generation'}.items())
    except (ValueError,TypeError):
        return False


async def read_recovery_bridge(db, owner, *, _pending_claim=False):
    """Read exact released-source/new-owner provenance; grants no execution itself."""
    session = await db.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id)
    execution = await db.fetchrow('SELECT * FROM runtime_task_executions WHERE task_id=$1',owner.task_id)
    if (not session or not execution or session['active_task_id'] != owner.task_id
            or session['owner_id'] != owner.worker_id or session['execution_epoch'] != owner.execution_epoch
            or execution['session_id'] != owner.session_id
            or any(execution[k] != session[k] for k in ('snapshot_id','execution_epoch','state_version','reply_id'))):
        return None
    if (session['status'] not in {'awaiting_interaction','stopping','running'}
            or execution['status'] != session['status']
            or not await db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id)):
        return None
    row = await db.fetchrow('SELECT * FROM runtime_suspensions WHERE snapshot_id=$1',session['snapshot_id'])
    snapshot = await db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',session['snapshot_id'])
    if (not _released_snapshot_matches(row,snapshot) or row['task_id'] != owner.task_id
            or row['storage_node_id'] != session['storage_node_id']
            or row['resume_worker_id'] != owner.worker_id or row['resume_epoch'] != owner.execution_epoch
            or row['resume_epoch'] != row['source_epoch']+1
            or any(row[k] != session[k] for k in ('state_version','runtime_seq','reply_id'))):
        return None
    command = await db.fetchrow('SELECT * FROM runtime_commands WHERE id=$1',row['resume_command_id'])
    if (not command or command['session_id'] != owner.session_id or command['task_id'] != owner.task_id
            or command['kind'] not in {'ResumeInteraction','StopTask'}
            or (command['kind']=='ResumeInteraction' and command['expected_epoch']!=row['source_epoch'])
            or (command['kind']=='StopTask' and not 0<=command['expected_epoch']<=row['source_epoch'])):
        return None
    if (_pending_claim and command['status'] == 'accepted'
            and command['claimed_by'] is None and command['claimed_epoch'] is None):
        return row  # Only the lock-holding admission transaction uses this before its final claim write.
    if (command['status'] not in {'claimed','completed'} or command['claimed_by'] != owner.worker_id
            or command['claimed_epoch'] != owner.execution_epoch):
        return None
    return row


async def claim_released_owner(db, session, command, worker_id, node_id, *, lease_seconds):
    """Caller holds product/session/execution locks and rolls back on later rejection."""
    from .postgres import _lease_seconds
    seconds = _lease_seconds(lease_seconds)
    if not isinstance(worker_id,str) or not worker_id.strip() or len(worker_id)>128:
        raise ValueError('A nonempty Worker identity is required')
    row = await db.fetchrow('SELECT * FROM runtime_suspensions WHERE snapshot_id=$1 FOR UPDATE',session['snapshot_id'])
    snapshot = await db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',session['snapshot_id'])
    if (not _released_snapshot_matches(row,snapshot) or row['resume_epoch'] is not None
            or session['superseded_at'] is not None
            or session['owner_id'] is not None or session['lease_until'] is not None
            or session['storage_node_id'] != node_id or row['storage_node_id'] != node_id
            or session['active_task_id'] != command['task_id'] or row['task_id'] != command['task_id']
            or command['status'] != 'accepted'
            or (command['kind'] == 'ResumeInteraction' and command['expected_epoch'] != row['source_epoch'])
            or (command['kind'] == 'StopTask' and not 0 <= command['expected_epoch'] <= row['source_epoch'])
            or command['session_id'] != session['id'] or row['session_id'] != session['id']
            or session['execution_epoch'] != row['source_epoch']
            or any(row[k] != session[k] for k in ('state_version','runtime_seq','reply_id'))):
        return False
    new_epoch = row['source_epoch']+1
    await db.execute('''UPDATE runtime_suspensions SET resume_worker_id=$2,resume_epoch=$3,resume_command_id=$4
        WHERE snapshot_id=$1''',row['snapshot_id'],worker_id,new_epoch,command['id'])
    await db.execute('''UPDATE runtime_sessions SET owner_id=$2,execution_epoch=$3,
        lease_until=clock_timestamp()+($4 * interval '1 second') WHERE id=$1''',session['id'],worker_id,new_epoch,seconds)
    await db.execute('UPDATE runtime_task_executions SET execution_epoch=$2 WHERE task_id=$1',command['task_id'],new_epoch)
    return True
