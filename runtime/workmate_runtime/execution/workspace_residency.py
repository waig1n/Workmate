"""Persist trusted Workspace ownership boundaries, independent of queue delivery."""
import logging

from redis.exceptions import RedisError
from workmate_product.coordination.worker_registry import AffinityRecord
from workmate_product.persistence.repositories import workspace_residencies as store


logger=logging.getLogger(__name__)


class WorkspaceResidencyCoordinator:
    def __init__(self,sessions,node_id,*,registry=None,release_queue=None):
        self.sessions,self.node_id=sessions,node_id
        self.registry,self.release_queue=registry,release_queue

    def _identity(self,identity,owner=None):
        if (identity.storage_node_id!=self.node_id or (owner is not None and
                owner.session_id!=identity.session_id)):
            raise ValueError('Workspace residency requires the exact local Session')

    async def record_held(self,identity,owner,generation):
        self._identity(identity,owner)
        async def register(db):
            return await store.record_held(db,session_id=identity.session_id,
                task_id=owner.task_id,worker_id=owner.worker_id,
                execution_epoch=owner.execution_epoch,node_id=self.node_id,generation=generation)
        return await self._confirm(register)

    @staticmethod
    def affinity(hold):
        return AffinityRecord(session_id=hold['session_id'],node_id=hold['storage_node_id'],
            incarnation=hold['holder_worker_id'],workspace_generation=hold['workspace_generation'],
            hold_version=hold['hold_version'])

    async def withdraw(self,hold):
        if self.registry is not None:
            try:
                await self.registry.withdraw_affinity(self.affinity(hold))
            except (ConnectionError,TimeoutError,OSError,RedisError) as error:
                # Hints are rebuildable; a stale hint cannot override PG release.
                logger.warning('Workspace affinity withdrawal deferred exception_type=%s',type(error).__name__)

    @staticmethod
    def _receipt(claim,*,owner=None):
        return dict(schema_version=1,session_id=claim.session_id,worker_id=claim.worker_id,
            node_id=claim.node_id,workspace_generation=claim.generation,hold_version=claim.hold_version,
            resources_quiescent=True,workspace_closed=True,ownership_released=True,
            task_id=owner.task_id if owner else None,
            execution_epoch=owner.execution_epoch if owner else None)

    async def _confirm(self,operation):
        try:
            async with self.sessions.begin() as db:
                result=await operation(db)
        except Exception:
            # Only repeat the immutable PG receipt after an ambiguous commit.
            # Resource closure and tool calls are never repeated here.
            async with self.sessions.begin() as db:
                result=await operation(db)
        return result

    async def release_owned(self,identity,owner,hold):
        self._identity(identity,owner)
        if (hold['session_id']!=identity.session_id or hold['holder_worker_id']!=owner.worker_id
                or hold['storage_node_id']!=self.node_id):
            raise ValueError('Owned close does not match this resource holding')
        claim=store.ReleaseClaim(store._request_id(identity.session_id,hold['hold_version']),
            identity.session_id,owner.worker_id,self.node_id,hold['workspace_generation'],hold['hold_version'])
        async def finish(db):
            await store.finish_owned_release(db,session_id=identity.session_id,task_id=owner.task_id,
                worker_id=owner.worker_id,execution_epoch=owner.execution_epoch,node_id=self.node_id,
                generation=claim.generation,hold_version=claim.hold_version,
                receipt=self._receipt(claim,owner=owner))
        await self._confirm(finish)
        await self.withdraw(hold)

    async def request_idle_release(self,hold,*,notify=True):
        async with self.sessions.begin() as db:
            request=await store.request_release(db,session_id=hold['session_id'],
                expected_worker=hold['holder_worker_id'],expected_hold_version=hold['hold_version'],
                generation=hold['workspace_generation'],node_id=self.node_id)
        if notify and request and request['status']=='pending' and self.release_queue is not None:
            from workmate_product.coordination.command_queue import QueueRoute
            from workmate_product.coordination.workspace_release_queue import WorkspaceReleaseEnvelope
            try:
                await self.release_queue.publish(QueueRoute(self.node_id,hold['holder_worker_id'],True),
                    WorkspaceReleaseEnvelope(request_id=request['id'],session_id=hold['session_id'],
                        storage_node_id=self.node_id,target_incarnation=hold['holder_worker_id'],
                        workspace_generation=hold['workspace_generation'],expected_hold_version=hold['hold_version']))
            except (ConnectionError,TimeoutError,OSError,RedisError) as error:
                logger.warning('Workspace release notification deferred exception_type=%s',type(error).__name__)
        return request

    async def begin_idle_release(self,identity,hold):
        self._identity(identity)
        if hold['session_id']!=identity.session_id or hold['storage_node_id']!=self.node_id:
            raise ValueError('Idle close does not match this resource holding')
        request=await self.request_idle_release(hold,notify=False)
        if request is None:
            return None
        claim=None
        try:
            async with self.sessions.begin() as db:
                claim=await store.begin_release(db,request['id'],hold['holder_worker_id'],self.node_id)
        except Exception:
            # Only the in-memory winner can confirm its ambiguous CAS commit.
            # Re-entering begin_release cannot authorize another cleanup winner.
            if claim is None:
                raise
            async with self.sessions.begin() as db:
                if not await store.confirm_release_claim(db,claim):
                    raise store.ResidencyConflict('Workspace release CAS was not durably committed')
        return claim

    async def finish_idle_release(self,claim):
        if claim.node_id!=self.node_id:
            raise ValueError('Foreign storage release receipt')
        async def finish(db):
            await store.finish_release(db,claim,self._receipt(claim))
        await self._confirm(finish)
        await self.withdraw(dict(session_id=claim.session_id,storage_node_id=claim.node_id,
            holder_worker_id=claim.worker_id,workspace_generation=claim.generation,hold_version=claim.hold_version))

    async def block_idle_release(self,claim,error):
        async with self.sessions.begin() as db:
            await store.block_release(db,claim,error if isinstance(error,str) else type(error).__name__)
