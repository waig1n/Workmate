"""Permission-only pause orchestration; files and product gates survive release."""
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.transport.postgres import ExecutionStore
from workmate_runtime.transport.suspension import CleanupReceipt, SuspensionStore


class SuspensionController:
    def __init__(self, pool, assembler, delivery):
        self.pool, self.assembler, self.delivery = pool, assembler, delivery

    async def try_suspend(self, owner, node_id):
        spaces = self.assembler.workspaces
        if spaces.provider.name not in {'bubblewrap', 'docker', 'kubernetes'}:
            return False
        bound = self.assembler._bound.get(owner)
        if bound is None:
            return False
        generation = spaces._ownership[bound.identity].generation
        async with self.pool.acquire() as db:
            boundary = await ExecutionStore(db).load_boundary(owner)
            claim = await SuspensionStore(db).begin(owner, node_id, boundary.snapshot_id, generation)
        if claim is None:
            # Projection may not yet expose awaiting_permission, or a control
            # intent won admission. Neither case may seal the local scope.
            return False
        try:
            checkpoint_options = {}
            checkpoints = getattr(self.delivery, "checkpoints", None)
            if checkpoints is not None:
                checkpoint_options["checkpoint"] = checkpoints.capture_required
            storage_proof = await spaces.suspend_task_workspace(owner,
                CoreSnapshot.model_validate(boundary.snapshot), agent_name=bound.agent_name, **checkpoint_options)
            proof = CleanupReceipt(schema_version=1, session_id=owner.session_id,
                task_id=owner.task_id, worker_id=owner.worker_id, execution_epoch=owner.execution_epoch,
                snapshot_id=claim.snapshot_id, snapshot_digest=claim.snapshot_digest,
                node_id=node_id, resource_generation=generation,
                resources_quiescent=True, workspace_closed=True,
                storage_proof=storage_proof if spaces.provider.name != 'bubblewrap' else None)
            try:
                async with self.pool.acquire() as db:
                    await SuspensionStore(db).release(claim, proof)
            except Exception:
                # Reconcile a lost commit response using the exact same proof;
                # never repeat resource cleanup or any model/tool operation.
                async with self.pool.acquire() as db:
                    await SuspensionStore(db).release(claim, proof)
        except BaseException:
            # A crash or cancellation without a committed complete receipt is
            # never permission for another process to reuse the workspace.
            async with self.pool.acquire() as db:
                await SuspensionStore(db).block(claim)
            raise
        self.assembler._bound.pop(owner, None)
        self.assembler._preparation_started.discard(owner)
        self.delivery._bindings.pop(owner, None)
        self.delivery._checkpoint_workspaces.pop(owner, None)
        return True
