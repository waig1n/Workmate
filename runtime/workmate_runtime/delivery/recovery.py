"""Startup compensation from a bound immutable seal, never mutable output."""
from workmate_runtime.transport.delivery import DeliveryStore,DeliveryConflict
from workmate_product.application.delivery_metadata import _digest
from workmate_runtime.transport.postgres import ExecutionOwner
from .coordinator import _joined_io
from .files import verify_capture_objects


class TerminalDeliveryRecovery:
    def __init__(self,pool,storage_node_id,storage_root,*,object_store=None):
        if not isinstance(storage_node_id,str) or not storage_node_id.strip():
            raise ValueError('Delivery recovery requires a trusted storage node')
        self.pool,self.node,self.root=pool,storage_node_id,storage_root
        self.object_store=object_store

    async def recover(self,owner):
        async with self.pool.acquire() as db:
            store=DeliveryStore(db)
            seal,digest=await store.load_seal(owner,self.node)
            existing=await store.load_capture(owner,self.node)
            if existing is not None and existing!=_digest(seal['capture']):
                raise DeliveryConflict('Existing capture differs from its immutable seal')
            if existing is None:
                await store.validate_recovery(owner,self.node,seal['capture'],digest)
        # No DB locks during filesystem I/O. This verifies content-addressed
        # sealed bytes even if output was overwritten or the mount disappeared.
        await _joined_io(verify_capture_objects,self.root,seal['capture'],self.object_store)
        if existing is not None:
            return existing
        try:
            async with self.pool.acquire() as db:
                return await DeliveryStore(db).record_capture(owner,self.node,seal['capture'],
                    seal_digest=digest,recovery=True)
        except DeliveryConflict:
            # Another recoverer may have committed and Product may already have
            # released admission. Return only its exact immutable receipt;
            # never reacquire or rewrite that now-historical Task's gates.
            async with self.pool.acquire() as db:
                existing=await DeliveryStore(db).load_capture(owner,self.node)
            if existing==_digest(seal['capture']):
                return existing
            raise

    async def recover_pending(self):
        async with self.pool.acquire() as db:
            candidates=await db.fetch('''SELECT s.id AS session_id,s.active_task_id AS task_id,
                s.owner_id AS worker_id,s.execution_epoch FROM runtime_sessions s
                JOIN conversations c ON c.id=s.conversation_id
                WHERE s.superseded_at IS NULL AND s.storage_node_id=$1 AND s.status IN ('completed','failed','cancelled','interrupted')
                    AND s.active_task_id=c.active_product_task_id AND s.reply_id IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM runtime_delivery_captures d WHERE d.task_id=s.active_task_id)
                ORDER BY s.id''',self.node)
        failures={}
        for row in candidates:
            try:
                await self.recover(ExecutionOwner(row['session_id'],row['task_id'],
                    row['worker_id'],row['execution_epoch']))
            except Exception as error:
                # One missing/corrupt seal does not block unrelated Sessions.
                # No text from private state or paths escapes into diagnostics.
                failures[row['session_id']]=type(error).__name__
        return failures
