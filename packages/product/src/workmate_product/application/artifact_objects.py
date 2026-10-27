"""Read published objects into private verified download snapshots.

The caller supplies an authorized Product Artifact descriptor. No Workspace or
execution SDK is consulted. This local adapter requires the published object
mount; the S3 adapter supplies the same verified private snapshot contract.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile


class ArtifactObjectUnavailable(RuntimeError):
    pass


@dataclass
class ArtifactSnapshot:
    path: Path

    def close(self):
        self.path.unlink(missing_ok=True)


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def snapshot_artifact(root, key, digest, size):
    """Validate and copy without following links or buffering whole files in RAM."""
    if (not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
            or key != f'.objects/sha256/{digest[:2]}/{digest}'
            or type(size) is not int or size < 0):
        raise ValueError('Invalid Artifact object descriptor')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    source = None
    snapshot = None
    try:
        for part in (Path(root).absolute() / key).parts[1:-1]:
            if part in {'.', '..'}:
                raise ArtifactObjectUnavailable('Artifact object unavailable')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        source = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise ArtifactObjectUnavailable('Artifact object unavailable')
        with tempfile.NamedTemporaryFile(prefix='workmate-artifact-', delete=False) as output:
            snapshot = ArtifactSnapshot(Path(output.name))
            checksum = hashlib.sha256()
            count = 0
            while chunk := os.read(source, min(1024 * 1024, size + 1 - count)):
                count += len(chunk)
                if count > size:
                    raise ArtifactObjectUnavailable('Artifact object unavailable')
                checksum.update(chunk)
                output.write(chunk)
            if (count != size or checksum.hexdigest() != digest
                    or _fingerprint(before) != _fingerprint(os.fstat(source))):
                raise ArtifactObjectUnavailable('Artifact object unavailable')
        return snapshot
    except BaseException as error:
        if snapshot is not None:
            snapshot.close()
        if isinstance(error, OSError):
            raise ArtifactObjectUnavailable('Artifact object unavailable') from error
        raise
    finally:
        if source is not None:
            os.close(source)
        os.close(directory)
