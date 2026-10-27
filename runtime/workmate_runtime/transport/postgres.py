"""PostgreSQL execution authority for the native Worker.

Short transactions serialize claims and fence durable writes. Notifications are
lossy hints staged on the connection and published only by its outer owner.
"""
from workmate_product.application.sandbox_proof import sandbox_proof_valid

from dataclasses import dataclass
import json
import uuid


def stage_runtime_wakeup(connection, key="runtime"):
    stage = getattr(connection, 'stage_wakeup', None)
    if stage is not None:
        stage(key)


class LostOwnership(RuntimeError):
    """Lease, epoch, Task identity or snapshot revision no longer matches."""


@dataclass(frozen=True)
class ExecutionOwner:
    session_id: str
    task_id: str
    worker_id: str
    execution_epoch: int


@dataclass(frozen=True)
class ExecutionBoundary:
    state_version: int
    runtime_seq: int
    status: str
    reply_id: str | None
    snapshot: dict | None
    snapshot_id: str | None = None


def _lease_seconds(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 300:
        raise ValueError('lease_seconds must be an integer between 1 and 300')
    return value


def _check_reply_identity(reply_id: str) -> None:
    if not isinstance(reply_id, str) or not reply_id.strip() or len(reply_id) > 128:
        raise ValueError('reply_id must be a nonempty identity of at most 128 characters')


def _stream_payloads(reply_id: str, events: list[dict]) -> list[str]:
    """Validate stream batches before acquiring a connection or any locks."""
    _check_reply_identity(reply_id)
    if not isinstance(events, list) or not events:
        raise ValueError('A stream batch must be a nonempty list of event objects')
    if any(not isinstance(event, dict) or not event for event in events):
        raise ValueError('Stream events must be nonempty JSON objects')
    try:
        return [json.dumps(event, allow_nan=False) for event in events]
    except (TypeError, ValueError) as error:
        raise ValueError('Stream events must contain finite JSON values') from error


class ExecutionStore:
    """A connection-scoped repository; never holds a transaction during tools/I/O.

    Claim locks Agent → Conversation → Task → Session → Command; boundary
    writers take Task KEY SHARE before Session to order implicit event/snapshot
    foreign-key locks against concurrent claim/admission. The database clock
    determines lease validity. Losing a lease prohibits writes; it does not prove
    that the old tool exited and does not grant automatic retry or takeover.
    """

    def __init__(self, connection):
        self.connection = connection

    async def load_boundary(self, owner: ExecutionOwner) -> ExecutionBoundary:
        """Read one consistent committed revision for the current owner.

        A timeout around commit is reconciled by this revision, never by
        repeating the model/tool operation. This is not a public API query.
        """
        row = await self.connection.fetchrow("""
            SELECT s.state_version, s.runtime_seq, s.status, s.reply_id,
                b.payload AS snapshot, s.snapshot_id
            FROM runtime_sessions s
            LEFT JOIN runtime_snapshots b ON b.id=s.snapshot_id AND b.session_id=s.id
            WHERE s.id=$1 AND s.active_task_id=$2 AND s.owner_id=$3 AND s.execution_epoch=$4
                AND s.superseded_at IS NULL AND s.lease_until > clock_timestamp()
                AND ((s.state_version=0 AND s.snapshot_id IS NULL)
                     OR b.state_version=s.state_version)
        """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch)
        if row is None:
            raise LostOwnership('Cannot load a boundary for an expired or superseded owner')
        return ExecutionBoundary(
            row['state_version'], row['runtime_seq'], row['status'], row['reply_id'],
            json.loads(row['snapshot']) if row['snapshot'] is not None else None,
            row['snapshot_id'],
        )

    async def load_task_snapshot(self, session_id: str, task_id: str) -> ExecutionBoundary | None:
        """Read the last immutable Task boundary for internal reconciliation.

        This trusted repository query grants no execution rights, requires no
        live lease, and must not be exposed as an unauthenticated Product API.
        None means no saved boundary, not proof that no tools were executed.
        """
        row = await self.connection.fetchrow('''
            SELECT s.* FROM runtime_task_executions e
            JOIN runtime_snapshots s ON s.id=e.snapshot_id
                AND s.task_id=e.task_id AND s.session_id=e.session_id
                AND s.state_version=e.state_version
            WHERE e.session_id=$1 AND e.task_id=$2
        ''', session_id, task_id)
        if row is None:
            return None
        return ExecutionBoundary(row['state_version'], row['runtime_seq'], row['status'],
                                 row['reply_id'], json.loads(row['payload']), row['id'])

    async def claim_start(self, command_id: str, worker_id: str, storage_node_id: str,
                          *, lease_seconds: int) -> ExecutionOwner | None:
        seconds = _lease_seconds(lease_seconds)
        if not worker_id or len(worker_id) > 128:
            raise ValueError('worker_id must be a nonempty identity of at most 128 characters')
        db = self.connection
        async with db.transaction():
            candidate = await db.fetchrow("""
                SELECT c.session_id, c.conversation_id, c.task_id, conv.agent_id
                FROM runtime_commands c JOIN conversations conv ON conv.id=c.conversation_id
                WHERE c.id=$1
            """, command_id)
            if candidate is None:
                return None
            agent = await db.fetchrow('SELECT id, deleted_at FROM agents WHERE id=$1 FOR SHARE', candidate['agent_id'])
            conversation = await db.fetchrow('SELECT * FROM conversations WHERE id=$1 FOR UPDATE', candidate['conversation_id'])
            task = await db.fetchrow('SELECT id, conversation_id, status FROM tasks WHERE id=$1 FOR UPDATE', candidate['task_id'])
            session_id = candidate['session_id']
            session = await db.fetchrow('SELECT * FROM runtime_sessions WHERE id=$1 FOR UPDATE', session_id)
            residency = await db.fetchrow('SELECT * FROM workspace_residencies WHERE session_id=$1 FOR UPDATE',session_id)
            command = await db.fetchrow(
                'SELECT * FROM runtime_commands WHERE id=$1 FOR UPDATE', command_id)
            if not all((agent, conversation, task, session, command)):
                return None
            # Recheck every binding after locking; candidate lookup was only a
            # hint and must never authorize a cross-Conversation execution.
            if (conversation['deleted_at'] is not None or agent['deleted_at'] is not None or conversation['agent_id'] != agent['id']
                    or command['conversation_id'] != conversation['id']
                    or command['session_id'] != session['id']
                    or command['task_id'] != task['id']
                    or task['conversation_id'] != conversation['id']
                    or session['superseded_at'] is not None
                    or session['conversation_id'] != conversation['id']
                    or conversation['active_product_task_id'] != task['id']
                    or command['actor_user_id'] != conversation['user_id']
                    or task['status'] != 'created' or command['protocol_version'] != 1):
                return None
            if await db.fetchval('SELECT status FROM users WHERE id=$1', command['actor_user_id']) != 'active':
                return None
            if (command['kind'] != 'StartTask' or command['status'] != 'accepted'
                    or session['storage_node_id'] != storage_node_id
                    or session['active_task_id'] is not None
                    or session['status'] not in {'accepted', 'completed', 'failed', 'cancelled', 'interrupted'}
                    or session['execution_epoch'] != command['expected_epoch']):
                return None
            if (residency is not None and (residency['storage_node_id'] != storage_node_id
                        or residency['status'] in {'releasing','blocked'}
                        or (residency['status']=='held' and residency['holder_worker_id']!=worker_id))):
                return None
            execution = await db.fetchrow('''
                SELECT * FROM runtime_task_executions WHERE task_id=$1 FOR UPDATE
            ''', command['task_id'])
            if (execution is None or execution['session_id'] != session_id
                    or execution['status'] != 'accepted' or execution['execution_epoch'] != 0
                    or execution['snapshot_id'] is not None or execution['reply_id'] is not None):
                return None
            epoch = await db.fetchval("""
                UPDATE runtime_sessions SET owner_id=$2, active_task_id=$3,
                    execution_epoch=execution_epoch+1, status='preparing', reply_id=NULL,
                    lease_until=clock_timestamp()+($4::double precision * interval '1 second')
                WHERE id=$1 RETURNING execution_epoch
            """, session_id, worker_id, command['task_id'], seconds)
            await db.execute("""
                UPDATE runtime_task_executions SET status='preparing', execution_epoch=$2,
                    state_version=$3, input_snapshot_id=$4 WHERE task_id=$1
            """, command['task_id'], epoch, session['state_version'], session['snapshot_id'])
            await db.execute("""
                UPDATE runtime_commands SET status='claimed', claimed_by=$2,
                    claimed_epoch=$3, claimed_at=clock_timestamp() WHERE id=$1
            """, command_id, worker_id, epoch)
            return ExecutionOwner(session_id, command['task_id'], worker_id, epoch)

    async def renew(self, owner: ExecutionOwner, *, lease_seconds: int) -> None:
        seconds = _lease_seconds(lease_seconds)
        result = await self.connection.fetchval("""
            UPDATE runtime_sessions
            SET lease_until=clock_timestamp()+($5::double precision * interval '1 second')
            WHERE id=$1 AND active_task_id=$2 AND owner_id=$3 AND execution_epoch=$4
              AND superseded_at IS NULL AND lease_until > clock_timestamp()
            RETURNING id
        """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch, seconds)
        if result is None:
            raise LostOwnership('Cannot renew an expired or superseded owner')

    async def check_running(self, owner: ExecutionOwner, reply_id: str) -> None:
        """Check current authority before model/tool I/O without loading a snapshot.

        This is a point-in-time check, not a lease extension or guarantee about
        future external side effects. Boundary writes still enforce ownership.
        """
        _check_reply_identity(reply_id)
        valid = await self.connection.fetchval("""
            SELECT 1 FROM runtime_sessions s
            WHERE s.id=$1 AND s.active_task_id=$2 AND s.owner_id=$3 AND s.execution_epoch=$4
                AND s.superseded_at IS NULL AND s.lease_until > clock_timestamp() AND s.status='running' AND s.reply_id=$5
                AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=$2 AND t.status IN ('created','running','awaiting_permission'))
                AND EXISTS (SELECT 1 FROM runtime_task_executions e
                    WHERE e.task_id=$2 AND e.session_id=s.id AND e.execution_epoch=s.execution_epoch
                        AND e.status='running' AND e.reply_id=$5 AND e.state_version=s.state_version)
        """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch, reply_id)
        if valid is None:
            raise LostOwnership('Running execution authority no longer matches')

    async def begin_reply(self, owner: ExecutionOwner, reply_id: str, event: dict) -> int:
        """Atomically bind a fresh Reply and publish its first event.

        This transition is deliberately not repeatable. A commit of unknown
        outcome must be reconciled, never followed by another Agent execution.
        """
        payloads = _stream_payloads(reply_id, [event])
        db = self.connection
        async with db.transaction():
            task = await db.fetchrow('SELECT status,approval_profile FROM tasks WHERE id=$1 FOR UPDATE', owner.task_id)
            if not task or task['status'] not in {'created','running'}:
                raise LostOwnership('Product Stop or terminal state prevents a new Reply')
            if task['approval_profile'] == 'full_access':
                proof = await db.fetchrow('SELECT * FROM runtime_sandbox_attestations WHERE task_id=$1',owner.task_id)
                if (not proof or proof['session_id'] != owner.session_id or proof['worker_id'] != owner.worker_id
                        or proof['execution_epoch'] != owner.execution_epoch or not sandbox_proof_valid(proof)):
                    raise LostOwnership('Full Access requires current Task sandbox proof')
            row = await db.fetchrow("""
                UPDATE runtime_sessions SET status='running', reply_id=$5, runtime_seq=runtime_seq+1
                WHERE id=$1 AND active_task_id=$2 AND owner_id=$3 AND execution_epoch=$4
                    AND superseded_at IS NULL AND lease_until > clock_timestamp() AND status='preparing' AND reply_id IS NULL
                RETURNING runtime_seq, state_version
            """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch, reply_id)
            if row is None:
                raise LostOwnership('Reply is already bound or preparation ownership no longer matches')
            execution = await db.fetchval("""
                UPDATE runtime_task_executions SET status='running', reply_id=$4
                WHERE task_id=$1 AND session_id=$2 AND execution_epoch=$3
                    AND status='preparing' AND reply_id IS NULL AND state_version=$5
                RETURNING task_id
            """, owner.task_id, owner.session_id, owner.execution_epoch, reply_id, row['state_version'])
            if execution is None:
                raise LostOwnership('Task execution is already bound or preparation no longer matches')
            await self._insert_stream_events(owner, reply_id, row['runtime_seq'], payloads)
            return row['runtime_seq']

    async def append_events(self, owner: ExecutionOwner, reply_id: str, events: list[dict]) -> int:
        """Append live stream data without pretending it is a recoverable snapshot.

        The returned sequence is the last committed Runtime sequence. Snapshot
        state_version and Product/browser event sequences are unchanged.
        """
        payloads = _stream_payloads(reply_id, events)
        db = self.connection
        async with db.transaction():
            await db.fetchval('SELECT id FROM tasks WHERE id=$1 FOR KEY SHARE', owner.task_id)
            runtime_seq = await db.fetchval("""
                UPDATE runtime_sessions s SET runtime_seq=s.runtime_seq+$6
                WHERE s.id=$1 AND s.active_task_id=$2 AND s.owner_id=$3 AND s.execution_epoch=$4
                    AND s.superseded_at IS NULL AND s.lease_until > clock_timestamp() AND s.status='running' AND s.reply_id=$5
                    AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=$2 AND t.status IN ('created','running','awaiting_permission'))
                    AND EXISTS (SELECT 1 FROM runtime_task_executions e
                        WHERE e.task_id=$2 AND e.session_id=s.id AND e.execution_epoch=s.execution_epoch
                            AND e.status='running' AND e.reply_id=$5 AND e.state_version=s.state_version)
                RETURNING s.runtime_seq
            """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch, reply_id, len(payloads))
            if runtime_seq is None:
                raise LostOwnership('Stream owner, reply or running execution no longer matches')
            await self._insert_stream_events(owner, reply_id, runtime_seq, payloads)
            return runtime_seq

    async def _insert_stream_events(self, owner: ExecutionOwner, reply_id: str,
                                    last_seq: int, payloads: list[str]) -> None:
        """Caller holds Session in a transaction after acquiring Task KEY SHARE."""
        first_seq = last_seq - len(payloads) + 1
        await self.connection.executemany("""
            INSERT INTO runtime_events(id, session_id, runtime_seq, execution_epoch, task_id, payload)
            VALUES ($1,$2,$3,$4,$5,$6::jsonb)
        """, [(str(uuid.uuid4()), owner.session_id, first_seq + index,
                owner.execution_epoch, owner.task_id, payload)
               for index, payload in enumerate(payloads)])
        valid = await self.connection.fetchval("""
            SELECT lease_until > clock_timestamp() FROM runtime_sessions
            WHERE id=$1 AND active_task_id=$2 AND owner_id=$3 AND execution_epoch=$4
                AND superseded_at IS NULL AND status='running' AND reply_id=$5
        """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch, reply_id)
        if not valid:
            raise LostOwnership('Lease expired or stream ownership changed before events could commit')
        stage_runtime_wakeup(self.connection)

    async def save_boundary(self, owner: ExecutionOwner, *, expected_state_version: int,
                            status: str, reply_id: str | None, snapshot: dict,
                            events: list[dict], completed_command_id: str | None = None) -> tuple[int, int]:
        """Save a drained/parked snapshot and its outbox events atomically.

        Return (state_version, runtime_seq). A commit with an unknown result must
        be reconciled by reading the revision; it must not blindly repeat tools.
        Terminal writes retain active_task_id until Product closes delivery and
        explicitly admits another Task. No Product audit/projection is written here.
        """
        if status not in {'awaiting_interaction', 'completed', 'failed', 'cancelled', 'interrupted'}:
            raise ValueError('save_boundary accepts only parked or terminal states')
        if status in {'awaiting_interaction', 'completed'} and not reply_id:
            raise ValueError('A parked or completed reply requires reply_id')
        if not events:
            raise ValueError('A durable boundary requires an event')
        # Serialize before taking any locks. Agent state must already be checked
        # by CoreSnapshot; credential / public projection filtering lives above.
        snapshot_json = json.dumps(snapshot, allow_nan=False)
        event_json = [json.dumps(event, allow_nan=False) for event in events]
        db = self.connection
        async with db.transaction():
            # INSERT snapshot/event takes a Task FK KEY SHARE implicitly. Acquire
            # it first, otherwise a claimant holding Task and waiting on Session
            # can deadlock against this writer holding Session and waiting on Task.
            await db.fetchval('SELECT id FROM tasks WHERE id=$1 FOR KEY SHARE', owner.task_id)
            row = await db.fetchrow("""
                UPDATE runtime_sessions
                SET snapshot=$6::jsonb, status=$7, reply_id=$8,
                    state_version=state_version+1, runtime_seq=runtime_seq+$9
                WHERE id=$1 AND active_task_id=$2 AND owner_id=$3 AND execution_epoch=$4
                  AND state_version=$5 AND superseded_at IS NULL AND lease_until > clock_timestamp()
                  AND status IN ('preparing','running','awaiting_interaction','stopping')
                  AND (reply_id IS NULL OR reply_id=$8)
                RETURNING state_version, runtime_seq
            """, owner.session_id, owner.task_id, owner.worker_id, owner.execution_epoch,
                expected_state_version, snapshot_json, status, reply_id, len(events))
            if row is None:
                raise LostOwnership('Owner, reply, boundary revision or execution state no longer matches')
            snapshot_id = str(uuid.uuid4())
            await db.execute('''
                INSERT INTO runtime_snapshots(id, session_id, task_id, state_version,
                    runtime_seq, execution_epoch, status, reply_id, payload)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
            ''', snapshot_id, owner.session_id, owner.task_id, row['state_version'],
                row['runtime_seq'], owner.execution_epoch, status, reply_id, snapshot_json)
            execution = await db.fetchval('''
                UPDATE runtime_task_executions SET snapshot_id=$1, state_version=$2,
                    status=$3, reply_id=$4
                WHERE task_id=$5 AND session_id=$6 AND execution_epoch=$7
                    AND state_version=$8
                    AND status IN ('preparing','running','awaiting_interaction','stopping')
                    AND (reply_id IS NULL OR reply_id=$4)
                RETURNING task_id
            ''', snapshot_id, row['state_version'], status, reply_id, owner.task_id,
                owner.session_id, owner.execution_epoch, expected_state_version)
            if execution is None:
                raise LostOwnership('Task execution boundary no longer matches')
            await db.execute('UPDATE runtime_sessions SET snapshot_id=$2 WHERE id=$1',
                             owner.session_id, snapshot_id)
            first_seq = row['runtime_seq'] - len(events) + 1
            await db.executemany("""
                INSERT INTO runtime_events(id, session_id, runtime_seq, execution_epoch, task_id, payload)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb)
            """, [(str(uuid.uuid4()), owner.session_id, first_seq + index,
                    owner.execution_epoch, owner.task_id, payload)
                   for index, payload in enumerate(event_json)])
            # Inserts can wait on DB constraints/triggers. Do not commit a
            # snapshot if our lease expired while producing its outbox rows.
            valid = await db.fetchval(
                'SELECT lease_until > clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',
                owner.session_id,
            )
            if not valid:
                raise LostOwnership('Lease expired before the boundary could commit')
            if completed_command_id is not None:
                completed = await db.fetchval("""
                    UPDATE runtime_commands SET status='completed'
                    WHERE id=$1 AND session_id=$2 AND task_id=$3 AND status='claimed'
                        AND claimed_by=$4 AND claimed_epoch=$5
                        AND (kind='StartTask' OR (kind='ResumeInteraction' AND reply_id=$6
                            AND (payload->>'state_version')::bigint=$7))
                    RETURNING id
                """, completed_command_id,owner.session_id,owner.task_id,owner.worker_id,
                    owner.execution_epoch,reply_id,expected_state_version)
                if completed is None:
                    raise LostOwnership('Boundary cannot complete an unrelated or superseded command')
                # Command update may wait on a trigger too.
                if not await db.fetchval('SELECT lease_until > clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id):
                    raise LostOwnership('Lease expired before command completion')
            stage_runtime_wakeup(db)
            stage_runtime_wakeup(db, 'delivery')
            return row['state_version'], row['runtime_seq']
