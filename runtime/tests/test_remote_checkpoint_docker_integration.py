"""Opt-in real Docker file recovery; local immutable object fixture, not S3.

Uses only an existing WORKMATE_TEST_SANDBOX_IMAGE, two random labelled volumes and
containers. No builds, pulls, remote hosts or business resources are touched.
The original volume is deleted before restoration, proving no source-file read.
"""
import hashlib
import os
import uuid

import pytest

from workmate_product.config.sandbox import parse_sandbox_settings
from workmate_product.application.artifact_objects import snapshot_artifact
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.sandbox.base import SandboxWorkspaceRequest
from workmate_runtime.sandbox.remote import RemoteSandboxProvider
from workmate_runtime.transport.postgres import ExecutionOwner


class LocalImmutableObjects:
    """Exact production snapshot verifier over private fixture object files."""
    def __init__(self, root):
        self.root = root

    def publish_file(self, key, source, digest, size):
        source.seek(0)
        content = source.read()
        assert len(content) == size and hashlib.sha256(content).hexdigest() == digest
        assert key == f'.objects/sha256/{digest[:2]}/{digest}'
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            assert path.read_bytes() == content
        else:
            with path.open('xb') as output:
                output.write(content)

    def snapshot(self, key, digest, size):
        return snapshot_artifact(self.root, key, digest, size)


@pytest.mark.skipif(not os.environ.get('WORKMATE_TEST_SANDBOX_IMAGE'),
                    reason='Opt in with an existing local sandbox image')
