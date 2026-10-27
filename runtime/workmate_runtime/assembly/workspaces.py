"""Worker-owned Core workspaces, independent of AgentScope App managers."""
import asyncio
import time
from dataclasses import dataclass,asdict
import hashlib
import json
import logging
from pathlib import Path

from agentscope.tool import BackendBase,Glob,Grep
from agentscope.workspace import WorkspaceBase
from .workspace_ownership import WorkspaceOwnershipProvider,WorkspaceOwnershipLease,WorkspaceOwnershipError
from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.workspaces import (WorkmateSandboxBash,WorkmateSandboxRead,WorkmateSandboxWrite,WorkmateSandboxEdit)
from workmate_runtime.sandbox import (SandboxProvider, SandboxWorkspaceRequest,
    BubblewrapSandboxProvider, WorkspaceBindingError)
# Compatibility imports; concrete backend types belong to the provider.
from workmate_runtime.sandbox.bubblewrap import _TaskBackend, _NestedTaskBackend
from workmate_runtime.sandbox.placement import workspace_request


logger = logging.getLogger(__name__)


class _InitializationCleanupFailed(WorkspaceBindingError):
    """Retain the exact partially initialized workspace until cleanup succeeds."""
    def __init__(self, workspace, initialization_error, cleanup_error, identity=None):
        super().__init__('Workspace initialization cleanup failed; another gateway must not be created')
        self.identity = identity
        self.workspace = workspace
        self.initialization_error = initialization_error
        self.cleanup_error = cleanup_error


@dataclass(frozen=True)
class WorkspaceIdentity:
    user_id: str
    agent_id: str
    conversation_id: str
    session_id: str
    storage_node_id: str

    def __post_init__(self):
        if any(not isinstance(v,str) or not v.strip() or len(v)>128 for v in vars(self).values()):
            raise ValueError('Workspace identity fields must be nonempty trusted identifiers')


class TaskWorkspaceOffloader:
    """Pinned Core Offloader protocol, using only the Task-bound backend.

    No lifecycle or gateway methods are exposed. All three Core offload paths
    resolve this backend, including nested media offload during compression.
    """
    def __init__(self, backend, owner, registry, workdir):
        self._backend,self._owner,self._registry = backend,owner,registry
        self._sessions_dir = backend.join_path(workdir,'sessions')
        self._data_dir = backend.join_path(workdir,'data')

    def get_backend(self):
        self._registry._scope(self._owner,launching=True)
        return self._backend

    def _session(self, session_id):
        if session_id != self._owner.session_id:
            raise WorkspaceBindingError('Offload belongs to another Session')

    async def offload_context(self,session_id,msgs):
        self._session(session_id)
        return await WorkspaceBase.offload_context(self,session_id,msgs)

    async def offload_tool_result(self,session_id,tool_result):
        self._session(session_id)
        return await WorkspaceBase.offload_tool_result(self,session_id,tool_result)

    async def offload_data_block(self,block):
        self.get_backend()
        return await WorkspaceBase.offload_data_block(self,block)


@dataclass(frozen=True)
class TaskWorkspaceBinding:
    owner: ExecutionOwner
    workspace: WorkspaceBase
    backend: BackendBase
    tools: tuple
    offloader: TaskWorkspaceOffloader
    instructions: str
    provider: SandboxProvider


