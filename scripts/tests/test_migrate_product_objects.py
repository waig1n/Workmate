import hashlib
import importlib.util
from pathlib import Path

import pytest
from workmate_product.application.artifact_objects import ArtifactSnapshot

spec = importlib.util.spec_from_file_location('migrate_product_objects',Path(__file__).parents[1]/'migrate_product_objects.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_apply_then_check_preserves_source_and_does_not_republish(tmp_path):
    digest = hashlib.sha256(b'report').hexdigest()
    root = tmp_path/'volume'
    artifact = root/f'.objects/sha256/{digest[:2]}/{digest}'
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'report')
    attachment = root/'.inputs/conversation/task/input.txt'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'input')
    class Store:
        objects = {}
        publishes = 0
        def publish(self,key,content,digest):
            self.publishes += 1
            self.objects[key] = content
        def snapshot(self,key,digest,size):
            content = self.objects[key]
            assert hashlib.sha256(content).hexdigest() == digest and len(content) == size
            path = tmp_path/'snapshot'
            path.write_bytes(content)
            return ArtifactSnapshot(path)
    store = Store()
    assert module.migrate(root,store,apply=True)['verified_objects'] == 2
    assert module.migrate(root,store)['verified_objects'] == 2
    assert store.publishes == 2
    assert artifact.read_bytes() == b'report' and attachment.read_bytes() == b'input'


def test_migration_rejects_symlink_namespace(tmp_path):
    root = tmp_path/'volume'
    root.mkdir()
    (root/'.objects').symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        module.migrate(root,object())
