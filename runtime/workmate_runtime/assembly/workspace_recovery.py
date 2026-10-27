"""Install an authorized recovery file set before any new Task tools can run.

Only fresh Start preparation calls this helper, never approval Resume. Ownership
is acquired by the caller; this does not release, rewrite or adopt old ownership.
"""
from copy import deepcopy
import os
from pathlib import Path
import tempfile

from workmate_contracts import snapshot_digest
from workmate_product.persistence.models import RuntimeSession
from workmate_product.persistence.recovery_forks import RecoveryFork
from workmate_runtime.delivery.coordinator import _joined_io
from workmate_runtime.delivery.files import _directory
from workmate_runtime.delivery.workspace_checkpoint import WorkspaceCheckpoint


class WorkspaceRecoveryError(RuntimeError):
    pass


async def restore_recovery_workspace(context, binding, sessions, object_store, policy, *, storage_node_id):
    fork_id = getattr(context.initial_spec, 'recovery_fork_id', None)
    if fork_id is None:
        return
    owner = binding.owner
    if owner.session_id != context.session_id or owner.task_id != context.task_id:
        raise WorkspaceRecoveryError('Recovery binding does not match Task and Session')
    async with sessions() as db:
        fork = await db.get(RecoveryFork, fork_id)
        session = await db.get(RuntimeSession, context.session_id)
        if (fork is None or session is None or fork.conversation_id != context.conversation_id
                or fork.target_session_id != context.session_id
                or fork.actor_user_id != context.actor_user_id or fork.target_node_id != storage_node_id
                or session.conversation_id != context.conversation_id or session.storage_node_id != storage_node_id
                or session.superseded_at is not None
                or session.active_task_id != owner.task_id or session.owner_id != owner.worker_id
                or session.execution_epoch != owner.execution_epoch):
            raise WorkspaceRecoveryError('Recovery lineage or execution ownership differs')
        manifest = deepcopy(fork.manifest)
        if snapshot_digest(manifest) != getattr(context.initial_spec, 'recovery_manifest_sha256', None):
            raise WorkspaceRecoveryError('Recovery manifest differs from frozen execution specification')
        first_task = fork.target_task_id == context.task_id
    if not first_task:
        return
    root = getattr(binding.workspace, 'host_workdir', None)
    if object_store is None or not policy.workspace_checkpoint_enabled:
        raise WorkspaceRecoveryError('Recovery requires enabled object storage')
    limits = dict(max_files=policy.workspace_checkpoint_max_files,
        max_bytes=policy.workspace_checkpoint_max_bytes, max_file_bytes=policy.workspace_checkpoint_max_file_bytes)
    # No DB transaction remains open while fetching and validating object bytes.
    if root is not None:
        checkpoint = WorkspaceCheckpoint(object_store, **limits)
        await _joined_io(_install, checkpoint, manifest, Path(root), fork_id)
    else:
        from workmate_runtime.delivery.checkpoints import remote_checkpoint
        try:
            checkpoint = remote_checkpoint(binding.workspace, object_store, **limits)
            await checkpoint.restore(manifest)
        except Exception as error:
            raise WorkspaceRecoveryError('Recovery files unavailable or unsupported Workspace; tools must not run') from error



def _install(checkpoint, manifest, root, fork_id):
    try:
        with _directory(root) as target:
            identity = os.fstat(target)
            def assert_empty():
                with _directory(root) as current:
                    actual = os.fstat(current)
                    if (actual.st_dev, actual.st_ino) != (identity.st_dev, identity.st_ino):
                        raise WorkspaceRecoveryError('Recovery Workspace root identity changed')
                if '.workmate-recovery-attempt' in os.listdir(target):
                    raise WorkspaceRecoveryError('Recovery was already attempted; do not overwrite its Workspace')
                for name in ('input', 'working', 'output'):
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target)
                    except FileNotFoundError:
                        continue
                    try:
                        if os.listdir(fd):
                            raise WorkspaceRecoveryError('Recovery requires empty input, working and output directories')
                    finally:
                        os.close(fd)
            assert_empty()
            with tempfile.TemporaryDirectory(prefix='workmate-recovery-') as temporary:
                stage = Path(temporary) / 'verified'
                checkpoint.restore(manifest, stage)
                assert_empty()
                # A crash during installation fails closed on retry, including an
                # empty checkpoint. Successful same-task Resume skips this helper.
                marker = os.open('.workmate-recovery-attempt', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=target)
                with os.fdopen(marker, 'w') as output:
                    output.write(fork_id)
                    output.flush()
                    os.fsync(output.fileno())
                # Copy from verified private snapshots rather than rename across
                # filesystems. All destinations are exclusively created.
                for directory in sorted(manifest['directories'], key=lambda p: (p.count('/'), p)):
                    with _directory(root / Path(directory).parent) as parent:
                        try:
                            os.mkdir(Path(directory).name, mode=0o700, dir_fd=parent)
                        except FileExistsError:
                            if directory not in {'input', 'working', 'output'}:
                                raise
                for entry in manifest['files']:
                    path = Path(entry['path'])
                    with _directory(root / path.parent) as parent:
                        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o700 if entry['executable'] else 0o600, dir_fd=parent)
                        with os.fdopen(fd, 'wb') as output, (stage / path).open('rb') as source:
                            while chunk := source.read(1024 * 1024):
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                        os.fsync(parent)
                os.fsync(target)
    except Exception as error:
        if isinstance(error, WorkspaceRecoveryError):
            raise
        raise WorkspaceRecoveryError('Recovery files unavailable or installation incomplete; tools must not run') from error
