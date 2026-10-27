"""Remote Task admission and cleanup do not confuse SDK completion with quiescence."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.tool import ExecResult
from workmate_runtime.execution.resources import TaskResourceRegistry
from workmate_runtime.transport.postgres import ExecutionOwner
from workmate_runtime.sandbox.remote_backend import RemoteTaskBackend

OWNER = ExecutionOwner('session', 'task', 'worker', 1)


class Control:
    python = '/usr/bin/python3'
    directory = '/var/lib/workmate/test'
    workdir = '/workspace'
    def __init__(self):
        self.calls = []
        self.state = {'active': False, 'epoch': '', 'pids': []}
    async def operation(self, operation, **payload):
        self.calls.append(operation)
        if operation == 'begin': self.state.update(active=True, epoch=payload['epoch'])
        if operation == 'seal': self.state['active'] = False
        if operation == 'kill': self.state['pids'] = []
        return dict(self.state)
    async def exec_shell(self, command, **kwargs):
        self.calls.append(command)
        return ExecResult(0, b'ok', b'')


async def test_cleanup_seals_launches_before_proving_empty_processes():
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER, fresh_claim=True, all_resource_paths_tracked=True)
    control = Control()
    workspace = SimpleNamespace(_gateway=None, _native_mcp_connections={})
    backend = RemoteTaskBackend(control, OWNER, registry, workspace)
    assert (await backend.exec_shell(['true'])).stdout == b'ok'
    assert await registry.verify_preparation_cleanup(OWNER)
    assert control.calls.index('seal') < control.calls.index('kill')
    with pytest.raises(Exception):
        await backend.exec_shell(['true'])


async def test_no_command_still_requires_real_cleanup_receipt():
    registry = TaskResourceRegistry()
    registry.open_scope(OWNER, fresh_claim=True, all_resource_paths_tracked=True)
    control = Control()
    backend = RemoteTaskBackend(control, OWNER, registry, SimpleNamespace(_gateway=None))
    assert await registry.verify_preparation_cleanup(OWNER)
    assert 'seal' in control.calls and 'kill' in control.calls
