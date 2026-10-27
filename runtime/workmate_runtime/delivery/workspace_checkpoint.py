"""Bounded file checkpoints, called only after verified execution quiescence.

This module conveys files, never execution authority, Core state or credentials.
A caller must authorize the manifest and keep the fresh target private until the
restore succeeds. Published objects are immutable; failed captures may leave
unreferenced objects for the existing retention process.
"""
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from workmate_product.application.workspace_checkpoint_manifest import validate_manifest, validate_checkpoint_path
from .files import _directory, _fingerprint, DeliveryFileError


class CheckpointError(RuntimeError):
    pass


class WorkspaceCheckpoint:
    def __init__(self, object_store, *, max_files, max_bytes, max_file_bytes=None):
        if object_store is None or any(type(n) is not int or n < 1 for n in (max_files, max_bytes)):
            raise ValueError('An object store and positive checkpoint limits are required')
        if max_file_bytes is not None and (type(max_file_bytes) is not int or max_file_bytes < 1):
            raise ValueError('Positive per-file checkpoint limit required')
        self.max_file_bytes = max_bytes if max_file_bytes is None else min(max_file_bytes, max_bytes)
        self.store, self.max_files, self.max_bytes = object_store, max_files, max_bytes

    @staticmethod
    def _path(value):
        try:
            return validate_checkpoint_path(value)
        except ValueError as error:
            raise CheckpointError(str(error)) from error

    def capture(self, workspace_root):
        files, directories, total, visited = [], [], 0, 0
        fingerprints = {}
        try:
            with _directory(workspace_root) as root:
                root_fingerprint = _fingerprint(os.fstat(root))
                def walk(fd, prefix):
                    nonlocal total, visited
                    self._path(prefix)
                    directories.append(prefix)
                    fingerprints[prefix] = _fingerprint(os.fstat(fd))
                    for name in sorted(os.listdir(fd)):
                        path = prefix + '/' + name
                        self._path(path)
                        visited += 1
                        if visited > self.max_files * 4 + 3:
                            raise CheckpointError('Checkpoint inventory exceeds limit')
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            try:
                                walk(child, path)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(info.st_mode):
                            if info.st_nlink != 1:
                                raise CheckpointError('Checkpoint contains a hardlinked file')
                            if len(files) >= self.max_files or total + info.st_size > self.max_bytes or info.st_size > self.max_file_bytes:
                                raise CheckpointError('Checkpoint file or byte limit exceeded')
                            raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                            try:
                                before = os.fstat(raw)
                                if before.st_nlink != 1 or _fingerprint(before) != _fingerprint(info):
                                    raise CheckpointError('Checkpoint source changed')
                                with tempfile.TemporaryFile() as spool:
                                    digest, size = hashlib.sha256(), 0
                                    while chunk := os.read(raw, min(1024 * 1024, self.max_bytes - total - size + 1)):
                                        size += len(chunk)
                                        if total + size > self.max_bytes or size > self.max_file_bytes:
                                            raise CheckpointError('Checkpoint source grew beyond limit')
                                        spool.write(chunk)
                                        digest.update(chunk)
                                    after = os.fstat(raw)
                                    named = os.stat(name, dir_fd=fd, follow_symlinks=False)
                                    if (after.st_nlink != 1 or named.st_nlink != 1 or size != before.st_size
                                            or _fingerprint(before) != _fingerprint(after)
                                            or _fingerprint(before) != _fingerprint(named)):
                                        raise CheckpointError('Checkpoint source changed')
                                    checksum = digest.hexdigest()
                                    key = f'.objects/sha256/{checksum[:2]}/{checksum}'
                                    spool.seek(0)
                                    self.store.publish_file(key, spool, checksum, size)
                                files.append(dict(path=path, storage_key=key, sha256=checksum,
                                                  size=size, executable=bool(before.st_mode & 0o111)))
                                total += size
                                fingerprints[path] = _fingerprint(before)
                            finally:
                                os.close(raw)
                        else:
                            raise CheckpointError('Checkpoint contains a symlink or special file')
                for name in ('input', 'working', 'output'):
                    try:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                    except FileNotFoundError:
                        continue
                    try:
                        walk(child, name)
                    finally:
                        os.close(child)
                if _fingerprint(os.fstat(root)) != root_fingerprint:
                    raise CheckpointError('Checkpoint root changed during capture')
                for path, fingerprint in fingerprints.items():
                    with _directory(Path(workspace_root) / Path(path).parent) as parent:
                        current = os.stat(Path(path).name, dir_fd=parent, follow_symlinks=False)
                    if (stat.S_ISREG(current.st_mode) and current.st_nlink != 1) or _fingerprint(current) != fingerprint:
                        raise CheckpointError('Checkpoint source changed before publication completed')
        except (OSError, DeliveryFileError) as error:
            raise CheckpointError('Checkpoint source is unavailable or unsafe') from error
        return dict(version=1, directories=directories, files=files, total_bytes=total)

    def _validate(self, manifest):
        try:
            return validate_manifest(manifest, max_files=self.max_files,
                max_bytes=self.max_bytes, max_file_bytes=self.max_file_bytes)
        except ValueError as error:
            raise CheckpointError(str(error)) from error

    def restore(self, manifest, workspace_root):
        """Verify every object before creating a new target; never merge or overwrite.

On filesystem failure the target can be incomplete and must remain unbound. The
caller owns its cleanup, and must never launch execution after any exception.
        """
        files, directories = self._validate(manifest)
        snapshots = []
        try:
            for entry in files:
                snapshot = self.store.snapshot(entry['storage_key'], entry['sha256'], entry['size'])
                snapshots.append(snapshot)
                digest, size = hashlib.sha256(), 0
                with snapshot.path.open('rb') as source:
                    while chunk := source.read(min(1024 * 1024, entry['size'] + 1 - size)):
                        size += len(chunk)
                        digest.update(chunk)
                        if size > entry['size']:
                            raise CheckpointError('Checkpoint object exceeds declared size')
                if size != entry['size'] or digest.hexdigest() != entry['sha256']:
                    raise CheckpointError('Checkpoint object integrity check failed')
            target = Path(workspace_root).absolute()
            with _directory(target.parent) as parent:
                os.mkdir(target.name, mode=0o700, dir_fd=parent)
                with _directory(target) as root:
                    for path in sorted(directories, key=lambda p: (p.count('/'), p)):
                        with _directory(target / Path(path).parent) as fd:
                            os.mkdir(Path(path).name, mode=0o700, dir_fd=fd)
                    for entry, snapshot in zip(files, snapshots):
                        path = Path(entry['path'])
                        with _directory(target / path.parent) as fd:
                            raw = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                          0o700 if entry['executable'] else 0o600, dir_fd=fd)
                            with os.fdopen(raw, 'wb') as output, snapshot.path.open('rb') as source:
                                while chunk := source.read(1024 * 1024):
                                    output.write(chunk)
                                output.flush()
                                os.fsync(output.fileno())
                            os.fsync(fd)
                    os.fsync(root)
                os.fsync(parent)
        except Exception as error:
            if isinstance(error, CheckpointError):
                raise
            raise CheckpointError('Checkpoint restoration failed; target must not be used') from error
        finally:
            for snapshot in snapshots:
                snapshot.close()
