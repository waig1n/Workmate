"""Concrete Worker composition, with no App server or Product HTTP imports."""
from dataclasses import dataclass
from functools import partial
import asyncio
from workmate_runtime.execution.retirement import RetirementWorker
from workmate_runtime.execution.product_authority import authorize_resume, report_worker_failure

from sqlalchemy.ext.asyncio import async_sessionmaker
from workmate_runtime.assembly.agent import WorkerAgentAssembler
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager
from workmate_runtime.sandbox.factory import create_sandbox_provider
from workmate_runtime.delivery.coordinator import DeliveryCoordinator
from workmate_runtime.delivery.recovery import TerminalDeliveryRecovery
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.execution.service import WorkerService,WorkerBindings
from workmate_runtime.execution.external_results import ExternalExecutionController,ExternalResultResources
from workmate_runtime.transport.product_pool import ProductWorkerPool


@dataclass
class WorkerApplication:
    service: WorkerService
    assembler: WorkerAgentAssembler
    delivery: DeliveryCoordinator
    workspaces: WorkerWorkspaceManager
    external_resources: ExternalResultResources
    delivery_recovery: TerminalDeliveryRecovery
    retirement: RetirementWorker
    knowledge_management: object | None = None
    workspace_lifecycle: object | None = None

    async def _sweep(self, stop):
        policy = self.assembler.settings.workspace_lifecycle
        while not stop.is_set():
            await self.workspaces.sweep_idle(ttl=policy.warm_ttl_seconds,
                capacity=policy.max_warm_per_worker)
            try:
                await asyncio.wait_for(stop.wait(), policy.sweep_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def serve(self,shutdown):
        retirement_stop = asyncio.Event()
        retirement_task = asyncio.create_task(self.retirement.serve(retirement_stop))
        sweep_task = asyncio.create_task(self._sweep(retirement_stop))
        lifecycle_task = (asyncio.create_task(self.workspace_lifecycle.serve(retirement_stop,shutdown))
            if self.workspace_lifecycle is not None else None)
        serving=None
        try:
            self.service.failures.update(await self.delivery_recovery.recover_pending())
            serving=asyncio.create_task(self.service.serve(shutdown))
            supervised=[serving,retirement_task,sweep_task]
            if lifecycle_task is not None:
                supervised.append(lifecycle_task)
            done,_=await asyncio.wait(supervised,return_when=asyncio.FIRST_COMPLETED)
            for background in supervised[1:]:
                if background in done:
                    await background
                    raise RuntimeError('Worker resource lifecycle exited before shutdown')
            await serving
        finally:
            if serving is not None and not serving.done():
                serving.cancel()
                await asyncio.gather(serving,return_exceptions=True)
            retirement_stop.set()
            try:
                await asyncio.gather(retirement_task, sweep_task,
                    *([lifecycle_task] if lifecycle_task is not None else []))
            finally:
                # Service has already cancelled/joined its controllers. Quiesce
                # remaining known resources without fabricating durable terminal
                # evidence for interrupted or lost-owner executions.
                registry=self.workspaces.resource_registry
                for owner in tuple(registry._scopes):
                    if not await registry.verify_preparation_cleanup(owner):
                        self.service.failures[owner.session_id]='ResourceCleanupUnproven'
                await self.workspaces.close()


def compose_worker(engine,settings,*,storage_node_id,workspace_dir,artifact_dir,
                   max_file_bytes,max_files,strict_delivery=False,
                   capacity=4,lease_seconds=30,drain_seconds=30,knowledge_resolver=None,
                   workspace_factory=None,memory_manager=None,ownership_provider=None,sandbox_provider=None,wakeup_bus=None,command_queue=None,command_source=None,
                   realtime_log=None):
    pool=ProductWorkerPool(engine,wakeup_bus=wakeup_bus)
    from workmate_product.skill_storage import store_from_settings
    sessions=async_sessionmaker(engine,expire_on_commit=False,
        info={'skill_blob_store': store_from_settings(settings), 'wakeup_bus': wakeup_bus})
    registry=TaskResourceRegistry()
    if sandbox_provider is not None and workspace_factory is not None:
        raise ValueError('Specify a sandbox provider or a workspace factory, not both')
    provider = sandbox_provider or create_sandbox_provider(settings.sandbox,
        workspace_factory=workspace_factory)
    if ownership_provider is None:
        from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
        ownership_provider=PosixWorkspaceOwnership()
    workspaces=WorkerWorkspaceManager(workspace_dir,storage_node_id=storage_node_id,
        resource_registry=registry,ownership_provider=ownership_provider,provider=provider)
    workspace_hints=release_queue=None
    if command_queue is not None and provider.name=='bubblewrap':
        from workmate_product.coordination.worker_registry import RedisWorkerRegistry
        from workmate_product.coordination.workspace_release_queue import RedisWorkspaceReleaseQueue
        from workmate_runtime.execution.workspace_residency import WorkspaceResidencyCoordinator
        workspace_hints=RedisWorkerRegistry(command_queue.client,namespace=command_queue.namespace)
        release_queue=RedisWorkspaceReleaseQueue(command_queue.client,namespace=command_queue.namespace)
        workspaces.residency=WorkspaceResidencyCoordinator(sessions,storage_node_id,
            registry=workspace_hints,release_queue=release_queue)
    from workmate_product.artifact_storage import artifact_store_from_settings
    object_store=artifact_store_from_settings(settings)
    checkpoints = None
    if settings.artifact_storage.workspace_checkpoint_enabled:
        from workmate_runtime.delivery.checkpoints import CheckpointCoordinator
        checkpoint_policy = settings.artifact_storage
        checkpoints = CheckpointCoordinator(pool, storage_node_id, object_store,
            max_files=checkpoint_policy.workspace_checkpoint_max_files,
            max_file_bytes=checkpoint_policy.workspace_checkpoint_max_file_bytes,
            max_bytes=checkpoint_policy.workspace_checkpoint_max_bytes)
    delivery=DeliveryCoordinator(pool,storage_node_id,artifact_dir,registry,
        max_file_bytes=max_file_bytes,max_files=max_files,
        mode=settings.artifact_delivery_mode,strict=strict_delivery,
        lease_seconds=lease_seconds,object_store=object_store,checkpoints=checkpoints)
    assembler=WorkerAgentAssembler(sessions,settings,workspaces,artifact_storage_dir=artifact_dir,artifact_object_store=object_store,
        delivery_coordinator=delivery,knowledge_resolver=knowledge_resolver,memory_manager=memory_manager)

    authorize_product_resume = partial(authorize_resume, storage_node_id=storage_node_id,
        knowledge_enabled=settings.knowledge.enabled)

    cold_workspaces = (settings.workspace_lifecycle.max_warm_per_worker == 0
        and provider.name == 'bubblewrap')

    async def verify_preparation_cleanup(owner):
        if await assembler.verify_preparation_cleanup(owner) is not True:
            return False
        if cold_workspaces or workspaces.residency is not None:
            await workspaces.close_prepared_workspace(owner)
        return True

    async def seal_terminal(owner, reply_id, status, snapshot):
        seal = await delivery.seal_terminal(owner, reply_id, status, snapshot)
        # A peer may publish final delivery immediately after terminal commit.
        # Release the local Workspace before that commit, not in finalize_result.
        # Remote/transient providers retain their existing lifecycle semantics.
        if cold_workspaces:
            await workspaces.close_task_workspace(owner)
        return seal

    external=ExternalResultResources(registry)
    realtime_writer_factory = None
    if realtime_log is not None:
        from workmate_product.realtime.log_manager import EventLogManager
        from workmate_runtime.execution.realtime_store import RealtimeWriterFactory
        realtime_writer_factory = RealtimeWriterFactory(sessions,
            log_manager=EventLogManager(sessions, realtime_log), log=realtime_log,
            policy=settings.realtime)
    tool_content_factory = None
    if settings.artifact_storage.tool_content_enabled:
        from workmate_runtime.execution.tool_content import ToolContentCapture
        from workmate_product.artifact_storage import tool_content_store_from_settings
        tool_content_factory = partial(ToolContentCapture, tool_content_store_from_settings(settings),
            inline_bytes=settings.artifact_storage.tool_content_inline_bytes,
            preview_chars=settings.artifact_storage.tool_content_preview_chars)
    controller=ExternalExecutionController(pool,verify_external_result=external.verify_result,
        verify_preparation_cleanup=verify_preparation_cleanup,
        verify_cleanup=registry.verify_cleanup,lease_seconds=lease_seconds,
        seal_terminal=seal_terminal,wakeup_bus=wakeup_bus,
        realtime_writer_factory=realtime_writer_factory,tool_content_factory=tool_content_factory)
    async def finalize_result(result):
        await delivery.capture(result)
        await workspaces.release_task(result.owner)
        assembler._bound.pop(result.owner, None)
        assembler._preparation_started.discard(result.owner)
        delivery._bindings.pop(result.owner, None)
        delivery._checkpoint_workspaces.pop(result.owner, None)
        for key in tuple(external.jobs):
            if key[0] == result.owner:
                external.jobs.pop(key)

    from workmate_runtime.execution.suspension import SuspensionController
    suspension = SuspensionController(pool,assembler,delivery)
    report_failure = partial(report_worker_failure, sessions)
    bindings=WorkerBindings(assembler.prepare_start,assembler.prepare_resume,assembler.prepare_cleanup,
        authorize_product_resume,registry.verify_cleanup,finalize_result,report_failure,suspension.try_suspend)
    if command_queue is not None:
        if command_source is not None:
            raise ValueError('Specify one command source')
        from workmate_runtime.execution.command_queue import CommandQueueSource
        command_source=CommandQueueSource(command_queue,sessions,wakeup_bus=wakeup_bus,
            pending_idle_ms=settings.command_queue.pending_idle_seconds*1000,
            max_generations=settings.command_queue.max_attempts)
    service=WorkerService(pool,storage_node_id,bindings,controller=controller,capacity=capacity,
        lease_seconds=lease_seconds,drain_seconds=drain_seconds,wakeup_bus=wakeup_bus,command_source=command_source)
    app=WorkerApplication(service,assembler,delivery,workspaces,external,
        TerminalDeliveryRecovery(pool,storage_node_id,artifact_dir,object_store=object_store),
        RetirementWorker(sessions,service,workspaces,artifact_dir=artifact_dir,object_store=object_store))
    if workspace_hints is not None:
        from workmate_runtime.execution.workspace_lifecycle import WorkerWorkspaceLifecycle
        app.workspace_lifecycle=WorkerWorkspaceLifecycle(sessions,service,workspaces,
            workspace_hints,release_queue,capacity=capacity)
    return app
