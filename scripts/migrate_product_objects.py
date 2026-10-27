"""Copy a drained local Product volume into the configured private S3 bucket.

Run with Gateway and Workers stopped. Default --check performs no writes. Source
files are retained; changing backend is a separate operator action after every
object verifies. This checks source files, not database references: separately
verify that this volume contains every referenced Artifact/input before switching.
No database rows or keys are rewritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from workmate_product.artifact_storage import artifact_store_from_settings, validate_key


def migrate(root, store, *, apply=False):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Source must be an existing directory')
    count = 0
    for namespace in ('.objects', '.inputs'):
        base = root / namespace
        if base.is_symlink():
            raise ValueError('Source namespace cannot be a symlink')
        if not base.exists():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise ValueError('Source contains directory symlink')
            for name in files:
                path = Path(directory) / name
                key = validate_key(path.relative_to(root).as_posix())
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError('Source contains special file')
                    content = source.read()
                    after = os.fstat(source.fileno())
                    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                        raise ValueError('Source changed; stop Gateway and Workers before migrating')
                digest = hashlib.sha256(content).hexdigest()
                if namespace == '.objects' and key != f'.objects/sha256/{digest[:2]}/{digest}':
                    raise ValueError('Source immutable object is corrupt or unrecognized')
                if apply:
                    store.publish(key,content,digest)
                snapshot = store.snapshot(key,digest,len(content))
                snapshot.close()
                count += 1
    return {'verified_objects': count, 'mode': 'apply' if apply else 'check', 'source_retained': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()
    from app.config import get_settings
    store = artifact_store_from_settings(get_settings())
    if store is None:
        parser.error('Set ARTIFACT_STORAGE_BACKEND=s3 and ARTIFACT_S3_* for this command')
    print(json.dumps(migrate(args.source,store,apply=args.apply)))


if __name__ == '__main__':
    main()
