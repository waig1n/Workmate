import asyncio
import json
import os
import sys

import pytest

from workmate_runtime.execution.resources import TaskResourceRegistry, ResourceScopeError
from workmate_runtime.sandbox.base import SandboxWorkspaceRequest
from workmate_runtime.transport.postgres import ExecutionOwner


@pytest.mark.asyncio
async def test_explicit_local_provider_files_mcp_memory_and_scope_fencing(tmp_path):
    from workmate_runtime.sandbox.local import LocalSandboxProvider
    from workmate_runtime.execution.controller import SandboxVerificationFailed
    provider = LocalSandboxProvider()
    await provider.probe()
    workspace = provider.create_workspace(SandboxWorkspaceRequest('local-test', tmp_path))
    await provider.initialize_workspace(workspace)
    registry = TaskResourceRegistry()
    owner = ExecutionOwner('session', 'task', 'worker', 1)
    registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
    backend = provider.bind_backend(workspace, owner, registry)
    result = await backend.exec_shell([sys.executable, '-c', 'import os;print(os.getuid());print(os.getcwd())'])
    assert result.exit_code == 0 and result.stdout.decode().splitlines() == [str(os.getuid()), str(tmp_path)]
    missing_cwd = await backend.exec_shell(['true'], cwd=str(tmp_path / 'does-not-exist'))
    assert missing_cwd.exit_code == 127
    path = str(tmp_path / 'output' / 'test.txt')
    await backend.write_file(path, b'local-data')
    assert await backend.read_file(path) == b'local-data'
    with pytest.raises(SandboxVerificationFailed):
        await provider.verify_task_isolation(backend)
    assert await workspace._gateway.health()
    assert not (tmp_path / '.mcp').exists()
    assert await registry.verify_preparation_cleanup(owner)
    with pytest.raises(ResourceScopeError):
        await backend.write_file(path, b'late')
    assert (tmp_path / 'output' / 'test.txt').read_bytes() == b'local-data'
    await provider.close_workspace(workspace)


@pytest.mark.asyncio
async def test_local_cancel_joins_and_terminates_process_group(tmp_path):
    from workmate_runtime.sandbox.local import LocalSandboxProvider
    provider = LocalSandboxProvider()
    workspace = provider.create_workspace(SandboxWorkspaceRequest('local-stop', tmp_path))
    await provider.initialize_workspace(workspace)
    registry = TaskResourceRegistry()
    owner = ExecutionOwner('session', 'task', 'worker', 1)
    registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
    backend = provider.bind_backend(workspace, owner, registry)
    started = tmp_path / 'started'
    task = asyncio.create_task(backend.exec_shell([sys.executable, '-c',
        'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(60)', str(started)]))
    for _ in range(100):
        if started.exists(): break
        await asyncio.sleep(.01)
    assert started.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await registry.verify_preparation_cleanup(owner)
    await provider.close_workspace(workspace)


@pytest.mark.asyncio
async def test_local_stdio_mcp_reconnects_after_task_cleanup_without_config_file(tmp_path):
    from workmate_runtime.sandbox.local import LocalSandboxProvider
    provider = LocalSandboxProvider()
    workspace = provider.create_workspace(SandboxWorkspaceRequest('local-mcp', tmp_path))
    await provider.initialize_workspace(workspace)
    fixture = tmp_path / 'server.py'
    fixture.write_text('''import os
from mcp.server.fastmcp import FastMCP
app=FastMCP('local-fixture')
@app.tool()
def identity() -> str: return str(os.getuid())
app.run(transport='stdio')
''')
    registry = TaskResourceRegistry(cleanup_timeout=20)
    for epoch in (1, 2):
        owner = ExecutionOwner('session', 'task'+str(epoch), 'worker', epoch)
        registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
        provider.bind_backend(workspace, owner, registry)
        client = workspace._gateway.make_client({'name': 'fixture', 'is_stateful': True,
            'mcp_config': {'type': 'stdio_mcp', 'command': sys.executable, 'args': [str(fixture)]}},
            agent_id='agent', session_id='session')
        await client.connect()
        for _ in range(2):
            chunk = await (await client.get_tool('identity'))()
            assert str(os.getuid()) in json.dumps(chunk.model_dump(mode='json'))
        assert await registry.verify_preparation_cleanup(owner)
    assert not (tmp_path / '.mcp').exists()
    await provider.close_workspace(workspace)


