"""Worker filesystem capture coordinated with fenced durable delivery records."""
import asyncio

from workmate_runtime.transport.delivery import DeliveryStore,DeliveryConflict
from workmate_runtime.transport.postgres import _lease_seconds
from .files import DeliveryFiles
from .remote_files import RemoteDeliveryFiles


async def _joined_io(function,*args,**kwargs):
    # Native file I/O runs away from the event loop. Cancellation joins the
    # thread so shutdown never abandons an in-flight filesystem publisher.
    operation=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if operation.done() and not operation.cancelled():
            operation.exception()
        raise


class DeliveryCoordinator:
    def __init__(self,pool,storage_node_id,storage_root,registry,*,max_file_bytes,max_files,
                 mode='hybrid',strict=False,lease_seconds=30,object_store=None,checkpoints=None):
        if mode not in {'hybrid','snapshot_fallback'} or type(strict) is not bool:
            raise ValueError('Unknown delivery policy')
        self.pool,self.node,self.root,self.registry=pool,storage_node_id,storage_root,registry
        self.max_bytes,self.max_files,self.mode,self.strict=max_file_bytes,max_files,mode,strict
        self.lease_seconds=_lease_seconds(lease_seconds)
        self._bindings={}
        self.object_store=object_store
        self.checkpoints=checkpoints
        self._checkpoint_workspaces={}

    async def prepare(self,owner,binding):
        if binding.owner!=owner or owner in self._bindings:
            raise DeliveryConflict('Delivery baseline requires a fresh matching Workspace binding')
        self.registry._scope(owner,launching=True)
        if binding.workspace.host_workdir is None:
            files=RemoteDeliveryFiles(binding.workspace.get_backend(),binding.workspace.workdir,self.root,
                max_file_bytes=self.max_bytes,max_files=self.max_files,object_store=self.object_store)
            baseline=await files.baseline()
        else:
            files=DeliveryFiles(binding.workspace.host_workdir,self.root,
                max_file_bytes=self.max_bytes,max_files=self.max_files,object_store=self.object_store)
            baseline=await _joined_io(files.baseline)
        async with self.pool.acquire() as db:
            await DeliveryStore(db).record_baseline(owner,self.node,baseline)
        self._bindings[owner]=files
        if self.checkpoints is not None:
            self._checkpoint_workspaces[owner]=binding.workspace

    async def rebind(self, owner, binding):
        """Rebuild only process-local file access; preserve the initial baseline."""
        if binding.owner != owner or owner in self._bindings:
            raise DeliveryConflict('Cold delivery requires a new matching binding')
        self.registry._scope(owner, launching=True)
        async with self.pool.acquire() as db:
            await DeliveryStore(db)._owner(owner, self.node)
            await DeliveryStore(db).load_baseline(owner, self.node)
        if binding.workspace.host_workdir is None:
            files = RemoteDeliveryFiles(binding.workspace.get_backend(), binding.workspace.workdir, self.root,
                max_file_bytes=self.max_bytes, max_files=self.max_files, object_store=self.object_store)
        else:
            files = DeliveryFiles(binding.workspace.host_workdir, self.root,
                max_file_bytes=self.max_bytes, max_files=self.max_files, object_store=self.object_store)
        self._bindings[owner] = files
        if self.checkpoints is not None:
            self._checkpoint_workspaces[owner]=binding.workspace

    async def seal_terminal(self,owner,reply_id,status,snapshot):
        """Publish final bytes after resource quiescence, before terminal commit."""
        scope=self.registry._scope(owner)
        if owner not in self._bindings or not scope.sealed or any(not r.finished for r in scope.resources.values()):
            raise DeliveryConflict('Task resources have not been sealed and proven quiescent')
        if self.checkpoints is not None:
            await self.checkpoints.capture(owner,self._checkpoint_workspaces.get(owner),boundary="terminal")
        async with self.pool.acquire() as db:
            baseline=await DeliveryStore(db).load_baseline(owner,self.node)
        # The execution controller keeps its existing heartbeat active while
        # this joined filesystem operation publishes the final immutable bytes.
        files=self._bindings[owner]
        # Shared publication exclusion spans bytes -> durable seal. Purge takes
        # the exclusive counterpart before testing references and unlinking.
        # No Product row locks are held during file I/O; heartbeat stays live.
        from workmate_product.persistence.repositories.conversation_purge import ARTIFACT_STORAGE_LOCK
        async with self.pool.acquire() as db:
            await db.execute('SELECT pg_advisory_xact_lock_shared($1)', ARTIFACT_STORAGE_LOCK)
            if isinstance(files,RemoteDeliveryFiles):
                payload=await files.capture(baseline,mode=self.mode,strict=self.strict)
            else:
                payload=await _joined_io(files.capture,baseline,mode=self.mode,strict=self.strict)
            return await DeliveryStore(db).record_seal(owner,self.node,reply_id=reply_id,
                status=status,snapshot=snapshot,payload=payload)

    async def capture(self,result):
        if result.reply_id is None:
            return None
        if result.status not in {'completed','failed','cancelled','interrupted'}:
            raise DeliveryConflict('Parked execution has no final delivery window')
        from .recovery import TerminalDeliveryRecovery
        return await TerminalDeliveryRecovery(self.pool,self.node,self.root,object_store=self.object_store).recover(result.owner)
