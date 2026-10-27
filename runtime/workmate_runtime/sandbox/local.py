"""Explicitly unsafe host execution; process groups are not sandbox containment.

Local commands and MCPs have the Worker's OS identity and host access. A Linux
subreaper drains ordinary daemon descendants; absent cleanup receipts fail
closed. Hostile same-UID code remains outside this non-security guarantee.
This provider never produces an isolation proof.
"""
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile

import httpx
from agentscope.tool import BackendBase, ExecResult
from agentscope.workspace import LocalWorkspace
from agentscope.workspace._gateway_client import GatewayClient

from workmate_runtime.execution.controller import SandboxVerificationFailed
from workmate_runtime.execution.resources import TaskResourceBackendMixin
from workmate_runtime.mcp.gateway import MemoryGatewayClient
from workmate_runtime.mcp.server import GatewayState, build_app
from workmate_runtime.workspaces import ensure_convention_dirs
from .base import WorkspaceBindingError

_SUPERVISOR = str(Path(__file__).with_name('local_supervisor.py'))


class _LocalGateway(MemoryGatewayClient):
    """Serialize requests with Task shutdown and reject late sealed admissions."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._admission = asyncio.Lock()
        self._owner = self._registry = None

    def bind(self, owner, registry):
        self._owner, self._registry = owner, registry

    async def exec_request(self, method, path, **kwargs):
        owner, registry = self._owner, self._registry
        async with self._admission:
            if path != '/health':
                if owner is None or owner != self._owner:
                    raise WorkspaceBindingError('Local MCP has no matching Task owner')
                registry._scope(owner, launching=True)
            return await super().exec_request(method, path, **kwargs)

    async def close_actors(self, owner, state):
        async with self._admission:
            if owner != self._owner:
                raise WorkspaceBindingError('Local MCP cleanup belongs to another Task')
            await state.close()


async def _join(operation):
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result


class _LocalProcessBackend(BackendBase):
    async def start_process(self, command, **kwargs):
        cwd = kwargs.pop('cwd', None) or self.workdir
        kwargs.setdefault('stdin', asyncio.subprocess.PIPE)
        receipt = Path(self.workspace._local_receipts.name, secrets.token_hex(16))
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', _SUPERVISOR,
            str(receipt), '--cwd', cwd, '--', *command, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
        self._receipts[process] = receipt
        return process

    @staticmethod
    async def _terminate_process_tree(process, *, grace):
        # Ask the subreaper to drain adopted daemons before it exits. If it is
        # killed or cannot finish, its missing receipt prevents clean release.
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), max(grace, 3))
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def _execute(self, command, data, *, cwd=None, timeout=None):
        self._task_resources._scope(self._execution_owner, launching=True)
        try:
            process = await self.start_process(command, cwd=cwd or self.workdir)
        except (FileNotFoundError, NotADirectoryError) as error:
            return ExecResult(exit_code=127, stdout=b'', stderr=str(error).encode())
        owner, resource_id = self._task_resources._processes[process]
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError) as error:
            await _join(self._task_resources.cleanup_resource(owner, resource_id))
            if isinstance(error, asyncio.CancelledError):
                raise
            return ExecResult(exit_code=-1, stdout=b'', stderr=b'timed out')
        await self._task_resources.mark_finished(owner, resource_id)
        return ExecResult(exit_code=process.returncode or 0, stdout=stdout, stderr=stderr)

    async def exec_shell(self, command, *, cwd=None, timeout=None):
        return await self._execute(command, None, cwd=cwd, timeout=timeout)

    async def _exec_with_input(self, command, data):
        return await self._execute(command, data)

    async def read_file(self, path):
        result = await self.exec_shell([sys.executable, '-I', '-c',
            'import sys;sys.stdout.buffer.write(open(sys.argv[1],"rb").read())', path])
        if result.exit_code:
            raise FileNotFoundError(path)
        return result.stdout

    async def write_file(self, path, data):
        result = await self._exec_with_input([sys.executable, '-I', '-c',
            'import os,sys;os.makedirs(os.path.dirname(sys.argv[1]) or ".",exist_ok=True);'
            'open(sys.argv[1],"wb").write(sys.stdin.buffer.read())', path], data)
        if result.exit_code:
            raise OSError('Local workspace write failed')

    async def getcwd(self):
        self._task_resources._scope(self._execution_owner, launching=True)
        return self.workdir


class LocalTaskBackend(TaskResourceBackendMixin, _LocalProcessBackend):
    """Trusted local assembler binding with explicitly weaker cleanup semantics."""
    _resource_containment = 'trusted_process_group'

    def __init__(self, workspace, owner, registry):
        # Do not relax Bubblewrap's existing constructor gate for generic
        # subclasses. Only this concrete local type uses local process groups.
        if type(self) is not LocalTaskBackend:
            raise TypeError('Local Task backend must be the trusted concrete implementation')
        registry._scope(owner, launching=True)
        self.workspace, self.workdir = workspace, workspace.workdir
        self._execution_owner, self._task_resources = owner, registry
        self._receipts = {}
        self._mcp_closed = False
        workspace._gateway.bind(owner, registry)
        identity = 'local-mcp:' + secrets.token_hex(16)
        registry.register_external(owner, identity, tool_call_id=identity,
            cleanup=self._close_mcps, probe=self._mcps_closed)

    async def _close_mcps(self):
        await self.workspace._gateway.close_actors(self._execution_owner, self.workspace._local_mcp_state)
        if any(not path.exists() or path.read_text() != 'clean' for path in self.workspace._local_mcp_receipts):
            raise WorkspaceBindingError('Local MCP process cleanup is unverified')
        self.workspace._local_mcp_receipts.clear()
        if hasattr(self.workspace, '_native_mcp_connections'):
            self.workspace._native_mcp_connections.clear()
        self._mcp_closed = True

    async def _mcps_closed(self):
        return self._mcp_closed and not self.workspace._local_mcp_state.actors

    def _process_cleanup_probe(self, process, process_group_probe):
        receipt = self._receipts[process]
        async def probe():
            return (await process_group_probe() and receipt.exists() and receipt.read_text() == 'clean')
        return probe


class LocalSandboxProvider:
    name = 'local'

    async def probe(self):
        if sys.platform != 'linux' or not hasattr(os, 'pidfd_open'):
            raise WorkspaceBindingError('Local provider descendant cleanup requires Linux with pidfd support')
        try:
            fd = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(fd, 0)
            finally:
                os.close(fd)
        except OSError as error:
            raise WorkspaceBindingError('Local provider requires working kernel pidfd operations') from error
        with tempfile.TemporaryDirectory(prefix='workmate-local-probe-') as directory:
            receipt = Path(directory, 'receipt')
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', _SUPERVISOR,
                str(receipt), sys.executable, '-I', '-c', 'pass',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await _join(process.wait())
                raise
            if process.returncode != 0 or not receipt.exists() or receipt.read_text() != 'clean':
                raise WorkspaceBindingError('Local supervisor cleanup probe failed')

    def create_workspace(self, request):
        if request.local_workdir is None:
            raise WorkspaceBindingError('Local provider requires a host workspace directory')
        workspace = LocalWorkspace(workdir=str(request.local_workdir), workspace_id=request.workspace_id)
        workspace.host_workdir = workspace.workdir
        workspace._workmate_provider = 'local'
        workspace._local_receipts = tempfile.TemporaryDirectory(prefix='workmate-local-receipts-')
        workspace._local_mcp_receipts = []
        workspace._local_bindings = []
        def client_factory(spec):
            from workmate_runtime.mcp.http_transport import governed_client_factory
            spec = deepcopy(spec)
            config = spec.get('mcp_config', {})
            if config.get('type') == 'stdio_mcp':
                receipt = Path(workspace._local_receipts.name, secrets.token_hex(16))
                workspace._local_mcp_receipts.append(receipt)
                command, args = config['command'], config.get('args') or []
                config['command'] = sys.executable
                config['args'] = ['-I', _SUPERVISOR, str(receipt), '--cwd',
                    str(config.get('cwd') or workspace.workdir), '--', command, *args]
                config['cwd'] = None
            return governed_client_factory(spec)
        workspace._local_mcp_state = GatewayState(client_factory)
        workspace._gateway = None
        return workspace

    async def initialize_workspace(self, workspace):
        ensure_convention_dirs(workspace.workdir)
        for name in ('skills', 'sessions', 'data'):
            Path(workspace.workdir, name).mkdir(parents=True, exist_ok=True)
        # ASGI transport stays in memory and needs neither a listening socket
        # nor the upstream LocalWorkspace .mcp persistence mechanism.
        token, nonce = secrets.token_hex(32), secrets.token_hex(16)
        app = build_app(workspace._local_mcp_state, auth_token=token, instance_nonce=nonce)
        source = GatewayClient(workspace.get_backend(), 1, auth_token=token, instance_nonce=nonce)
        workspace._gateway = _LocalGateway(source,
            http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False))
        workspace.is_alive = True

    def bind_backend(self, workspace, owner, registry):
        if not isinstance(workspace, LocalWorkspace) or not workspace.is_alive or workspace._workmate_provider != self.name:
            raise WorkspaceBindingError('Local Workspace is not initialized')
        backend = LocalTaskBackend(workspace, owner, registry)
        workspace._local_bindings.append(backend)
        return backend

    async def verify_task_isolation(self, backend):
        raise SandboxVerificationFailed('Local execution provides no sandbox isolation or Full Access proof')

    async def close_workspace(self, workspace):
        for backend in workspace._local_bindings:
            scope = backend._task_resources._scope(backend._execution_owner)
            if (not scope.sealed or any(not resource.finished or
                    (resource.launch is not None and (not resource.launch.done()
                    or resource.launch.cancelled() or resource.launch.exception() is not None))
                    for resource in scope.resources.values())):
                raise WorkspaceBindingError('Local Task resources are unresolved; cleanup evidence is retained')
        await workspace._local_mcp_state.close()
        if workspace._gateway is not None:
            await workspace._gateway.aclose()
            workspace._gateway = None
        workspace.is_alive = False
        workspace._local_receipts.cleanup()
