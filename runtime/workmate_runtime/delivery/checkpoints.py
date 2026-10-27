"""Best-effort file recovery points captured only at quiescent boundaries."""
import logging
import os
from .coordinator import _joined_io
from .workspace_checkpoint import WorkspaceCheckpoint
from workmate_runtime.transport.checkpoints import CheckpointStore

logger = logging.getLogger(__name__)


class CheckpointCoordinator:
    def __init__(self, pool, node, store, *, max_files=1000, max_bytes=134217728,
                 max_file_bytes=16777216):
        self.pool, self.node = pool, node
        self.store = store
        self.limits = dict(max_files=max_files, max_bytes=max_bytes, max_file_bytes=max_file_bytes)
        self.files = WorkspaceCheckpoint(store, max_files=max_files, max_bytes=max_bytes,
                                         max_file_bytes=max_file_bytes)

    async def capture(self, owner, root, *, boundary):
        try:
            return await self._capture(owner, root, boundary=boundary)
        except Exception:
            logger.exception('Workspace checkpoint unavailable for task %s', owner.task_id)
            return None

    async def capture_required(self, owner, root, *, boundary):
        """Suspension must not release compute without its configured file copy."""
        return await self._capture(owner, root, boundary=boundary, required=True)

    async def _capture(self, owner, root, *, boundary, required=False):
        # Shared exclusion protects newly published objects from offline purge
        # until the durable reference exists. No business row lock during I/O.
        from workmate_product.persistence.repositories.conversation_purge import ARTIFACT_STORAGE_LOCK
        async with self.pool.acquire() as db:
            async with db.transaction():
                await db.execute('SELECT pg_advisory_xact_lock_shared($1)', ARTIFACT_STORAGE_LOCK)
                manifest, error_code = None, None
                try:
                    local = root if isinstance(root, (str, os.PathLike)) else getattr(root, 'host_workdir', None)
                    if local is not None:
                        manifest = await _joined_io(self.files.capture, local)
                    else:
                        remote = remote_checkpoint(root, self.store, **self.limits)
                        manifest = await remote.capture()
                except Exception:
                    if required:
                        raise
                    error_code = 'checkpoint_capture_failed'
                    logger.warning('Workspace checkpoint unavailable for task %s', owner.task_id)
                try:
                    result = await CheckpointStore(db).record(owner, self.node, boundary=boundary,
                        manifest=manifest, error_code=error_code)
                    if required and (not isinstance(result, str) or not result):
                        raise RuntimeError('Required checkpoint has no committed receipt')
                    return result
                except Exception:
                    if required:
                        raise
                    logger.exception('Workspace checkpoint metadata unavailable for task %s', owner.task_id)
                    return None


def remote_checkpoint(workspace, store, **limits):
    """Use the management channel even when the Task backend has been sealed."""
    from workmate_runtime.sandbox.base import TrustedSandboxControlBackend
    from .remote_workspace_checkpoint import RemoteWorkspaceCheckpoint
    provider = getattr(workspace, '_workmate_provider', None)
    if provider not in {'docker', 'kubernetes'}:
        raise ValueError('Workspace provider has no supported file checkpoint transport')
    if hasattr(workspace, '_workmate_control'):
        backend = workspace._workmate_control
    else:
        backend = workspace.get_backend()
    if not isinstance(backend, TrustedSandboxControlBackend):
        raise ValueError('Workspace checkpoint requires a trusted management channel')
    return RemoteWorkspaceCheckpoint(backend, workspace.workdir, store, provider=provider, **limits)