class WorkerWorkspaceManager:
    """One initialized workspace per trusted Session; one backend per Task owner.

    close() seals new Task resource launches and releases workspace infrastructure.
    It is not Task cleanup proof and never mutates Runtime/Product gates. Worker
    shutdown must first cancel and join its controllers and resource cleanup.
    """
    def __init__(self, basedir, *, storage_node_id, resource_registry,
                 ownership_provider: WorkspaceOwnershipProvider,
                 workspace_factory=WorkerMCPWorkspace, provider: SandboxProvider | None = None, residency=None):
        if not isinstance(storage_node_id,str) or not storage_node_id.strip():
            raise ValueError('An explicit storage node identity is required')
        if not callable(getattr(ownership_provider,'acquire',None)):
            raise TypeError('An explicit Workspace ownership provider is required')
        self._ownership_provider=ownership_provider
        self.residency = residency
        self._holds = {}
        self._residence_owners = {}
        self._idle_release_claims = {}
        self.basedir = Path(basedir).absolute()
        if self.basedir.is_symlink():
            raise WorkspaceBindingError('Workspace root cannot be a symbolic link')
        self.basedir.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.basedir = self.basedir.resolve()
        self.storage_node_id,self.resource_registry = storage_node_id,resource_registry
        self.provider = provider if provider is not None else BubblewrapSandboxProvider(workspace_factory=workspace_factory)
        self._entries,self._identities,self._bindings = {},{},{}
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task = None
        self._closed_workspaces = set()
        self._workspace_close_tasks = {}
        self._ownership: dict[WorkspaceIdentity,WorkspaceOwnershipLease] = {}
        self._workspace_identities = {}
        # Lifecycle maps use object IDs. Retain each actual object so a failed
        # initialization retry cannot reuse an ID and inherit a closed marker.
        self._workspace_objects = {}
        self._retired_sessions = set()
        self._retirement_tasks = {}
        self._idle = {}
        self._idle_closed = set()
        self._idle_failures = {}

    def _validate_identity(self, identity):
        if not isinstance(identity,WorkspaceIdentity) or identity.storage_node_id != self.storage_node_id:
            raise WorkspaceBindingError('Workspace storage affinity does not match this Worker')
        if identity.session_id in self._retired_sessions:
            raise WorkspaceBindingError('Session Workspace has been retired by this Worker')
        if identity.session_id in self._identities and self._identities[identity.session_id] != identity:
            raise WorkspaceBindingError('Session cannot be rebound to another workspace identity')

    async def _initialize(self, identity, owner=None, expected_storage_proof=None):
        digest = hashlib.sha256(json.dumps([identity.user_id,identity.agent_id,
            identity.conversation_id,identity.session_id],separators=(',',':')).encode()).hexdigest()
        try:
            lease=self._ownership_provider.acquire(self.basedir,digest,asdict(identity))
        except WorkspaceOwnershipError as error:
            raise WorkspaceBindingError(str(error)) from error
        self._ownership[identity]=lease
        workspace=None
        try:
            if self.residency is not None:
                self._holds[identity] = await self.residency.record_held(identity,owner,lease.generation)
                self._residence_owners[identity] = owner
            workspace=self.provider.create_workspace(workspace_request(self.basedir,digest,lease.path,self.provider.name, expected_storage_proof=expected_storage_proof))
            self._workspace_objects[id(workspace)]=workspace
            self._workspace_identities[id(workspace)]=identity
            await self.provider.initialize_workspace(workspace)
        except BaseException as initialization_error:
            try:
                if workspace is not None:
                    await self._close_workspace(workspace)
                self._release_ownership(identity)
                await self._release_residence(identity,owner)
                if workspace is not None:
                    self._forget_workspace(workspace)
            except BaseException as cleanup_error:
                raise _InitializationCleanupFailed(workspace,initialization_error,cleanup_error,identity) from cleanup_error
            raise
        return workspace

    def _forget_workspace(self, workspace):
        """Forget only confirmed closed objects; failures retain original handles."""
        key = id(workspace)
        self._closed_workspaces.discard(key)
        self._workspace_close_tasks.pop(key, None)
        self._workspace_identities.pop(key, None)
        self._workspace_objects.pop(key, None)

    async def _begin_idle_release(self, identity):
        if self.residency is None or identity not in self._holds:
            return True
        if identity not in self._idle_release_claims:
            claim = await self.residency.begin_idle_release(identity,self._holds[identity])
            if claim is None:
                return False
            self._idle_release_claims[identity] = claim
        return True

    async def _release_residence(self, identity, owner=None):
        hold = self._holds.get(identity)
        if self.residency is None or hold is None:
            return
        claim = self._idle_release_claims.get(identity)
        if claim is not None:
            await self.residency.finish_idle_release(claim)
        else:
            await self.residency.release_owned(identity,owner or self._residence_owners[identity],hold)
        self._holds.pop(identity, None)
        self._residence_owners.pop(identity, None)
        self._idle_release_claims.pop(identity, None)

    async def _block_idle_release(self, identity, error):
        claim = self._idle_release_claims.get(identity)
        if self.residency is not None and claim is not None:
            await self.residency.block_idle_release(claim,type(error).__name__)

    async def _close_workspace(self,workspace):
        key=id(workspace)
        if key in self._closed_workspaces:
            return
        task=self._workspace_close_tasks.get(key)
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            task=asyncio.create_task(self.provider.close_workspace(workspace))
            self._workspace_close_tasks[key]=task
        await asyncio.shield(task)
        self._closed_workspaces.add(key)

    def _release_ownership(self,identity):
        lease=self._ownership.get(identity)
        if lease is None:
            return
        # close() seals admission before this check. Never turn an unresolved
        # process launch or external job into a clean Workspace handoff.
        self._verify_workspace_resources(identity)
        lease.close(clean=True)
        self._ownership.pop(identity)

    def _verify_workspace_resources(self, identity):
        for owner, (bound_identity, _, _) in self._bindings.items():
            if bound_identity != identity:
                continue
            scope = self.resource_registry._scope(owner)
            if not scope.sealed or any(not r.finished or (r.launch is not None and
                    (not r.launch.done() or r.launch.cancelled() or r.launch.exception() is not None))
                    for r in scope.resources.values()):
                raise WorkspaceBindingError('Task resources are not safely releasable')

    async def close_task_workspace(self, owner):
        """Release clean local compute before terminal publication in cold mode.

        Immutable delivery bytes must already be sealed. Keep Task references
        until final delivery succeeds; a failed close retains ownership and
        prevents cached infrastructure reuse. Active/parked Tasks cannot close.
        """
        async with self._lock:
            binding = self._bindings.get(owner)
            if binding is None:
                return
            identity = binding[0]
            self._verify_workspace_resources(identity)
            workspace = self._entries[identity].result()
            try:
                await self._close_workspace(workspace)
                self._release_ownership(identity)
                await self._release_residence(identity,owner)
            except BaseException as error:
                self._idle_failures[identity] = type(error).__name__
                raise
            self._idle_failures.pop(identity, None)

    async def close_prepared_workspace(self, owner):
        """After preparation has joined, release even an unbound initialization.

        get_workspace shields initialization from caller cancellation. Join it
        before publishing EXECUTION_NOT_STARTED, otherwise a peer can acquire
        the next Task while the abandoned initialization still owns its files.
        """
        if owner in self._bindings:
            await self.close_task_workspace(owner)
            return
        async with self._lock:
            identity = self._identities.get(owner.session_id)
            if any(bound_identity == identity for bound_identity,_,_ in self._bindings.values()):
                raise WorkspaceBindingError('Preparation cleanup belongs to a different Task hold')
            pending = self._entries.get(identity)
            if pending is None:
                return
            try:
                workspace = await asyncio.shield(pending)
            except _InitializationCleanupFailed as error:
                workspace = error.workspace
            except Exception:
                # Ordinary initialization failure has already cleaned itself.
                self._entries.pop(identity)
                return
            try:
                if identity in self._holds and identity in self._ownership:
                    # A new Task can fail before bind_task refreshes the warm
                    # owner. Conversely, delayed old cleanup must not close a
                    # new Task's pre-bind workspace, even if local metadata
                    # still names the old owner. PG validates the exact owner.
                    await self._refresh_residence_owner(identity,owner)
                if workspace is not None:
                    await self._close_workspace(workspace)
                self._release_ownership(identity)
                await self._release_residence(identity,owner)
            except BaseException as error:
                self._idle_failures[identity] = type(error).__name__
                raise
            self._entries.pop(identity)
            self._idle.pop(identity, None)
            self._idle_failures.pop(identity, None)
            self._idle_closed.add(identity.session_id)
            if workspace is not None:
                self._forget_workspace(workspace)

    async def get_workspace(self, identity, *, owner=None, expected_storage_proof=None):
        if self.residency is not None and owner is None:
            raise WorkspaceBindingError("Durable residence requires the claimed Task owner")
        async with self._lock:
            if self._closed:
                raise WorkspaceBindingError('Workspace manager is closed')
            self._validate_identity(identity)
            if identity in self._idle_failures:
                raise WorkspaceBindingError('Idle closure failed; Workspace requires cleanup')
            self._idle.pop(identity, None)
            self._idle_closed.discard(identity.session_id)
            self._identities[identity.session_id] = identity
            task = self._entries.get(identity)
            if task is None:
                task = asyncio.create_task(self._initialize(identity,owner,expected_storage_proof))
                self._entries[identity] = task
        # A disconnected caller cannot cancel initialization shared by another
        # caller. close() owns draining and closing pending initialization.
        try:
            workspace = await asyncio.shield(task)
        except Exception as error:
            async with self._lock:
                if self._entries.get(identity) is task and not isinstance(error,_InitializationCleanupFailed):
                    self._entries.pop(identity)
            raise
        async with self._lock:
            if self._closed or identity.session_id in self._retired_sessions:
                raise WorkspaceBindingError('Workspace manager closed or Session retired during initialization')
            if id(workspace) in self._closed_workspaces:
                raise WorkspaceBindingError('Workspace closed pending Task finalization')
        if expected_storage_proof is not None:
            await self.provider.verify_persistent_storage(workspace, expected_storage_proof)
        return workspace

    def _task_backend(self, workspace, owner):
        return self.provider.bind_backend(workspace,owner,self.resource_registry)

    async def _refresh_residence_owner(self, identity, owner):
        """Fence warm ownership under the manager lock before using or closing it."""
        previous = self._holds[identity]
        refreshed = await self.residency.record_held(
            identity,owner,self._ownership[identity].generation)
        if any(refreshed[key] != previous[key]
                for key in ('hold_version','workspace_generation')):
            raise WorkspaceBindingError('Warm reuse changed the compute holding')
        self._holds[identity] = refreshed
        self._residence_owners[identity] = owner

    async def bind_task(self, identity, owner, *, fresh_claim, all_resource_paths_tracked,
                        read_model_input_types, expected_storage_proof=None):
        self._validate_identity(identity)
        if (not isinstance(owner,ExecutionOwner) or owner.session_id != identity.session_id
                or type(fresh_claim) is not bool or all_resource_paths_tracked is not True):
            raise WorkspaceBindingError('Task identity and complete resource coverage are required')
        if (not isinstance(read_model_input_types,(list,tuple)) or not read_model_input_types
                or any(not isinstance(v,str) or not v.strip() for v in read_model_input_types)):
            raise ValueError('Read media types must come from the selected model formatter')
        async with self._lock:
            if not fresh_claim and owner not in self._bindings:
                raise WorkspaceBindingError('Resume requires the existing same-process Task binding')
        workspace = await self.get_workspace(identity,owner=owner,expected_storage_proof=expected_storage_proof)
        async with self._lock:
            if self._closed or identity.session_id in self._retired_sessions:
                raise WorkspaceBindingError('Workspace manager closed or Session retired during Task binding')
            if self.residency is not None and self._residence_owners.get(identity) != owner:
                # A warm compute hold survives Task boundaries, but its durable
                # last Task/epoch must advance before any new backend is exposed.
                # The manager lock excludes TTL, typed release and retirement.
                try:
                    await self._refresh_residence_owner(identity,owner)
                except BaseException as error:
                    self._idle_failures[identity] = type(error).__name__
                    raise
            existing = self._bindings.get(owner)
            if existing is None:
                self.resource_registry.open_scope(owner,fresh_claim=fresh_claim,
                    all_resource_paths_tracked=all_resource_paths_tracked)
                backend = self._task_backend(workspace,owner)
                offloader = TaskWorkspaceOffloader(backend,owner,self.resource_registry,workspace.workdir)
                self._bindings[owner] = (identity,backend,offloader)
            else:
                if existing[0] != identity:
                    raise WorkspaceBindingError('Task workspace identity changed')
                self.resource_registry._scope(owner,launching=True)
                _,backend,offloader = existing
            cache = dict(backend=backend,sandbox_workdir=workspace.workdir,host_workdir=getattr(workspace,"host_workdir",None))
            glob_kwargs = dict(backend=backend)
            helper = getattr(workspace,'_workmate_glob_helper_path',workspace._glob_helper_path)
            if helper is not None:
                glob_kwargs['glob_helper_path'] = helper
            tools = (WorkmateSandboxBash(cwd=workspace.workdir,backend=backend),WorkmateSandboxEdit(**cache),
                Glob(**glob_kwargs),Grep(backend=backend),
                WorkmateSandboxRead(**cache,model_input_types=list(read_model_input_types)),WorkmateSandboxWrite(**cache))
            return TaskWorkspaceBinding(owner,workspace,backend,tools,offloader,
                workspace.instructions.format(workdir=workspace.workdir),self.provider)

    async def suspend_task_workspace(self, owner, snapshot, *, agent_name, checkpoint=None):
        """Local permission-pause primitive; not a durable owner handoff.

        The suspension controller must exclude Resume/Stop through the
        durable protocol before calling this method, supply the exact committed
        snapshot for this owner, and persist release proof afterwards.
        Close compute while preserving files and the caller's delivery baseline.
        Failure retains the binding and ownership; a sealed scope cannot resume.
        Only verified local and persistent Docker/Kubernetes lifecycles are supported.
        """
        async with self._lock:
            if self._closed or self.provider.name not in {'bubblewrap', 'docker', 'kubernetes'}:
                raise WorkspaceBindingError('Workspace cannot be suspended by this manager')
            binding = self._bindings.get(owner)
            if binding is None:
                raise WorkspaceBindingError('Suspension requires a known local Task binding')
            identity = binding[0]
            if identity.session_id in self._retired_sessions:
                raise WorkspaceBindingError('Retired Workspace cannot be suspended')
            clean = await self.resource_registry.verify_parked_cleanup(
                owner, snapshot, agent_name=agent_name)
            if clean is not True:
                raise WorkspaceBindingError('Permission pause resources are not safely releasable')
            self._verify_workspace_resources(identity)
            workspace = self._entries[identity].result()
            try:
                remote = self.provider.name in {'docker', 'kubernetes'}
                storage_proof = await self.provider.capture_persistent_storage(workspace) if remote else None
                if remote and (not isinstance(storage_proof, dict) or not storage_proof):
                    raise WorkspaceBindingError('Persistent storage continuity proof unavailable')
                if checkpoint is not None and remote:
                    await checkpoint(owner, workspace, boundary="approval")
                if remote:
                    await self.provider.verify_persistent_storage(workspace, storage_proof)
                await self._close_workspace(workspace)
                if checkpoint is not None and not remote:
                    await checkpoint(owner, workspace.host_workdir, boundary="approval")
                self._release_ownership(identity)
                await self._release_residence(identity,owner)
            except BaseException as error:
                self._idle_failures[identity] = type(error).__name__
                raise
            self._idle_failures.pop(identity, None)
            self._drop_task_binding(owner, binding)
            return storage_proof

    async def release_task(self, owner):
        """Called only after durable terminal delivery and controller completion.

        Validate all evidence before removing references. Unknown scopes remain
        fail-closed for stale backends after release; repeated release is harmless.
        """
        async with self._lock:
            binding = self._bindings.get(owner)
            if binding is None:
                return
            identity, backend, _ = binding
            scope = self.resource_registry._scope(owner)
            if not scope.sealed or any(not r.finished or (r.launch is not None and
                    (not r.launch.done() or r.launch.cancelled() or r.launch.exception() is not None))
                    for r in scope.resources.values()):
                raise WorkspaceBindingError('Task resources are not safely releasable')
            self._drop_task_binding(owner, binding)

    def _drop_task_binding(self, owner, binding):
        """Remove local references under the manager lock after verified cleanup."""
        identity, backend, _ = binding
        workspace = self._entries[identity].result()
        if hasattr(workspace, '_local_bindings'):
            workspace._local_bindings = [b for b in workspace._local_bindings if b is not backend]
        self._bindings.pop(owner)
        self.resource_registry._scopes.pop(owner)
        for process, (bound_owner, _) in tuple(self.resource_registry._processes.items()):
            if bound_owner == owner:
                self.resource_registry._processes.pop(process)
        if not any(item[0] == identity for item in self._bindings.values()):
            if id(workspace) in self._closed_workspaces:
                self._entries.pop(identity)
                self._idle.pop(identity, None)
                self._idle_closed.add(identity.session_id)
                self._forget_workspace(workspace)
            else:
                self._idle[identity] = time.monotonic()

    def affinity_holds(self):
        """Hints only: expose initialized, locally held compute, never authority."""
        hints = []
        if self._closed:
            return ()
        for identity,hold in self._holds.items():
            pending = self._entries.get(identity)
            if (identity not in self._ownership or identity in self._idle_failures
                    or identity in self._idle_release_claims or identity.session_id in self._retired_sessions
                    or pending is None or not pending.done() or pending.cancelled()
                    or pending.exception() is not None
                    or id(pending.result()) in self._closed_workspaces):
                continue
            hints.append(dict(hold))
        return tuple(hints)

    async def release_idle_session(self, session_id, hold_version, generation):
        """A typed release cannot close active compute or a replacement hold."""
        async with self._lock:
            identity = self._identities.get(session_id)
            hold = self._holds.get(identity)
            if (self._closed or identity not in self._idle or identity in self._idle_failures
                    or hold is None or type(hold_version) is not int
                    or hold.get('hold_version') != hold_version
                    or hold.get('workspace_generation') != generation):
                return False
            return await self._evict_idle(identity)

    async def _evict_idle(self, identity):
        """Caller owns the manager lock; durable CAS precedes any compute close."""
        pending = self._entries.get(identity)
        if pending is None or not pending.done() or identity.session_id in self._retired_sessions:
            return False
        workspace = pending.result()
        if not await self._begin_idle_release(identity):
            return False
        try:
            await self._close_workspace(workspace)
            self._release_ownership(identity)
            await self._release_residence(identity)
        except BaseException as error:
            self._idle_failures[identity] = type(error).__name__
            await self._block_idle_release(identity,error)
            raise
        self._entries.pop(identity)
        self._idle.pop(identity)
        self._idle_closed.add(identity.session_id)
        self._forget_workspace(workspace)
        return True

    async def sweep_idle(self, *, ttl, capacity):
        """Serialize eviction with acquire, binding, retirement and shutdown.

        Only providers with independently persistent files participate. Keeping
        the existing manager lock during closure intentionally favors a simple
        safe implementation over concurrent eviction machinery.
        """
        if self.provider.name not in {'bubblewrap', 'docker', 'kubernetes'}:
            return
        async with self._lock:
            if self._closed:
                return
            candidates = sorted(self._idle, key=self._idle.get)
            excess = max(0, len(candidates) - capacity)
            for index, identity in enumerate(candidates):
                if identity in self._idle_failures:
                    continue
                if index >= excess and time.monotonic() - self._idle[identity] < ttl:
                    continue
                try:
                    await self._evict_idle(identity)
                except Exception as error:
                    logger.error("Workspace idle closure failed session_id=%s exception_type=%s",
                        identity.session_id,type(error).__name__)

    def _storage_lease(self, identity):
        digest = hashlib.sha256(json.dumps([identity.user_id, identity.agent_id,
            identity.conversation_id, identity.session_id], separators=(',', ':')).encode()).hexdigest()
        from .posix_workspace_ownership import OWNERSHIP_DIRECTORY_NAME, RECORD_FILE_SUFFIX
        placement = self.basedir / ('.sandbox-' + digest + '.json')
        record = self.basedir / OWNERSHIP_DIRECTORY_NAME / (digest + RECORD_FILE_SUFFIX)
        if placement.exists() and not record.exists():
            raise WorkspaceBindingError('Provisioned Workspace has no cleanup ownership evidence')
        try:
            lease = self._ownership_provider.acquire(self.basedir, digest, asdict(identity))
        except WorkspaceOwnershipError as error:
            raise WorkspaceBindingError(str(error)) from error
        return digest, lease

    async def retire_stored_workspace(self, identity):
        """Recheck clean persisted ownership after restart; never start compute."""
        async with self._lock:
            if identity.storage_node_id != self.storage_node_id:
                raise WorkspaceBindingError('Retirement storage node differs')
            self._retired_sessions.add(identity.session_id)
            _, lease = self._storage_lease(identity)
            lease.close(clean=True)
            self._identities[identity.session_id] = identity
            self._idle_closed.add(identity.session_id)
            return True

    async def purge_workspace(self, identity):
        """Caller has a durable retirement receipt and permanently sealed admission."""
        from workmate_product.application.storage_cleanup import remove_storage_path
        from workmate_runtime.delivery.coordinator import _joined_io
        from workmate_runtime.sandbox.purge import purge_provider_storage
        async with self._lock:
            if (identity.storage_node_id != self.storage_node_id
                    or identity.session_id not in self._retired_sessions
                    or identity in self._ownership):
                raise WorkspaceBindingError('Workspace must be retired before purge')
            digest, lease = self._storage_lease(identity)
            try:
                placement = self.basedir / ('.sandbox-' + digest + '.json')
                if placement.is_symlink():
                    raise WorkspaceBindingError('Unsafe sandbox placement')
                if placement.exists():
                    record = json.loads(placement.read_text())
                    if record.get('provider') != self.provider.name:
                        raise WorkspaceBindingError('Purge requires the original provider configuration')
                    await purge_provider_storage(self.provider, record['resource_id'])
                # Retain the empty inode and tiny ownership/placement records for
                # crash-safe retry; no user working files survive this operation.
                await _joined_io(remove_storage_path, self.basedir, digest, keep_directory=True)
            finally:
                # Purge starts no processes. Even partial file deletion is safe
                # to retry after restart, while Product admission stays closed.
                lease.close(clean=True)

    async def retire_session(self,session_id):
        """Close only a locally held Session; never construct or delete a Workspace.

        Retirement permanently seals this manager's Session admission. A true
        result proves that its existing gateway and verified local resource scope
        were closed; it is not evidence about another process or missing owner.
        Caller cancellation cannot abandon or duplicate an in-flight close.
        Failed cleanup retains the dirty lease and can be retried by this owner.
        """
        if not isinstance(session_id,str) or not session_id.strip() or len(session_id)>128:
            raise WorkspaceBindingError('An exact Session identity is required')
        async with self._lock:
            self._retired_sessions.add(session_id)
            identity=self._identities.get(session_id)
            pending=self._entries.get(identity)
            cold_closed = (pending is not None and pending.done()
                and not pending.cancelled() and pending.exception() is None
                and id(pending.result()) in self._closed_workspaces
                and identity not in self._ownership and identity not in self._holds)
            if pending is None or cold_closed:
                if not cold_closed and session_id not in self._idle_closed:
                    return False
                # A different Worker may have acquired this storage after idle
                # eviction. Recheck the real lease, never certify its resources
                # using our historical process-local closure record.
                digest = hashlib.sha256(json.dumps([identity.user_id, identity.agent_id,
                    identity.conversation_id, identity.session_id], separators=(',', ':')).encode()).hexdigest()
                try:
                    lease = self._ownership_provider.acquire(self.basedir, digest, asdict(identity))
                except WorkspaceOwnershipError as error:
                    raise WorkspaceBindingError(str(error)) from error
                lease.close(clean=True)
                return True
            for owner,(bound_identity,_,_) in self._bindings.items():
                if bound_identity==identity:
                    self.resource_registry._scope(owner).sealed=True
            task=self._retirement_tasks.get(session_id)
            if task is None or (task.done() and (task.cancelled() or task.exception() is not None or not task.result())):
                task=asyncio.create_task(self._retire_workspace(identity,pending))
                self._retirement_tasks[session_id]=task
        return await asyncio.shield(task)

    async def _retire_workspace(self,identity,pending):
        try:
            workspace=await asyncio.shield(pending)
        except _InitializationCleanupFailed as error:
            workspace=error.workspace
        except (Exception,asyncio.CancelledError):
            # An ordinary failed initialization has already cleaned its partial
            # infrastructure. Without a retained local entry, do not manufacture
            # a Session retirement receipt from that failure.
            return False
        if not await self._begin_idle_release(identity):
            return False
        try:
            if workspace is not None:
                await self._close_workspace(workspace)
            self._release_ownership(identity)
            await self._release_residence(identity)
        except BaseException as error:
            await self._block_idle_release(identity,error)
            raise
        async with self._lock:
            for owner,binding in tuple(self._bindings.items()):
                if binding[0] == identity:
                    self._drop_task_binding(owner,binding)
            self._entries.pop(identity,None)
            self._idle.pop(identity,None)
            self._idle_closed.add(identity.session_id)
            if workspace is not None:
                self._forget_workspace(workspace)
        return True

    async def close(self):
        async with self._lock:
            failed_close = (self._close_task is not None and self._close_task.done()
                and (self._close_task.cancelled() or self._close_task.exception() is not None))
            if self._close_task is None or failed_close:
                self._closed = True
                for owner in self._bindings:
                    self.resource_registry._scope(owner).sealed = True
                self._close_task = asyncio.create_task(self._close_all(tuple(self._entries.items())))
            task = self._close_task
        await asyncio.shield(task)

    async def _close_all(self, pending):
        results = await asyncio.gather(*(task for _,task in pending),return_exceptions=True)
        workspaces = [(result.workspace,result.identity) if isinstance(result,_InitializationCleanupFailed)
            else (result,identity)
            for (identity,_),result in zip(pending,results)
            if not isinstance(result,BaseException) or isinstance(result,_InitializationCleanupFailed)]
        async def close_one(workspace,identity):
            if (workspace is not None and id(workspace) not in self._workspace_objects
                    and identity not in self._ownership and identity not in self._holds):
                return  # A concurrent retirement already closed and forgot it.
            if identity in self._idle and not await self._begin_idle_release(identity):
                raise WorkspaceBindingError('Residence cannot close while durable execution is active')
            if workspace is not None:
                await self._close_workspace(workspace)
            self._release_ownership(identity)
            await self._release_residence(identity)
        closed = await asyncio.gather(*(close_one(workspace,identity) for workspace,identity in workspaces),return_exceptions=True)
        errors = [e for e in closed if isinstance(e,BaseException)]
        if errors:
            raise BaseExceptionGroup('Workspace infrastructure cleanup failed',errors)
