"""Rebuildable, Worker-private Skill cache; never mounted into Task sandboxes.

Readers and publishers share a per-digest process lock. A hit revalidates both
archive and complete extracted tree; existence is never evidence of integrity.
The caller must resolve current Product metadata before consulting this cache.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from workmate_product.application.skill_artifacts import (
    MAX_ARCHIVE_BYTES, SkillArtifactError, parse_skill_archive,
)


class SkillCacheError(ValueError):
    """The private cache boundary or source package is invalid."""


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise SkillCacheError('Invalid pinned Skill digest')
    return value


def _marker(artifact):
    return json.dumps({'digest': artifact.digest, 'manifest': artifact.manifest},
                      ensure_ascii=False, separators=(',', ':')).encode()


def _read(parent, name, limit, *, mode=0o444):
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_size > limit
                or stat.S_IMODE(info.st_mode) != mode):
            raise SkillCacheError('Invalid Skill cache file')
        result = bytearray()
        while chunk := os.read(fd, min(65536, limit + 1 - len(result))):
            result.extend(chunk)
            if len(result) > limit:
                raise SkillCacheError('Skill cache file exceeds bound')
        if len(result) != info.st_size:
            raise SkillCacheError('Skill cache file changed during read')
        return bytes(result)
    finally:
        os.close(fd)


def _directory(parent, name):
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise SkillCacheError('Invalid private Skill cache directory')
    return fd


def _write(parent, name, content, mode=0o444):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 mode, dir_fd=parent)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise SkillCacheError('Incomplete Skill cache write')
            view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


class SkillDirectoryCache:
    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute() or '..' in self.root.parts:
            raise SkillCacheError('Skill cache requires an absolute private directory')

    @contextmanager
    def _locked(self, digest):
        _digest(digest)
        root = os.open('/', _DIRECTORY_FLAGS)
        lock = None
        try:
            for component in self.root.parts[1:]:
                try:
                    os.mkdir(component, 0o700, dir_fd=root)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=root)
                os.close(root)
                root = child
            info = os.fstat(root)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise SkillCacheError('Skill cache must be owned by Worker with mode 0700')
            lock = os.open(digest + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                           | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=root)
            info = os.fstat(lock)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise SkillCacheError('Invalid Skill cache lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield root
        except OSError as error:
            raise SkillCacheError('Skill cache filesystem operation failed') from error
        finally:
            if lock is not None:
                os.close(lock)
            os.close(root)

    def _read_entry(self, root, digest):
        entry = _directory(root, digest)
        try:
            if set(os.listdir(entry)) != {'archive.zip', 'manifest.json', 'tree'}:
                raise SkillCacheError('Incomplete Skill cache entry')
            raw = _read(entry, 'archive.zip', MAX_ARCHIVE_BYTES)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise SkillCacheError('Skill cache archive digest mismatch')
            artifact = parse_skill_archive(raw)
            marker = _marker(artifact)
            if _read(entry, 'manifest.json', len(marker)) != marker:
                raise SkillCacheError('Skill cache manifest mismatch')
            expected = {name: (digest, size, executable)
                        for name, digest, size, executable in artifact.manifest}
            directories = {''}
            for name in expected:
                parts = name.split('/')
                directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
            seen = set()

            def verify(parent, prefix=''):
                for name in os.listdir(parent):
                    relative = f'{prefix}/{name}' if prefix else name
                    if relative in expected:
                        checksum, size, executable = expected[relative]
                        content = _read(parent, name, size, mode=0o555 if executable else 0o444)
                        if len(content) != size or hashlib.sha256(content).hexdigest() != checksum:
                            raise SkillCacheError('Skill cache content mismatch')
                        seen.add(relative)
                    elif relative in directories:
                        child = _directory(parent, name)
                        try:
                            verify(child, relative)
                        finally:
                            os.close(child)
                    else:
                        raise SkillCacheError('Unexpected Skill cache member')

            tree = _directory(entry, 'tree')
            try:
                verify(tree)
            finally:
                os.close(tree)
            if seen != set(expected):
                raise SkillCacheError('Missing Skill cache member')
            return artifact
        finally:
            os.close(entry)

    def load(self, metadata):
        """Return only a metadata-matching complete hit, otherwise a rebuildable miss."""
        with self._locked(metadata.digest) as root:
            try:
                artifact = self._read_entry(root, metadata.digest)
                metadata.validate(artifact)
                return artifact
            except (OSError, ValueError, SkillArtifactError):
                return None

    def store(self, artifact):
        """Validate source bytes, then publish a fully fsynced entry under its digest."""
        _digest(artifact.digest)
        checked = parse_skill_archive(artifact.archive)
        if checked != artifact:
            raise SkillCacheError('Skill cache source does not match its package')
        with self._locked(artifact.digest) as root:
            try:
                existing = self._read_entry(root, artifact.digest)
                if existing == checked:
                    return existing
            except (OSError, ValueError, SkillArtifactError):
                pass
            temporary = '.partial-' + uuid.uuid4().hex
            os.mkdir(temporary, 0o700, dir_fd=root)
            entry = os.open(temporary, _DIRECTORY_FLAGS, dir_fd=root)
            try:
                _write(entry, 'archive.zip', artifact.archive)
                os.mkdir('tree', 0o700, dir_fd=entry)
                tree = _directory(entry, 'tree')
                try:
                    for name, _, _, executable in artifact.manifest:
                        parent = os.dup(tree)
                        try:
                            parts = name.split('/')
                            for part in parts[:-1]:
                                try:
                                    os.mkdir(part, 0o700, dir_fd=parent)
                                except FileExistsError:
                                    pass
                                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                                os.fsync(parent)
                                os.close(parent)
                                parent = child
                            _write(parent, parts[-1], artifact.files[name], 0o555 if executable else 0o444)
                            os.fsync(parent)
                        finally:
                            os.close(parent)
                    os.fsync(tree)
                finally:
                    os.close(tree)
                _write(entry, 'manifest.json', _marker(artifact))
                os.fsync(entry)
                # A corrupt/symlink entry is cache garbage, not an external tree.
                try:
                    info = os.stat(artifact.digest, dir_fd=root, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISDIR(info.st_mode):
                        shutil.rmtree(artifact.digest, dir_fd=root)
                    else:
                        os.unlink(artifact.digest, dir_fd=root)
                os.rename(temporary, artifact.digest, src_dir_fd=root, dst_dir_fd=root)
                os.fsync(root)
            finally:
                os.close(entry)
                try:
                    shutil.rmtree(temporary, dir_fd=root)
                except FileNotFoundError:
                    pass
        return checked
