"""Bubblewrap implementation of the Worker sandbox boundary."""
import asyncio
import os
import re

from workmate_runtime.execution.controller import SandboxVerificationFailed
from workmate_runtime.execution.resources import TaskResourceBackendMixin
from workmate_runtime.mcp.workspace import WorkerMCPWorkspace
from workmate_runtime.workspaces import (
    WorkmateSandboxWorkspace, WorkmateClearedEnvBubblewrapBackend, WorkmateNestedBubblewrapBackend,
    ensure_convention_dirs,
)
from .base import SandboxWorkspaceRequest, WorkspaceBindingError


class _TaskBackend(TaskResourceBackendMixin, WorkmateClearedEnvBubblewrapBackend):
    pass


class _NestedTaskBackend(TaskResourceBackendMixin, WorkmateNestedBubblewrapBackend):
    pass


class BubblewrapSandboxProvider:
    name = 'bubblewrap'

    def __init__(self, *, workspace_factory=WorkerMCPWorkspace):
        self._factory = workspace_factory

    def create_workspace(self, request: SandboxWorkspaceRequest):
        if request.local_workdir is None:
            raise WorkspaceBindingError('Bubblewrap requires a local workspace directory')
        ensure_convention_dirs(str(request.local_workdir))
        return self._factory(workspace_id=request.workspace_id,
            host_workdir=str(request.local_workdir), gateway_port=None)

    async def initialize_workspace(self, workspace):
        await workspace.initialize()

    async def close_workspace(self, workspace):
        await workspace.close()

    def bind_backend(self, workspace, owner, registry):
        source = workspace.get_backend()
        backend_type = {WorkmateClearedEnvBubblewrapBackend: _TaskBackend,
            WorkmateNestedBubblewrapBackend: _NestedTaskBackend}.get(type(source))
        if backend_type is None:
            raise WorkspaceBindingError('Unsupported workspace backend for Task resource coverage')
        # Keep the shared gateway backend distinct from each Task owner.
        return backend_type(resource_registry=registry, execution_owner=owner,
            host_workdir=source._host_workdir, host_tmpdir=source._host_tmpdir,
            host_cache_dir=source._host_cache_dir, workdir=source._workdir,
            share_net=source._share_net, env=dict(source._env))

    async def probe(self):
        await WorkmateSandboxWorkspace._probe_bubblewrap()

    async def verify_task_isolation(self, backend):
        if type(backend) not in {_TaskBackend, _NestedTaskBackend}:
            raise SandboxVerificationFailed('Only a bound production Task backend can attest sandbox isolation')
        try:
            host_namespace = os.readlink('/proc/self/ns/pid')
            result = await backend.exec_shell(['/usr/bin/readlink', '/proc/self/ns/pid'], timeout=10)
            namespace = result.stdout.decode('ascii', errors='strict').strip()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise SandboxVerificationFailed('Task Bubblewrap probe failed') from error
        if result.exit_code != 0 or not re.fullmatch(r'pid:\[\d+\]', namespace) or namespace == host_namespace:
            raise SandboxVerificationFailed('Task Bubblewrap PID namespace could not be verified')
