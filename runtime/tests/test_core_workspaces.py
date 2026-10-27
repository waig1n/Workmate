"""Core workspace assembly must work with the App package unavailable."""
import os
from pathlib import Path
import subprocess
import sys


def test_core_workspace_tools_without_agentscope_app(tmp_path):
    runtime = Path(__file__).resolve().parents[1]
    source = '''
import asyncio
import sys
from importlib.abc import MetaPathFinder
class RejectApp(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agentscope.app' or fullname.startswith('agentscope.app.'):
            raise AssertionError('App dependency: ' + fullname)
sys.meta_path.insert(0, RejectApp())
from workmate_runtime.workspaces import (
    WorkmateSandboxWorkspace, WorkmateSandboxRead, WorkmateSandboxWrite, WorkmateSandboxEdit,
    WorkmateSandboxBash, WorkmateNestedBubblewrapBackend, ensure_convention_dirs,
)
from agentscope.tool import LocalBackend
async def check():
    ensure_convention_dirs(sys.argv[1])
    workspace = WorkmateSandboxWorkspace(host_workdir=sys.argv[1],
        read_model_input_types=['text/plain','image/*'])
    workspace._backend = LocalBackend()
    tools = await workspace.list_tools()
    assert {type(t) for t in tools} >= {WorkmateSandboxRead,WorkmateSandboxWrite,WorkmateSandboxEdit,WorkmateSandboxBash}
    read = next(t for t in tools if isinstance(t, WorkmateSandboxRead))
    assert read.model_input_types == ['text/plain','image/*']
    assert await workspace._restore_mcp_specs() == {}
    await workspace._save_mcp_file()
    assert not any(n == 'agentscope.app' or n.startswith('agentscope.app.') for n in sys.modules)
asyncio.run(check())
'''
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path)],
        env={**os.environ, 'PYTHONPATH': str(runtime)}, capture_output=True,
        text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
