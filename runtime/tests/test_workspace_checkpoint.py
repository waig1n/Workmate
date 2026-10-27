import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from workmate_runtime.delivery.workspace_checkpoint import WorkspaceCheckpoint, CheckpointError


class Store:
    def __init__(self, tmp):
        self.tmp, self.objects = tmp, {}

    def publish_file(self, key, source, digest, size):
        self.objects[key] = source.read()
        assert hashlib.sha256(self.objects[key]).hexdigest() == digest
        assert len(self.objects[key]) == size

    def snapshot(self, key, digest, size):
        path = self.tmp / ('snapshot-' + str(len(list(self.tmp.glob('snapshot-*')))))
        path.write_bytes(self.objects[key])
        return SimpleNamespace(path=path, close=lambda: path.unlink(missing_ok=True))


def test_roundtrip_all_scopes_excludes_runtime_state(tmp_path):
    source = tmp_path / 'old'
    for name in ('input', 'working', 'output', '.workmate'):
        (source / name).mkdir(parents=True)
        (source / name / '中文').write_bytes(b'content')
    (source / 'working' / 'empty').mkdir()
    (source / 'working' / '中文').chmod(0o700)
    checkpoint = WorkspaceCheckpoint(Store(tmp_path), max_files=10, max_bytes=100)
    manifest = checkpoint.capture(source)
    assert len(manifest['files']) == 3
    target = tmp_path / 'new'
    checkpoint.restore(manifest, target)
    assert not (target / '.workmate').exists()
    assert (target / 'working' / 'empty').is_dir()
    assert (target / 'working' / '中文').read_bytes() == b'content'
    assert (target / 'working' / '中文').stat().st_mode & 0o100
    with pytest.raises(CheckpointError):
        checkpoint.restore(manifest, target)


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'size', 'count'])
def test_capture_fails_closed(tmp_path, kind):
    source = tmp_path / 'old'
    (source / 'working').mkdir(parents=True)
    path = source / 'working' / 'file'
    if kind == 'symlink':
        path.symlink_to('/etc/passwd')
    elif kind == 'fifo':
        import os
        os.mkfifo(path)
    else:
        path.write_bytes(b'big')
        if kind == 'count':
            (source / 'working' / 'other').write_bytes(b'x')
    with pytest.raises(CheckpointError):
        WorkspaceCheckpoint(Store(tmp_path), max_files=1, max_bytes=2 if kind == 'size' else 10).capture(source)


@pytest.mark.parametrize('change', ['path', 'key', 'size', 'digest', 'duplicate', 'corrupt', 'prefix'])
def test_invalid_restore_leaves_no_target(tmp_path, change):
    source = tmp_path / 'old'
    (source / 'working').mkdir(parents=True)
    (source / 'working' / 'a').write_bytes(b'a')
    store = Store(tmp_path)
    checkpoint = WorkspaceCheckpoint(store, max_files=4, max_bytes=10)
    manifest = checkpoint.capture(source)
    file = manifest['files'][0]
    if change == 'path': file['path'] = 'working/../../escape'
    if change == 'key': file['storage_key'] = '.inputs/something'
    if change == 'size': file['size'] = 11
    if change == 'digest': file['sha256'] = 'x'
    if change == 'duplicate': manifest['files'].append(dict(file))
    if change == 'corrupt': store.objects[file['storage_key']] = b'b'
    if change == 'prefix': manifest['directories'].append('working/a/child')
    target = tmp_path / 'new'
    with pytest.raises(CheckpointError): checkpoint.restore(manifest, target)
    assert not target.exists()
    assert not list(tmp_path.glob('snapshot-*'))


def test_s3_publish_file_allows_immutable_workspace_objects(tmp_path):
    import io
    from workmate_product.artifact_storage import S3ArtifactStore
    class Client:
        def put_object(self, **kw): self.content = kw['Body'].read()
        def get_object(self, **kw): return {'Body': io.BytesIO(self.content), 'ContentLength': len(self.content)}
    store = S3ArtifactStore(Client(), 'private')
    digest = hashlib.sha256(b'abc').hexdigest()
    store.publish_file(f'.objects/sha256/{digest[:2]}/{digest}', io.BytesIO(b'abc'), digest, 3)
    with pytest.raises(ValueError):
        store.publish_file(f'.objects/sha256/{digest[:2]}/{digest}', io.BytesIO(b'bad'), digest, 3)


@pytest.mark.parametrize('values', [(0, 10), (1, 0), (True, 2)])
def test_reject_invalid_limits(tmp_path, values):
    with pytest.raises(ValueError):
        WorkspaceCheckpoint(Store(tmp_path), max_files=values[0], max_bytes=values[1])


def test_per_file_bound_and_late_source_mutation(tmp_path):
    source = tmp_path / 'old'
    (source / 'working').mkdir(parents=True)
    file = source / 'working' / 'a'
    file.write_bytes(b'abc')
    with pytest.raises(CheckpointError):
        WorkspaceCheckpoint(Store(tmp_path), max_files=5, max_bytes=20, max_file_bytes=2).capture(source)
    class MutatingStore(Store):
        def publish_file(self, *args):
            super().publish_file(*args)
            file.write_bytes(b'changed')
    with pytest.raises(CheckpointError):
        WorkspaceCheckpoint(MutatingStore(tmp_path), max_files=5, max_bytes=20).capture(source)


@pytest.mark.parametrize('manifest', [None, {}, {'version': True, 'files': [], 'directories': []}])
def test_malformed_manifest(tmp_path, manifest):
    with pytest.raises(CheckpointError):
        WorkspaceCheckpoint(Store(tmp_path), max_files=1, max_bytes=1).restore(manifest, tmp_path/'new')


def test_missing_object_and_symlink_target_parent(tmp_path):
    source = tmp_path / 'old'
    (source / 'input').mkdir(parents=True)
    (source / 'input' / 'file').write_bytes(b'a')
    store = Store(tmp_path)
    cp = WorkspaceCheckpoint(store, max_files=2, max_bytes=4)
    manifest = cp.capture(source)
    (tmp_path/'link').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CheckpointError): cp.restore(manifest, tmp_path/'link'/'new')
    store.objects.clear()
    with pytest.raises(CheckpointError): cp.restore(manifest, tmp_path/'new')
    assert not (tmp_path/'new').exists()


@pytest.mark.parametrize('phase', ['before', 'opened', 'after_read', 'published'])
def test_capture_rejects_hardlinks_and_new_links_during_capture(tmp_path, monkeypatch, phase):
    import os
    source = tmp_path / 'old'
    (source / 'working').mkdir(parents=True)
    file = source / 'working' / 'file'
    file.write_bytes(b'data')
    linked = tmp_path / 'outside-link'
    store = Store(tmp_path)
    def link():
        if not linked.exists(): os.link(file, linked)
    if phase == 'before': link()
    if phase == 'opened':
        original = os.open
        def opened(path, flags, *args, **kwargs):
            result = original(path, flags, *args, **kwargs)
            if path == 'file': link()
            return result
        monkeypatch.setattr(os, 'open', opened)
    if phase == 'after_read':
        original = os.read
        def read(fd, size):
            result = original(fd, size)
            link()
            return result
        monkeypatch.setattr(os, 'read', read)
    if phase == 'published':
        original = store.publish_file
        def publish(*args):
            original(*args)
            link()
        store.publish_file = publish
    with pytest.raises(CheckpointError):
        WorkspaceCheckpoint(store, max_files=4, max_bytes=100).capture(source)
