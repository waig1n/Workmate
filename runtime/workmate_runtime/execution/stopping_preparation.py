"""Stop preparation by joining its producer before trusted resource verification."""
import asyncio
from contextlib import nullcontext
from workmate_product.coordination import FALLBACK_SECONDS, wait_for_wakeup

from workmate_runtime.execution.controller import ExecutionAborted, ExecutionResult, SandboxVerificationFailed
from workmate_runtime.execution.running import RunningExecutionController
from workmate_runtime.transport.stopping_preparation import PreparationStopStore
from workmate_runtime.transport.postgres import LostOwnership
from workmate_runtime.assembly.models import ModelUnavailable


class PreparingExecutionController(RunningExecutionController):
    def __init__(self, pool, *, verify_preparation_cleanup, **kwargs):
        super().__init__(pool,**kwargs)
        if not callable(verify_preparation_cleanup):
            raise TypeError('Trusted joined-preparation verification is required')
        self.verify_preparation_cleanup=verify_preparation_cleanup

    async def stop_unclaimed(self, command_id, worker_id, storage_node_id):
        async with self.pool.acquire() as db:
            async with db.transaction():
                store=PreparationStopStore(db)
                claim=await store.claim(command_id,worker_id,storage_node_id,
                    lease_seconds=self.lease_seconds,unclaimed_only=True)
                if claim is None:
                    return None
                version,seq=await store.finish(claim,storage_node_id,
                    preparation_joined=True,resources_quiescent=True)
        return ExecutionResult(claim.owner,None,'cancelled',version,seq,())

    async def _claim_preparation(self, guard):
        async with guard.write_lock:
            if (guard.failure is not None and not isinstance(guard.failure,LostOwnership)) or guard.reply_id is not None:
                return None
            async with self.pool.acquire() as db:
                commands=await db.fetch("""SELECT id FROM runtime_commands WHERE task_id=$1
                    AND kind='StopTask' AND status='accepted' ORDER BY created_at,id""",guard.owner.task_id)
                for command in commands:
                    claim=await PreparationStopStore(db).claim(command['id'],guard.owner.worker_id,
                        guard.node_id,lease_seconds=self.lease_seconds)
                    if claim:
                        # begin_reply can lose to Product Stop before the watcher
                        # polls. Locked no-Reply ownership proves no Core start
                        # committed; only that exact rejection may be cleared.
                        if isinstance(guard.failure,LostOwnership):
                            guard.failure=None
                        return claim
        return None

    async def _watch_preparation(self, guard, execution):
        subscription = self.wakeup_bus.subscribe('commands') if self.wakeup_bus else nullcontext(None)
        with subscription as pulse:
            while guard.reply_id is None:
                version = pulse.version if pulse else 0
                finished=execution.done()
                claim=await self._claim_preparation(guard)
                if claim:
                    return claim
                if finished:
                    return None
                wake = asyncio.create_task(wait_for_wakeup(pulse, version,
                    FALLBACK_SECONDS if pulse else self.poll_interval))
                try:
                    await asyncio.wait((execution, wake), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
            return None

    async def _finish_preparation(self, guard, claim, execution):
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution,return_exceptions=True)
        if guard.active_tools or guard.reply_id is not None or guard.failure is not None:
            raise ExecutionAborted('Preparation cancellation crossed the Reply boundary')
        if await self.verify_preparation_cleanup(guard.owner) is not True:
            raise ExecutionAborted('Preparation resources are not proven quiescent')
        async with self.pool.acquire() as db:
            version,seq=await PreparationStopStore(db).finish(claim,guard.node_id,
                preparation_joined=True,resources_quiescent=True)
        return ExecutionResult(claim.owner,None,'cancelled',version,seq,())

    async def _execute(self, guard, prepare):
        execution=asyncio.create_task(super()._execute(guard,prepare))
        watcher=asyncio.create_task(self._watch_preparation(guard,execution))
        try:
            claim=await watcher
            if claim:
                return await self._finish_preparation(guard,claim,execution)
            try:
                return await execution
            except (SandboxVerificationFailed, ModelUnavailable) as error:
                if guard.active_tools or guard.reply_id is not None or guard.failure is not None:
                    raise ExecutionAborted('Preparation rejection crossed the Reply boundary')
                if await self.verify_preparation_cleanup(guard.owner) is not True:
                    raise ExecutionAborted('Rejected preparation resources are not proven quiescent')
                from workmate_runtime.transport.preparation_rejection import PreparationRejectionStore
                try:
                    async with self.pool.acquire() as db:
                        version, seq = await PreparationRejectionStore(db).finish(guard.owner,
                            guard.command_id,guard.node_id,preparation_joined=True,resources_quiescent=True,
                            reason='model_unavailable' if isinstance(error, ModelUnavailable)
                                else 'sandbox_verification_failed')
                except LostOwnership:
                    # Product Stop may commit after the watcher finished. Only
                    # its exact locked claim can settle cancellation instead.
                    claim = await self._claim_preparation(guard)
                    if claim:
                        return await self._finish_preparation(guard,claim,execution)
                    raise
                return ExecutionResult(guard.owner,None,'failed',version,seq,())
        finally:
            watcher.cancel()
            if not execution.done():
                execution.cancel()
            await asyncio.gather(watcher,execution,return_exceptions=True)
