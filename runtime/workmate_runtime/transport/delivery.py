"""Fenced immutable delivery metadata; no filesystem I/O inside transactions."""
import json
from workmate_contracts import BoundaryEvidence, snapshot_digest
from workmate_product.application.delivery_metadata import DeliveryConflict, _digest, _validate
from workmate_runtime.transport.stopping import StopStore
from workmate_runtime.transport.running import _matches_terminal_status


class DeliveryStore:
    def __init__(self,db):
        self.db=db

    async def _owner(self,owner,node,*,require_lease=True):
        command=await self.db.fetchval("SELECT id FROM runtime_commands WHERE task_id=$1 AND kind='StartTask'",owner.task_id)
        rows=await StopStore(self.db)._locked(command) if command else None
        if not rows:
            raise DeliveryConflict('Missing delivery owner binding')
        conv,task,session,execution,start=rows
        if (conv['active_product_task_id']!=owner.task_id or task['id']!=owner.task_id
                or task['conversation_id']!=conv['id'] or session['conversation_id']!=conv['id']
                or start['conversation_id']!=conv['id'] or start['session_id']!=owner.session_id
                or start['actor_user_id']!=conv['user_id'] or start['protocol_version']!=1
                or session['id']!=owner.session_id or session['active_task_id']!=owner.task_id
                or session['owner_id']!=owner.worker_id or session['execution_epoch']!=owner.execution_epoch
                or session['storage_node_id']!=node or execution['session_id']!=owner.session_id
                or any(execution[k]!=session[k] for k in ('execution_epoch','status','reply_id','state_version'))
                or (require_lease and not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id))):
            raise DeliveryConflict('Delivery owner, lease or admission gates changed')
        return session,execution,start

    async def _insert(self,table,owner,node,payload,*,snapshot_id=None,require_lease=True):
        digest=_digest(payload)
        existing=await self.db.fetchrow(f'SELECT * FROM {table} WHERE task_id=$1',owner.task_id)
        if existing:
            if (existing['session_id']!=owner.session_id or existing['execution_epoch']!=owner.execution_epoch
                    or existing['worker_id']!=owner.worker_id or existing['storage_node_id']!=node
                    or existing['digest']!=digest or _digest(json.loads(existing['payload']))!=digest
                    or (snapshot_id is not None and existing['snapshot_id']!=snapshot_id)):
                raise DeliveryConflict('Immutable delivery record differs from retry')
            return digest
        columns=',snapshot_id' if snapshot_id else ''
        value=',$8' if snapshot_id else ''
        args=[owner.task_id,owner.session_id,owner.execution_epoch,owner.worker_id,node,json.dumps(payload),digest]
        if snapshot_id:
            args.append(snapshot_id)
        await self.db.execute(f'''INSERT INTO {table}(task_id,session_id,execution_epoch,worker_id,
            storage_node_id,payload,digest{columns}) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7{value})''',*args)
        if require_lease and not await self.db.fetchval('SELECT lease_until>clock_timestamp() FROM runtime_sessions WHERE id=$1 AND superseded_at IS NULL',owner.session_id):
            raise DeliveryConflict('Lease expired before delivery metadata commit')
        if table == 'runtime_delivery_captures':
            from .postgres import stage_runtime_wakeup
            stage_runtime_wakeup(self.db, 'delivery')
        return digest

    async def record_baseline(self,owner,node,payload):
        _validate(payload,capture=False)
        async with self.db.transaction():
            session,execution,start=await self._owner(owner,node)
            if (session['status']!='preparing' or session['reply_id'] is not None or execution['snapshot_id'] is not None
                    or start['status']!='claimed' or start['claimed_by']!=owner.worker_id
                    or start['claimed_epoch']!=owner.execution_epoch):
                raise DeliveryConflict('Baseline must precede first Reply advancement')
            return await self._insert('runtime_delivery_baselines',owner,node,payload)

    async def load_baseline(self,owner,node):
        row=await self.db.fetchrow('SELECT * FROM runtime_delivery_baselines WHERE task_id=$1',owner.task_id)
        if not row or row['session_id']!=owner.session_id or row['storage_node_id']!=node:
            raise DeliveryConflict('No matching execution baseline')
        if (row['execution_epoch'],row['worker_id']) != (owner.execution_epoch,owner.worker_id):
            from workmate_product.persistence.recovery_provenance import LINEAGE_SELECT, valid_lineage
            history = await self.db.fetch(LINEAGE_SELECT + ' WHERE p.task_id=$1 AND p.session_id=$2',
                                         owner.task_id,owner.session_id)
            if not valid_lineage(history,task_id=owner.task_id,session_id=owner.session_id,node_id=node,
                    source_worker=row['worker_id'],source_epoch=row['execution_epoch'],
                    worker_id=owner.worker_id,execution_epoch=owner.execution_epoch):
                raise DeliveryConflict('Delivery baseline has no verified recovery ancestry')
        payload=json.loads(row['payload'])
        _validate(payload,capture=False)
        if _digest(payload)!=row['digest']:
            raise DeliveryConflict('Corrupt delivery baseline')
        return payload

    async def load_capture(self,owner,node):
        row=await self.db.fetchrow('SELECT * FROM runtime_delivery_captures WHERE task_id=$1',owner.task_id)
        if row is None:
            return None
        if (row['session_id']!=owner.session_id or row['execution_epoch']!=owner.execution_epoch
                or row['worker_id']!=owner.worker_id or row['storage_node_id']!=node):
            raise DeliveryConflict('Captured receipt belongs to a different owner')
        payload=json.loads(row['payload'])
        _validate(payload,capture=True)
        if _digest(payload)!=row['digest']:
            raise DeliveryConflict('Captured receipt digest changed')
        return row['digest']

    async def record_capture(self,owner,node,payload,*,seal_digest=None,recovery=False):
        if seal_digest is None:
            _,seal_digest=await self.load_seal(owner,node)
        return await self._record_capture(owner,node,payload,seal_digest=seal_digest,recovery=recovery)

    async def validate_recovery(self,owner,node,payload,seal_digest):
        await self._record_capture(owner,node,payload,seal_digest=seal_digest,recovery=True,verify_only=True)

    async def _record_capture(self,owner,node,payload,*,seal_digest=None,recovery=False,verify_only=False):
        _validate(payload,capture=True)
        if recovery and seal_digest is None:
            raise DeliveryConflict('Recovery requires a pre-terminal immutable seal')
        async with self.db.transaction():
            session,execution,start=await self._owner(owner,node,require_lease=not recovery)
            await self.load_baseline(owner,node)
            if (session['status'] not in {'completed','failed','interrupted','cancelled'}
                    or start['status']!='completed' or execution['snapshot_id']!=session['snapshot_id']):
                raise DeliveryConflict('Capture requires a durable terminal execution')
            snapshot=await self.db.fetchrow('SELECT * FROM runtime_snapshots WHERE id=$1',session['snapshot_id'])
            if (not snapshot or snapshot['task_id']!=owner.task_id or snapshot['session_id']!=owner.session_id
                    or any(snapshot[k]!=session[k] for k in ('execution_epoch','status','reply_id','state_version','runtime_seq'))):
                raise DeliveryConflict('Terminal snapshot identity changed')
            event=await self.db.fetchrow('''SELECT * FROM runtime_events WHERE session_id=$1 AND runtime_seq=$2''',
                owner.session_id,snapshot['runtime_seq'])
            boundary=json.loads(event['payload']) if event else {}
            proof=boundary.get('resource_cleanup',boundary.get('stop_cleanup',{}))
            if (not event or event['task_id']!=owner.task_id or event['execution_epoch']!=owner.execution_epoch
                    or boundary.get('type')!='EXECUTION_BOUNDARY' or boundary.get('reply_id')!=session['reply_id']
                    or boundary.get('status')!=session['status'] or boundary.get('pending_tool_call_ids')!=[]
                    or proof.get('core_drained') is not True or proof.get('resources_quiescent') is not True):
                raise DeliveryConflict('Terminal resource quiescence has not been durably proven')
            try:
                evidence=BoundaryEvidence.model_validate(boundary.get('boundary_evidence'))
                end_raw=await self.db.fetchval('''SELECT payload FROM runtime_events
                    WHERE session_id=$1 AND task_id=$2 AND execution_epoch=$3 AND runtime_seq=$4''',
                    owner.session_id,owner.task_id,owner.execution_epoch,snapshot['runtime_seq']-1)
                end=json.loads(end_raw) if end_raw else {}
                valid=(not evidence.pending_tools
                    and evidence.snapshot_sha256==snapshot_digest(json.loads(snapshot['payload']))
                    and end.get('type')=='REPLY_END' and end.get('reply_id')==snapshot['reply_id']
                    and _matches_terminal_status(end, snapshot['status'], boundary))
            except (ValueError,TypeError):
                valid=False
            if not valid:
                raise DeliveryConflict('Capture does not match the exact drained Core snapshot')
            if seal_digest is not None:
                seal,digest=await self.load_seal(owner,node)
                if (digest!=seal_digest or boundary.get('delivery_seal_sha256')!=digest
                        or seal['snapshot_sha256']!=evidence.snapshot_sha256
                        or seal['reply_id']!=snapshot['reply_id'] or seal['status']!=snapshot['status']
                        or seal['capture']!=payload):
                    raise DeliveryConflict('Terminal boundary does not bind this immutable delivery seal')
                unresolved=await self.db.fetchval("""SELECT EXISTS(SELECT 1 FROM runtime_commands
                    WHERE session_id=$1 AND status IN ('claimed','indeterminate'))""",owner.session_id)
                if unresolved:
                    raise DeliveryConflict('Unresolved commands prevent delivery recovery')
            if verify_only:
                return
            return await self._insert('runtime_delivery_captures',owner,node,payload,
                snapshot_id=snapshot['id'],require_lease=not recovery)

    async def record_seal(self,owner,node,*,reply_id,status,snapshot,payload):
        _validate(payload,capture=True)
        if status not in {'completed','failed','interrupted','cancelled'}:
            raise DeliveryConflict('Only terminal Core state can seal output')
        async with self.db.transaction():
            session,execution,start=await self._owner(owner,node)
            if session['status'] not in {'running','stopping'} or session['reply_id']!=reply_id:
                raise DeliveryConflict('Delivery seal requires the currently draining Reply')
            baseline=await self.load_baseline(owner,node)
            seal={'version':1,'baseline_sha256':_digest(baseline),
                'snapshot_sha256':snapshot_digest(snapshot),'reply_id':reply_id,'status':status,'capture':payload}
            return await self._insert('runtime_delivery_seals',owner,node,seal)

    async def load_seal(self,owner,node):
        row=await self.db.fetchrow('SELECT * FROM runtime_delivery_seals WHERE task_id=$1',owner.task_id)
        if (not row or row['session_id']!=owner.session_id or row['execution_epoch']!=owner.execution_epoch
                or row['worker_id']!=owner.worker_id or row['storage_node_id']!=node):
            raise DeliveryConflict('No matching pre-terminal immutable delivery seal')
        seal=json.loads(row['payload'])
        if (not isinstance(seal,dict) or set(seal)!={'version','baseline_sha256','snapshot_sha256','reply_id','status','capture'}
                or seal['version']!=1 or _digest(seal)!=row['digest']):
            raise DeliveryConflict('Corrupt terminal delivery seal')
        _validate(seal['capture'],capture=True)
        baseline=await self.load_baseline(owner,node)
        if seal['baseline_sha256']!=_digest(baseline):
            raise DeliveryConflict('Terminal seal refers to a different baseline')
        return seal,row['digest']