@pytest.mark.asyncio
async def test_local_subreaper_drains_setsid_daemon_before_receipt(tmp_path):
    from workmate_runtime.sandbox.local import LocalSandboxProvider
    provider = LocalSandboxProvider()
    workspace = provider.create_workspace(SandboxWorkspaceRequest('local-daemon', tmp_path))
    await provider.initialize_workspace(workspace)
    registry = TaskResourceRegistry()
    owner = ExecutionOwner('session', 'task', 'worker', 1)
    registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
    backend = provider.bind_backend(workspace, owner, registry)
    result = await backend.exec_shell([sys.executable, '-c', '''import os,subprocess,sys
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
    start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(p.pid)
'''])
    assert result.exit_code == 0
    pid = int(result.stdout)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert await registry.verify_preparation_cleanup(owner)
    await provider.close_workspace(workspace)


@pytest.mark.asyncio
async def test_local_cancelled_launch_without_receipt_retains_unclean_workspace(tmp_path, monkeypatch):
    import workmate_runtime.sandbox.local as local
    from workmate_runtime.sandbox.base import WorkspaceBindingError
    # Model a supervisor crash before it can generate a receipt, without
    # creating any descendant that could be left behind by this test.
    broken = tmp_path / 'broken_supervisor.py'
    broken.write_text('import time;time.sleep(60)')
    monkeypatch.setattr(local, '_SUPERVISOR', str(broken))
    provider = local.LocalSandboxProvider()
    workspace = provider.create_workspace(SandboxWorkspaceRequest('local-crash', tmp_path))
    await provider.initialize_workspace(workspace)
    registry = TaskResourceRegistry()
    owner = ExecutionOwner('session', 'task', 'worker', 1)
    registry.open_scope(owner, fresh_claim=True, all_resource_paths_tracked=True)
    backend = provider.bind_backend(workspace, owner, registry)
    created, release = asyncio.Event(), asyncio.Event()
    original = local._LocalProcessBackend.start_process
    processes = []
    async def delayed(self, command, **kwargs):
        process = await original(self, command, **kwargs)
        processes.append(process)
        created.set()
        await release.wait()
        return process
    monkeypatch.setattr(local._LocalProcessBackend, 'start_process', delayed)
    task = asyncio.create_task(backend.exec_shell(['true']))
    await created.wait()
    task.cancel()
    processes[0].kill()
    await processes[0].wait()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await registry.verify_preparation_cleanup(owner)
    with pytest.raises(WorkspaceBindingError, match='retained'):
        await provider.close_workspace(workspace)
    assert os.path.isdir(workspace._local_receipts.name)
    # All test children are already joined; discard only this test's retained
    # metadata rather than pretending that provider cleanup was successful.
    await workspace._gateway.aclose()
    workspace._local_receipts.cleanup()


@pytest.mark.asyncio
async def test_local_kernel_pidfd_failure_is_a_startup_error(monkeypatch):
    import errno
    from workmate_runtime.sandbox.local import LocalSandboxProvider
    from workmate_runtime.sandbox.base import WorkspaceBindingError
    def unsupported(*args):
        raise OSError(errno.ENOSYS, 'pidfd unavailable')
    monkeypatch.setattr(os, 'pidfd_open', unsupported)
    with pytest.raises(WorkspaceBindingError, match='kernel pidfd'):
        await LocalSandboxProvider().probe()


@pytest.mark.asyncio
async def test_local_supervisor_never_reuses_a_previous_clean_receipt(tmp_path):
    from workmate_runtime.sandbox.local import _SUPERVISOR
    receipt = tmp_path / 'receipt'
    receipt.write_text('clean')
    started = tmp_path / 'started'
    # The controlled child writes a marker then exits; the supervisor can
    # normally complete too quickly to kill deterministically. Signal-stop
    # the supervisor from its child before writing that marker instead.
    process = await asyncio.create_subprocess_exec(sys.executable, '-I', _SUPERVISOR, str(receipt),
        sys.executable, '-I', '-c',
        'import os,pathlib,signal,sys;os.kill(os.getppid(),signal.SIGSTOP);pathlib.Path(sys.argv[1]).write_text("ready")',
        str(started), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        for _ in range(100):
            if started.exists(): break
            await asyncio.sleep(.01)
        assert started.exists() and not receipt.exists()
        # Resume and terminate gracefully so its adopted child is reaped;
        # the stale clean marker was already proven absent before this point.
        process.send_signal(__import__('signal').SIGCONT)
        await asyncio.wait_for(process.wait(), 5)
        assert receipt.read_text() == 'clean'
    finally:
        if process.returncode is None:
            process.send_signal(__import__('signal').SIGCONT)
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
