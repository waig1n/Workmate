"""Local provider exercises the same product tools and immutable delivery path."""
from pathlib import Path

from agentscope.message import ToolResultState
from agentscope.state import AgentState
from agentscope.tool import Glob

from workmate_runtime.assembly.posix_workspace_ownership import PosixWorkspaceOwnership
from workmate_runtime.assembly.workspaces import WorkerWorkspaceManager, WorkspaceIdentity
from workmate_runtime.delivery.files import DeliveryFiles
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.sandbox.local import LocalSandboxProvider
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.workspaces import WorkmateSandboxRead, WorkmateSandboxWrite


async def test_local_manager_tools_delivery_and_reopen(tmp_path):
    registry = TaskResourceRegistry()
    identity = WorkspaceIdentity('user', 'agent', 'conversation', 'session', 'node')
    owner = ExecutionOwner('session', 'task', 'worker', 1)

    def manager():
        return WorkerWorkspaceManager(tmp_path / 'workspaces', storage_node_id='node',
            resource_registry=registry, ownership_provider=PosixWorkspaceOwnership(),
            provider=LocalSandboxProvider())

    workspace_manager = manager()
    binding = await workspace_manager.bind_task(identity, owner, fresh_claim=True,
        all_resource_paths_tracked=True, read_model_input_types=['text/plain'])
    root = Path(binding.workspace.host_workdir)
    delivery = DeliveryFiles(root, tmp_path / 'objects', max_file_bytes=1024, max_files=10)
    baseline = delivery.baseline()
    read = next(tool for tool in binding.tools if isinstance(tool, WorkmateSandboxRead))
    write = next(tool for tool in binding.tools if isinstance(tool, WorkmateSandboxWrite))
    glob = next(tool for tool in binding.tools if isinstance(tool, Glob))
    target = str(root / 'output' / 'local.txt')
    state = AgentState()
    try:
        assert (await write.call(target, 'original', state)).state != ToolResultState.ERROR
        assert (await write.call(target, 'replacement', state)).state == ToolResultState.ERROR
        assert (await read.call(target, _agent_state=state)).state != ToolResultState.ERROR
        assert (await write.call(target, 'replacement', state)).state != ToolResultState.ERROR
        matches = await glob.call('*.txt', path=str(root / 'output'))
        assert matches.state != ToolResultState.ERROR
        assert 'local.txt' in str(matches.content)
        assert await registry.verify_preparation_cleanup(owner)
        captured = delivery.capture(baseline)
        item, = captured['files']
        assert (tmp_path / 'objects' / item['storage_key']).read_bytes() == b'replacement'
    finally:
        await registry.verify_preparation_cleanup(owner)
        await workspace_manager.close()

    reopened = manager()
    try:
        workspace = await reopened.get_workspace(identity)
        assert workspace.host_workdir == str(root)
        assert Path(target).read_bytes() == b'replacement'
    finally:
        await reopened.close()
