"""Process-incarnation-local resource evidence for a complete Task owner scope.

This is not a recovery ledger. Only a fresh claimed execution whose assembler
tracks every resource-producing path may open a scope. Restoring an old owner
into an empty registry cannot establish quiescence. Unknown external jobs and
unresolved launches remain blockers, even after their asyncio caller exits.
"""
import asyncio
from dataclasses import dataclass, field
import math
import os
import uuid

from agentscope.workspace._bubblewrap._bubblewrap_backend import BubblewrapBackend
from workmate_runtime.engines.agentscope import CoreSnapshot
from workmate_runtime.transport.postgres import ExecutionOwner


class ResourceScopeError(RuntimeError):
    """Unknown, incomplete or sealed resource scope cannot launch more work."""


class _ProcessExitPending(ResourceScopeError):
    """The OS reaped a launch before asyncio confirmed its child exit."""


@dataclass
class _Resource:
    kind: str
    tool_call_id: str | None = None
    cleanup: object = None
    probe: object = None
    finished: bool = False
    launch: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Scope:
    resources: dict[str, _Resource] = field(default_factory=dict)
    sealed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _seconds(value, name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError(f'{name} must be finite seconds greater than zero and at most 60')
    return value


class TaskResourceRegistry:
    """Trusted assembler/resource adapters only; never an API or model surface.

    Process groups alone cannot contain daemon escapes. Production process
    registration uses the Bubblewrap PID namespace. trusted_process_group is
    also used by explicitly unsafe local execution: it proves only that known
    process groups ended, never daemon containment or sandbox isolation. Workspace-wide gateway
    processes are not Task resources and must use an unbound backend.
    """

    def __init__(self, *, grace=1.0, cleanup_timeout=5.0):
        self.grace = _seconds(grace,'grace')
        self.cleanup_timeout = _seconds(cleanup_timeout,'cleanup_timeout')
        self._scopes: dict[ExecutionOwner,_Scope] = {}
        self._processes: dict[asyncio.subprocess.Process,tuple[ExecutionOwner,str]] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()

    def _cleanup_done(self, task):
        self._cleanup_tasks.discard(task)
        if not task.cancelled():
            # A repeatedly cancelled caller may no longer await this task.
            # The reserved launch remains unknown; consume the exception so
            # it does not become an unobserved detached-task warning.
            task.exception()

    def open_scope(self, owner, *, fresh_claim, all_resource_paths_tracked):
        if (not isinstance(owner,ExecutionOwner) or type(owner.execution_epoch) is not int
                or owner.execution_epoch < 1 or any(not isinstance(v,str) or not v.strip()
                    for v in (owner.session_id,owner.task_id,owner.worker_id))):
            raise ResourceScopeError('A complete claimed execution owner is required')
        if fresh_claim is not True or all_resource_paths_tracked is not True:
            raise ResourceScopeError('Only a fresh claim with complete resource coverage can open a scope')
        if owner in self._scopes:
            raise ResourceScopeError('A resource scope cannot be reopened or overwritten')
        self._scopes[owner] = _Scope()

    def _scope(self, owner, *, launching=False):
        scope = self._scopes.get(owner)
        if scope is None or (launching and scope.sealed):
            raise ResourceScopeError('Unknown or sealed resource scope')
        return scope

    def reserve(self, owner, resource_id, *, kind):
        scope = self._scope(owner,launching=True)
        if not isinstance(resource_id,str) or not resource_id.strip() or kind not in {'process','external'}:
            raise ValueError('Resources require a nonempty identity and supported kind')
        if resource_id in scope.resources:
            raise ResourceScopeError('Resource identities cannot be reused')
        scope.resources[resource_id] = _Resource(kind)

    def _resource(self, owner, resource_id):
        resource = self._scope(owner).resources.get(resource_id)
        if resource is None:
            raise ResourceScopeError('Resource does not belong to this execution owner')
        return resource

    def attach_process(self, owner, resource_id, process, *, terminate_tree,
                       containment='bubblewrap_pid_namespace'):
        resource = self._resource(owner,resource_id)
        if (resource.kind != 'process' or resource.probe is not None
                or not isinstance(process,asyncio.subprocess.Process) or process in self._processes
                or not callable(terminate_tree)
                or containment not in {'bubblewrap_pid_namespace','trusted_process_group'} or os.name != 'posix'):
            raise ResourceScopeError('Unsupported, rebound or uncontained process resource')
        # Only a dedicated group created for this exact child can be signalled.
        # Never terminate the Worker's inherited process group.
        try:
            if os.getpgid(process.pid) != process.pid:
                raise ResourceScopeError('Task child must own its process group')
        except ProcessLookupError:
            if process.returncode is None:
                raise _ProcessExitPending('Launch identity disappeared before it could be registered')

        async def probe():
            if process.returncode is None:
                return False
            await process.wait()
            try:
                os.killpg(process.pid,0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            # Leader exit alone is not proof that descendants stopped.
            return False

        async def cleanup():
            # Reuse the version-pinned Backend helper. Its return alone is not
            # evidence: probe independently checks the entire original group.
            await terminate_tree(process,grace=self.grace)

        resource.probe = probe
        resource.cleanup = cleanup
        self._processes[process] = (owner,resource_id)

    def register_external(self, owner, resource_id, *, tool_call_id, cleanup, probe):
        if not isinstance(tool_call_id,str) or not tool_call_id.strip() or not callable(cleanup) or not callable(probe):
            raise ValueError('External jobs need an original tool identity and trusted cleanup/probe callbacks')
        scope = self._scope(owner,launching=True)
        if any(r.tool_call_id == tool_call_id for r in scope.resources.values()):
            raise ResourceScopeError('External tool identity is already registered')
        self.reserve(owner,resource_id,kind='external')
        resource = scope.resources[resource_id]
        resource.tool_call_id,resource.cleanup,resource.probe = tool_call_id,cleanup,probe

    async def mark_finished(self, owner, resource_id):
        """Check trusted evidence; a tool result or cancelled asyncio task is insufficient."""
        resource = self._resource(owner,resource_id)
        if resource.probe is None:
            return False
        try:
            async with asyncio.timeout(self.cleanup_timeout):
                resource.finished = await resource.probe() is True
        except Exception:
            resource.finished = False
        return resource.finished

    async def cleanup_resource(self, owner, resource_id):
        resource = self._resource(owner,resource_id)
        try:
            async with resource.lock:
                async with asyncio.timeout(self.cleanup_timeout):
                    if resource.launch is not None:
                        if not resource.launch.done():
                            return False
                        await asyncio.shield(resource.launch)
                    if resource.probe is None or resource.cleanup is None:
                        return False
                    if await resource.probe() is not True:
                        await resource.cleanup()
                    resource.finished = await resource.probe() is True
        except Exception:
            resource.finished = False
        return resource.finished

    async def verify_preparation_cleanup(self, owner):
        """Joined preparation has no Core Reply; require a tracked resource scope."""
        scope = self._scopes.get(owner)
        if scope is None:
            return False
        scope.sealed = True
        async with scope.lock:
            results = [await self.cleanup_resource(owner,rid) for rid in tuple(scope.resources)]
            return all(results)

    async def verify_parked_cleanup(self, owner, snapshot, *, agent_name):
        """Seal and clean a drained permission boundary without ending its Reply.

        The caller must have joined the Core iterator before capturing this full
        snapshot. Permission requests themselves have not launched resources.
        Submitted external calls, however, are deliberately not recoverable by
        this interface, even when a registered cleanup callback exists.
        """
        scope = self._scopes.get(owner)
        if scope is None:
            return False
        try:
            state = CoreSnapshot.model_validate(snapshot).to_state()
            if (not isinstance(agent_name, str) or not agent_name.strip()
                    or state.session_id != owner.session_id or not state.reply_id
                    or not state.context or state.context[-1].id != state.reply_id):
                return False
            pending = state.get_awaiting_tool_calls(agent_name)
            if not pending or any(call.state != 'asking' for call in pending):
                return False
            # Inspect all retained context, not only the agent's tail message:
            # another submitted external job must not disappear behind an ASK.
            for message in state.context:
                closed = {result.id for result in message.get_content_blocks('tool_result')}
                if any(call.state == 'submitted' and call.id not in closed
                       for call in message.get_content_blocks('tool_call')):
                    return False
        except (ValueError, TypeError):
            return False
        # Unsupported boundaries remain pinned and usable by their current
        # owner. Only an eligible permission boundary starts irreversible
        # admission sealing and resource cleanup.
        return await self.verify_preparation_cleanup(owner)

    async def verify_cleanup(self, owner, original, stopped):
        """Controller callback: seal admission, stop known resources, require evidence.

        Cancellation propagates and never becomes a positive result. Unknown
        launches stay registered; a subsequent proof may resolve them, but they
        are never erased just because their Python producer failed.
        """
        scope = self._scopes.get(owner)
        if scope is None:
            return False
        scope.sealed = True
        try:
            snapshots = [CoreSnapshot.model_validate(s) for s in (original,stopped) if s is not None]
            if stopped is None or any(s.to_state().session_id != owner.session_id for s in snapshots):
                return False
            current_reply = CoreSnapshot.model_validate(stopped).to_state().reply_id
            submitted = set()
            for snap in snapshots:
                state = snap.to_state()
                # A fresh Start inherits the previous Task's full Session
                # state. Its historical Reply belongs to that Task's ledger.
                if current_reply is not None and state.reply_id != current_reply:
                    continue
                for msg in state.context:
                    if current_reply is not None and msg.id != current_reply:
                        continue
                    closed = {result.id for result in msg.get_content_blocks('tool_result')}
                    submitted.update(call.id for call in msg.get_content_blocks('tool_call')
                        if call.state == 'submitted' and call.id not in closed)
        except (ValueError,TypeError):
            return False
        registered = {r.tool_call_id for r in scope.resources.values() if r.kind == 'external'}
        if not submitted <= registered:
            return False
        async with scope.lock:
            results = [await self.cleanup_resource(owner,rid) for rid in tuple(scope.resources)]
            return all(results)


class TrustedTaskBackend:
    """Marker for audited, owner-bound sandbox backends with resource tracking.

    Only workmate provider implementations may inherit this marker. A matching
    owner and live TaskResourceRegistry scope remain mandatory at use sites.
    """


class TaskResourceBackendMixin(TrustedTaskBackend):
    """Compose before FP's Bubblewrap backend for Task tools only.

    Constructor arguments resource_registry/execution_owner bind one execution;
    never rebind a shared Session gateway backend. Bubblewrap start_process is
    the common primitive used by exec_shell and streamed file writes.
    """

    _resource_containment = 'bubblewrap_pid_namespace'

    def _process_cleanup_probe(self, process, probe):
        return probe

    def __init__(self, *, resource_registry, execution_owner, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(self,BubblewrapBackend):
            raise TypeError('Task resource tracking supports only the pinned Bubblewrap backend')
        resource_registry._scope(execution_owner,launching=True)
        self._task_resources = resource_registry
        self._execution_owner = execution_owner

    async def _cleanup_unregistered_child(self, process):
        registry = self._task_resources
        if not isinstance(process,asyncio.subprocess.Process) or process in registry._processes:
            return  # Never terminate a handle already owned by another entry.
        try:
            if os.getpgid(process.pid) == process.pid:
                await self._terminate_process_tree(process,grace=registry.grace)
            elif process.returncode is None:
                # An invalid backend may have omitted start_new_session. Its
                # child is ours, but its inherited group must not be signalled.
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(),registry.grace)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        except (ProcessLookupError,OSError):
            pass  # The unresolved reservation still prevents a positive proof.

    async def start_process(self, command, **kwargs):
        resource_id = f'process:{uuid.uuid4()}'
        registry,owner = self._task_resources,self._execution_owner
        registry.reserve(owner,resource_id,kind='process')
        start = super().start_process
        async def launch_and_bind():
            process = await start(command,**kwargs)
            try:
                try:
                    registry.attach_process(owner,resource_id,process,terminate_tree=self._terminate_process_tree,
                        containment=self._resource_containment)
                except _ProcessExitPending:
                    # A very short child can be OS-reaped before asyncio's
                    # returncode callback. Join this exact known child, then
                    # retry normal registration; group probes still decide
                    # whether descendants are quiescent. Inherited pipes can
                    # hold an early wait() open, so unknown outcomes stay bounded.
                    async with asyncio.timeout(registry.cleanup_timeout):
                        await process.wait()
                    registry.attach_process(owner,resource_id,process,terminate_tree=self._terminate_process_tree,
                        containment=self._resource_containment)
                resource = registry._resource(owner, resource_id)
                resource.probe = self._process_cleanup_probe(process, resource.probe)
            except Exception:
                cleanup = asyncio.create_task(self._cleanup_unregistered_child(process))
                registry._cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(registry._cleanup_done)
                await asyncio.shield(cleanup)
                raise
            return process
        launch = asyncio.create_task(launch_and_bind())
        registry._resource(owner,resource_id).launch = launch
        try:
            return await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Spawn may already have created the child. Join the launch and
            # bind its real handle even across repeated caller cancellation.
            # Keep a strong reference until the independent cleanup completes.
            async def finish_cancelled_launch():
                await asyncio.shield(launch)
                return await registry.cleanup_resource(owner,resource_id)
            cleanup = asyncio.create_task(finish_cancelled_launch())
            registry._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(registry._cleanup_done)
            await asyncio.shield(cleanup)
            raise