@pytest.mark.asyncio
async def test_real_docker_checkpoint_new_volume_and_task_uid_restore(monkeypatch, tmp_path):
    import aiodocker
    from agentscope.workspace import DockerWorkspace
    from workmate_runtime.delivery.remote_workspace_checkpoint import RemoteWorkspaceCheckpoint
    from workmate_runtime.delivery.workspace_checkpoint import CheckpointError

    image = os.environ['WORKMATE_TEST_SANDBOX_IMAGE']
    test_owner = 'checkpoint-' + uuid.uuid4().hex
    workspace_ids = [test_owner + '-source', test_owner + '-target']
    workspaces = []
    provider = RemoteSandboxProvider(parse_sandbox_settings({'provider': 'docker'}))
    registry = TaskResourceRegistry(cleanup_timeout=30)
    owners = []
    store = LocalImmutableObjects(tmp_path / 'objects')

    async def use_existing_image(workspace):
        workspace._image_tag = image
    monkeypatch.setattr(DockerWorkspace, '_build_or_reuse_image', use_existing_image)

    async def remove_owned(docker, workspace_id):
        name = 'workmate-' + workspace_id
        try:
            container = await docker.containers.get(name)
            info = await container.show()
        except aiodocker.exceptions.DockerError as error:
            if error.status != 404:
                raise
        else:
            assert info['Config']['Labels']['workmate.workspace'] == workspace_id
            await container.delete(force=True)
        try:
            volume = await docker.volumes.get(name)
        except aiodocker.exceptions.DockerError as error:
            if error.status != 404:
                raise
        else:
            info = await volume.show()
            assert info['Labels']['workmate.test.owner'] == test_owner
            assert info['Labels']['workmate.test.workspace'] == workspace_id
            await volume.delete()

    async def create(index):
        workspace_id = workspace_ids[index]
        # Labels are immutable: precreate exactly our fresh random volume.
        async with aiodocker.Docker() as docker:
            volume = await docker.volumes.create({'Name': 'workmate-' + workspace_id,
                'Labels': {'workmate.test.owner': test_owner, 'workmate.test.workspace': workspace_id}})
            info = await volume.show()
            assert info['Labels']['workmate.test.owner'] == test_owner
            assert info['Labels']['workmate.test.workspace'] == workspace_id
        workspace = provider.create_workspace(SandboxWorkspaceRequest(workspace_id))
        workspaces.append(workspace)
        await provider.initialize_workspace(workspace)
        return workspace

    async def bind(workspace, suffix):
        owner = ExecutionOwner(workspace.workspace_id, 'task-' + suffix, test_owner, 1)
        owners.append(owner)
        registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
        backend = provider.bind_backend(workspace, owner, registry)
        evidence = await provider.verify_task_isolation(backend)
        assert evidence['evidence']['uids'] == [62000] * 3
        return owner, backend

    def checkpoint(workspace):
        return RemoteWorkspaceCheckpoint(workspace._workmate_control, workspace.workdir, store,
            provider='docker', max_files=20, max_bytes=1024 * 1024, max_file_bytes=512 * 1024)

    try:
        await provider.probe()
        source = await create(0)
        source_owner, backend = await bind(source, 'source')
        expected = {'input/request.txt': '原始输入'.encode(),
                    'working/binary.dat': bytes(range(256)) * 513,
                    'working/run.sh': b'#!/bin/sh\nprintf recovered-executable',
                    'output/report.txt': '已交付成果'.encode()}
        for path, content in expected.items():
            await backend.write_file(source.workdir + '/' + path, content)
        result = await backend.exec_shell(['chmod', '700', source.workdir + '/working/run.sh'])
        assert result.exit_code == 0
        assert await registry.verify_preparation_cleanup(source_owner)
        assert await backend.quiescent()
        manifest = await checkpoint(source).capture()
        assert {item['path'] for item in manifest['files']} == set(expected)
        assert next(item for item in manifest['files'] if item['path'] == 'working/run.sh')['executable']
        await provider.close_workspace(source)
        async with aiodocker.Docker() as docker:
            await remove_owned(docker, workspace_ids[0])
            with pytest.raises(aiodocker.exceptions.DockerError) as missing:
                await docker.volumes.get('workmate-' + workspace_ids[0])
            assert missing.value.status == 404

        target = await create(1)
        target_owner, target_backend = await bind(target, 'sentinel')
        await target_backend.write_file(target.workdir + '/working/keep.txt', b'do-not-overwrite')
        assert await registry.verify_preparation_cleanup(target_owner)
        assert await target_backend.quiescent()
        with pytest.raises(CheckpointError):
            await checkpoint(target).restore(manifest)
        result = await target._workmate_control.exec_shell([target._workmate_control.python, '-I', '-c',
            'import pathlib,sys;p=pathlib.Path(sys.argv[1]);assert p.read_bytes()==b"do-not-overwrite";p.unlink()',
            target.workdir + '/working/keep.txt'])
        assert result.exit_code == 0

        entry = manifest['files'][0]
        object_path = store.root / entry['storage_key']
        original = object_path.read_bytes()
        object_path.write_bytes(b'corrupt')
        with pytest.raises((CheckpointError, RuntimeError)):
            await checkpoint(target).restore(manifest)
        # Failed source verification must not partially install user files.
        check = await target._workmate_control.exec_shell([target._workmate_control.python, '-I', '-c',
            'import os,sys;assert all(not os.listdir(os.path.join(sys.argv[1],n)) for n in ("input","working","output"))',
            target.workdir])
        assert check.exit_code == 0
        object_path.write_bytes(original)
        await checkpoint(target).restore(manifest)
        with pytest.raises(CheckpointError):
            await checkpoint(target).restore(manifest)

        restored_owner, restored_backend = await bind(target, 'restored')
        for path, content in expected.items():
            assert await restored_backend.read_file(target.workdir + '/' + path) == content
        executable = await restored_backend.exec_shell([target.workdir + '/working/run.sh'])
        assert executable.exit_code == 0 and executable.stdout == b'recovered-executable'
        assert await registry.verify_preparation_cleanup(restored_owner)
        assert await restored_backend.quiescent()
        await provider.close_workspace(target)
    finally:
        # Continue cleaning the other exact resource if one cleanup step fails.
        errors = []
        for owner in owners:
            try:
                await registry.verify_preparation_cleanup(owner)
            except Exception as error:
                errors.append(error)
        for workspace in workspaces:
            client = getattr(workspace, '_client', None)
            if client is not None:
                try:
                    await client.close()
                except Exception as error:
                    errors.append(error)
        async with aiodocker.Docker() as docker:
            for workspace_id in workspace_ids:
                try:
                    await remove_owned(docker, workspace_id)
                except Exception as error:
                    errors.append(error)
        if errors:
            raise RuntimeError(f'Test-owned Docker resource cleanup failed: {errors!r}')
