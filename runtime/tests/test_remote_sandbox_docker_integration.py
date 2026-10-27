"""Opt-in real Docker provider; only random test-owned containers/volumes touched.

Use an existing local image with the documented sandbox dependencies:
WORKMATE_TEST_SANDBOX_IMAGE=workmate-runtime:2.0 python -m pytest ...
No image build, image pull, business container or existing volume is modified.
"""
import json
import os
import uuid

import pytest

from workmate_product.config.sandbox import parse_sandbox_settings
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.sandbox.base import SandboxWorkspaceRequest
from workmate_runtime.sandbox.remote import RemoteSandboxProvider
from workmate_runtime.transport.postgres import ExecutionOwner


@pytest.mark.skipif(not os.environ.get('WORKMATE_TEST_SANDBOX_IMAGE'),
                    reason='Opt in with an existing local sandbox image')
@pytest.mark.asyncio
async def test_real_docker_workspace_uid_gateway_cleanup_and_persistent_files(monkeypatch, tmp_path):
    import aiodocker
    from agentscope.workspace import DockerWorkspace

    image = os.environ['WORKMATE_TEST_SANDBOX_IMAGE']
    workspace_id = 'test-sandbox-' + uuid.uuid4().hex
    resource_name = 'workmate-' + workspace_id
    async def use_existing_image(workspace):
        workspace._image_tag = image
    monkeypatch.setattr(DockerWorkspace, '_build_or_reuse_image', use_existing_image)
    provider = RemoteSandboxProvider(parse_sandbox_settings({'provider': 'docker'}))
    request = SandboxWorkspaceRequest(workspace_id)
    workspace = None
    try:
        await provider.probe()
        workspace = provider.create_workspace(request)
        await provider.initialize_workspace(workspace)
        control = workspace._workmate_control
        root = await control.exec_shell([control.python, '-I', '-c', 'import os;print(os.geteuid())'])
        assert root.exit_code == 0 and root.stdout.strip() == b'0'
        assert await workspace._gateway.health()

        from workmate_runtime.delivery.remote_files import RemoteDeliveryFiles
        delivery = RemoteDeliveryFiles(control, workspace.workdir, tmp_path / 'delivery',
            max_file_bytes=1024 * 1024, max_files=10)
        baseline = await delivery.baseline()

        registry = TaskResourceRegistry(cleanup_timeout=30)
        owner = ExecutionOwner('test-session', 'test-task-1', 'test-worker', 1)
        registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
        backend = provider.bind_backend(workspace, owner, registry)
        evidence = await provider.verify_task_isolation(backend)
        assert evidence['evidence']['uids'] == [62000] * 3
        assert evidence['evidence']['no_new_privs'] is True
        output = workspace.workdir + '/output/persistent.txt'
        await backend.write_file(output, b'persisted-through-restart')
        assert await backend.read_file(output) == b'persisted-through-restart'
        from agentscope.tool import Glob
        glob = Glob(backend=backend, glob_helper_path=workspace._workmate_glob_helper_path)
        matches = await glob.call('*.txt', workspace.workdir + '/output')
        assert 'persistent.txt' in json.dumps(matches.model_dump(mode='json'))
        denied = await backend.exec_shell(['ls', control.directory])
        assert denied.exit_code != 0
        assert await workspace._gateway.health()

        fixture = workspace.workdir + '/working/mcp_fixture.py'
        await backend.write_file(fixture, b'''import json,os
from mcp.server.fastmcp import FastMCP
server=FastMCP('sandbox-test')
@server.tool()
def identity() -> str:
    return json.dumps({'uid':os.getuid(),'pid':os.getpid(),'has_secret':bool(os.environ.get('WORKMATE_PROBE_SECRET'))})
server.run(transport='stdio')
''')
        async def check_stdio(task_backend):
            client = workspace._gateway.make_client({
                'name': 'fixture', 'is_stateful': True,
                'mcp_config': {'type': 'stdio_mcp', 'command': control.python,
                    'args': ['-I', fixture], 'env': {'WORKMATE_PROBE_SECRET': uuid.uuid4().hex}},
            }, agent_id='test-agent', session_id='test-session')
            await client.connect()
            tool = await client.get_tool('identity')
            chunk = await tool()
            payload = json.loads(chunk.model_dump(mode='json')['content'][0]['text'])
            assert payload['uid'] == 62001 and payload['has_secret'] is True
            # Agent UID 62000 cannot read the STDIO MCP UID's credentials.
            denied = await task_backend.exec_shell(['cat', f"/proc/{payload['pid']}/environ"])
            assert denied.exit_code != 0
            # Leave the actor connected: Task cleanup must close it through
            # the encrypted reset endpoint before proving UID quiescence.
        await check_stdio(backend)
        assert await registry.verify_preparation_cleanup(owner)
        assert await backend.quiescent()
        artifacts = await delivery.capture(baseline)
        assert [item['name'] for item in artifacts['files']] == ['persistent.txt']
        assert (tmp_path / 'delivery' / artifacts['files'][0]['storage_key']).read_bytes() == b'persisted-through-restart'

        # Same Workspace, next epoch: reset discarded the previous actor,
        # and the same MCP name must reconnect under the new launch token.
        next_owner = ExecutionOwner('test-session', 'test-task-2', 'test-worker', 2)
        registry.open_scope(next_owner, fresh_claim=True, all_resource_paths_tracked=True)
        next_backend = provider.bind_backend(workspace, next_owner, registry)
        await provider.verify_task_isolation(next_backend)
        await check_stdio(next_backend)
        assert await registry.verify_preparation_cleanup(next_owner)
        await provider.close_workspace(workspace)

        # Recreate only our exact workspace/container; Docker named volume
        # must preserve user output without carrying a stale active Task.
        workspace = provider.create_workspace(request)
        await provider.initialize_workspace(workspace)
        owner2 = ExecutionOwner('test-session', 'test-task-3', 'test-worker', 3)
        registry.open_scope(owner2, fresh_claim=True, all_resource_paths_tracked=True)
        backend2 = provider.bind_backend(workspace, owner2, registry)
        await provider.verify_task_isolation(backend2)
        assert await backend2.read_file(output) == b'persisted-through-restart'
        assert await registry.verify_preparation_cleanup(owner2)
        await provider.close_workspace(workspace)
        workspace = None
    finally:
        # Test cleanup must also work when initialization failed before a
        # valid Task binding exists. Verify our unique label before deletion.
        if workspace is not None and getattr(workspace, '_client', None) is not None:
            await workspace._client.close()
        async with aiodocker.Docker() as docker:
            try:
                container = await docker.containers.get(resource_name)
                info = await container.show()
            except aiodocker.exceptions.DockerError as error:
                if error.status != 404:
                    raise
            else:
                assert info['Config']['Labels']['workmate.workspace'] == workspace_id
                await container.delete(force=True)
            try:
                volume = await docker.volumes.get(resource_name)
            except aiodocker.exceptions.DockerError as error:
                if error.status != 404:
                    raise
            else:
                await volume.delete()
