"""Worker sandbox bootstrap with retained gateway process cleanup evidence."""
import asyncio
import os
import shlex

from agentscope.workspace._bubblewrap._bubblewrap_backend import BubblewrapBackend
from agentscope.workspace._bubblewrap._bubblewrap_workspace import _read_glob_helper_bytes, SANDBOX_WORKDIR
from agentscope.workspace._gateway_client import GatewayClient
from workmate_runtime.workspaces import WorkmateSandboxWorkspace
from workmate_runtime.observability.preparation import bootstrap_stage
from .bundle import build_gateway_bundle
from .gateway import MemoryGatewayClient, MCPGatewayError


class WorkerMCPWorkspace(WorkmateSandboxWorkspace):
    async def _wait_gateway_health(self, timeout):
        """Cap retry sleeps at 100 ms and bound each health call by time left.

        Cold recovery creates a new process; long exponential sleeps add to
        both initial preparation and Resume even after that process is ready.
        This only changes discovery cadence, never the health/nonce contract.
        """
        assert self._gateway is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._gateway_process is not None and self._gateway_process.returncode is not None:
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                if await asyncio.wait_for(self._gateway.health(), remaining):
                    return True
            except TimeoutError:
                return False
            await asyncio.sleep(min(.1, max(0, deadline - loop.time())))
        return False

    async def _setup_mcp_gateway(self):
        backend = self.get_backend()
        with bootstrap_stage(self.workspace_id, 'bootstrap_probe'):
            ready = await self._bootstrap_is_ready()
        if not ready:
            with bootstrap_stage(self.workspace_id, 'bootstrap_install'):
                for command in self._bootstrap_commands():
                    result = await backend.exec_shell(['sh', '-c', command], timeout=self._bootstrap_cmd_timeout)
                    if not result.ok():
                        raise MCPGatewayError('MCP sandbox bootstrap failed')
        with bootstrap_stage(self.workspace_id, 'gateway_files'):
            await backend.write_file(self._glob_helper_path, _read_glob_helper_bytes())
            await backend.write_file(self._gateway_script, build_gateway_bundle())
        attempts = 3 if self._gateway_port_input is None else 1
        for attempt in range(attempts):
            with bootstrap_stage(self.workspace_id, 'gateway_previous_close'):
                await self._stop_gateway_process()
                if self._gateway is not None:
                    await self._gateway.aclose()
                    self._gateway = None
            if self._gateway_port_input is None:
                self.gateway_port = self._allocate_gateway_port()
            self._rotate_gateway_credentials()
            command = (f'exec {shlex.quote(self._gateway_python)} -I -u '
                f'{shlex.quote(self._gateway_script)} --port {self.gateway_port} '
                f'--auth-token={shlex.quote(self._gateway_token)} '
                f'--instance-nonce={shlex.quote(self._gateway_nonce)} '
                f'> {shlex.quote(self._gateway_log)} 2>&1')
            with bootstrap_stage(self.workspace_id, 'gateway_spawn'):
                self._gateway_process = await backend.start_process(['sh', '-c', command], cwd=SANDBOX_WORKDIR)
            source_client = GatewayClient(backend, self.gateway_port, timeout=30.0,
                auth_token=self._gateway_token, instance_nonce=self._gateway_nonce)
            self._gateway = MemoryGatewayClient(source_client)
            with bootstrap_stage(self.workspace_id, 'gateway_health'):
                healthy = await self._wait_gateway_health(30.0)
            if healthy:
                return
        raise MCPGatewayError('MCP gateway did not become ready')

    async def _stop_gateway_process(self):
        process = self._gateway_process
        if process is None:
            return
        if process.returncode is None:
            if os.getpgid(process.pid) != process.pid:
                raise MCPGatewayError('Gateway process group identity is unavailable')
            await BubblewrapBackend._terminate_process_tree(process, grace=5.0)
        await process.wait()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            self._gateway_process = None
            return
        raise MCPGatewayError('Gateway process descendants remain unresolved')

    async def close(self):
        # Do not inherit Core.close: it swallows shutdown failures and erases
        # the only original handles needed to diagnose/retry them.
        await self._stop_gateway_process()
        if self._gateway is not None:
            await self._gateway.aclose()
            self._gateway = None
        for attribute in ('_tmpdir', '_owned_cache_dir', '_owned_workdir'):
            directory = getattr(self, attribute, None)
            if directory is not None:
                directory.cleanup()
                setattr(self, attribute, None)
        self._mcp_instances.clear()
        self._mcp_last_used.clear()
        self._backend = None
        self.is_alive = False
