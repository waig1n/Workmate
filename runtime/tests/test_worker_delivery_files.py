"""Real filesystem delivery capture, no live Workspace path in published objects."""
import hashlib
import os
from pathlib import Path

import pytest

from workmate_runtime.delivery.files import DeliveryFiles, DeliveryFileError


@pytest.fixture
def files(tmp_path):
    workspace, store = tmp_path/'workspace', tmp_path/'objects'
    (workspace/'output').mkdir(parents=True)
    return workspace, store, DeliveryFiles(workspace,store,max_file_bytes=16,max_files=3)


def manifest(workspace, call_id, names):
    import json
    (workspace/'.workmate').mkdir(exist_ok=True)
    (workspace/'.workmate/presented_files.json').write_text(json.dumps({'version':1,'call_id':call_id,'files':names}))


def test_capture_immutable_versions_and_nested_output(files):
    root,store,delivery = files
    baseline = delivery.baseline()
    (root/'output/sub').mkdir()
    target = root/'output/sub/a.txt'
    target.write_bytes(b'first')
    capture = delivery.capture(baseline)
    item, = capture['files']
    assert item['name']=='sub/a.txt' and item['change_type']=='created'
    assert item['sha256']==hashlib.sha256(b'first').hexdigest()
    target.write_bytes(b'next')
    assert (store/item['storage_key']).read_bytes()==b'first'
    assert str(root) not in str(capture)


def test_explicit_can_present_unchanged_file(files):
    root,store,delivery = files
    (root/'output/a').write_bytes(b'same')
    baseline=delivery.baseline()
    manifest(root,'new',['output/a'])
    capture=delivery.capture(baseline)
    assert capture['source']=='explicit'
    assert capture['files'][0]['change_type']=='presented'


@pytest.mark.parametrize('rewrite', [False, True])
def test_snapshot_ignores_timestamp_change_with_identical_content(files, rewrite):
    root, _, delivery = files
    target = root/'output/a'
    target.write_bytes(b'same')
    baseline = delivery.baseline()
    previous = target.stat()
    if rewrite:
        target.write_bytes(b'same')
    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    assert delivery.capture(baseline)['files'] == []


def test_explicit_same_content_rewrite_is_presented(files):
    root, _, delivery = files
    target = root/'output/a'
    target.write_bytes(b'same')
    baseline = delivery.baseline()
    previous = target.stat()
    target.write_bytes(b'same')
    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    manifest(root, 'new', ['output/a'])
    captured = delivery.capture(baseline)
    item, = captured['files']
    assert captured['source'] == 'explicit'
    assert item['change_type'] == 'presented'


def test_snapshot_detects_same_size_changed_content_with_original_timestamp(files):
    root, _, delivery = files
    target = root/'output/a'
    target.write_bytes(b'first')
    baseline = delivery.baseline()
    previous = target.stat()
    target.write_bytes(b'other')
    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    item, = delivery.capture(baseline)['files']
    assert item['change_type'] == 'modified'
    assert item['sha256'] == hashlib.sha256(b'other').hexdigest()


def test_old_manifest_does_not_leak_into_next_task(files):
    root,store,delivery=files
    (root/'output/a').write_bytes(b'same')
    manifest(root,'old',['a'])
    baseline=delivery.baseline()
    assert delivery.capture(baseline)['files']==[]


def test_strict_mode_without_declaration_delivers_nothing(files):
    root,store,delivery=files
    baseline=delivery.baseline()
    (root/'output/a').write_bytes(b'new')
    captured=delivery.capture(baseline,strict=True)
    assert captured['source']=='strict_no_declaration' and captured['files']==[]


def test_snapshot_mode_ignores_explicit_selection(files):
    root,store,delivery=files
    baseline=delivery.baseline()
    for name in ['a','b']:
        (root/'output'/name).write_bytes(name.encode())
    manifest(root,'new',['a'])
    assert len(delivery.capture(baseline,mode='snapshot_fallback')['files'])==2


def test_limits_are_reported_not_silent(files):
    root,store,delivery=files
    baseline=delivery.baseline()
    for name in ['a','b','c','d']:
        (root/'output'/name).write_bytes(b'x'* (17 if name=='a' else 2))
    capture=delivery.capture(baseline)
    assert len(capture['files'])==2
    assert len(capture['warnings'])>=2


@pytest.mark.parametrize('name',['../outside','/etc/passwd','a/../../b','a\\b'])
def test_manifest_unsafe_names_are_not_read(files,name):
    root,store,delivery=files
    baseline=delivery.baseline()
    manifest(root,'new',[name])
    assert delivery.capture(baseline,strict=True)['files']==[]


def test_output_symlink_is_rejected(files,tmp_path):
    root,store,delivery=files
    baseline=delivery.baseline()
    outside=tmp_path/'secret'
    outside.write_bytes(b'secret')
    (root/'output/leak').symlink_to(outside)
    with pytest.raises(DeliveryFileError):
        delivery.capture(baseline)


def test_shared_object_symlink_cannot_overwrite_external_file(files,tmp_path):
    root,store,delivery=files
    baseline=delivery.baseline()
    (root/'output/a').write_bytes(b'new')
    outside=tmp_path/'outside'
    outside.mkdir()
    store.mkdir()
    (store/'.objects').symlink_to(outside,target_is_directory=True)
    with pytest.raises(DeliveryFileError):
        delivery.capture(baseline)
    assert list(outside.iterdir())==[]


def test_corrupt_existing_object_fails_without_replacement(files):
    root,store,delivery=files
    baseline=delivery.baseline()
    (root/'output/a').write_bytes(b'one')
    capture=delivery.capture(baseline)
    object_path=store/capture['files'][0]['storage_key']
    object_path.write_bytes(b'bad')
    with pytest.raises(DeliveryFileError):
        delivery.capture(baseline)
    assert object_path.read_bytes()==b'bad'


def test_unknown_baseline_version_rejected(files):
    root,store,delivery=files
    baseline=delivery.baseline()
    baseline['version']=2
    with pytest.raises(DeliveryFileError):
        delivery.capture(baseline)


def test_empty_directories_count_toward_inventory_limit(files):
    root,store,delivery=files
    for index in range(10001):
        (root/'output'/str(index)).mkdir()
    with pytest.raises(DeliveryFileError,match='inventory'):
        delivery.baseline()


def test_object_publication_precedes_capture_without_local_objects(tmp_path):
    from workmate_runtime.delivery.files import DeliveryFiles, verify_capture_objects
    from workmate_product.application.artifact_objects import ArtifactSnapshot, ArtifactObjectUnavailable
    workspace = tmp_path/'workspace'
    (workspace/'output').mkdir(parents=True)
    stored = {}
    class Store:
        def publish(self,key,content,digest):
            stored[key] = content
        def snapshot(self,key,digest,size):
            if key not in stored:
                raise ArtifactObjectUnavailable('missing')
            path = tmp_path/'snapshot'
            path.write_bytes(stored[key])
            return ArtifactSnapshot(path)
    store = Store()
    files = DeliveryFiles(workspace,tmp_path/'unmounted',max_file_bytes=100,max_files=2,object_store=store)
    baseline = files.baseline()
    (workspace/'output'/'result.txt').write_bytes(b'delivered')
    capture = files.capture(baseline)
    assert stored[capture['files'][0]['storage_key']] == b'delivered'
    assert not (tmp_path/'unmounted').exists()
    assert verify_capture_objects(tmp_path/'unmounted',capture,store)
    stored.clear()
    with pytest.raises(ArtifactObjectUnavailable):
        verify_capture_objects(tmp_path/'unmounted',capture,store)
