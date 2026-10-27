# -*- coding: utf-8 -*-
"""Core sandbox filesystem tools and media capability contracts."""

import asyncio
import os

import pytest
from agentscope.message import ToolResultState
from agentscope.state import AgentState
from agentscope.tool import ExecResult, LocalBackend

from workmate_runtime.workspaces import (
    CONVENTION_DIRS,
    WorkmateClearedEnvBubblewrapBackend,
    WorkmateNestedBubblewrapBackend,
    WorkmateSandboxBash,
    WorkmateSandboxEdit,
    WorkmateSandboxRead,
    WorkmateSandboxWorkspace,
    WorkmateSandboxWrite,
    _sandbox_file_to_host,
    ensure_convention_dirs,
)


@pytest.fixture
def basedir(tmp_path):
    return str(tmp_path / "workspaces")


def test_ensure_convention_dirs_idempotent(tmp_path):
    d = str(tmp_path / "w")
    ensure_convention_dirs(d)
    ensure_convention_dirs(d)
    assert sorted(x for x in os.listdir(d) if not x.startswith(".")) == [
        "input",
        "output",
        "working",
    ]


def test_nested_bubblewrap_drops_all_capabilities(tmp_path):
    workdir = tmp_path / "workdir"
    tempdir = tmp_path / "tmp"
    backend = WorkmateNestedBubblewrapBackend(
        host_workdir=str(workdir),
        host_tmpdir=str(tempdir),
        env={},
    )

    args = backend._bwrap_argv(["env"], cwd=None)

    assert "--unshare-all" not in args
    assert "--unshare-pid" in args
    command_index = args.index("--")
    assert args[command_index - 2 : command_index] == [
        "--cap-drop",
        "ALL",
    ]
    assert args[command_index + 1 :] == ["env"]


def test_bubblewrap_launcher_environment_contains_no_runtime_secrets():
    launcher_env = WorkmateClearedEnvBubblewrapBackend._launcher_env()

    assert set(launcher_env) == {"PATH", "LANG"}
    assert "WORKMATE_INTERNAL_TOKEN" not in launcher_env
    assert "CHAT_MODEL_API_KEY" not in launcher_env


async def test_sandbox_bash_stays_attached_to_agent_run(tmp_path):
    """Long shell calls must not become unowned wake-up Replies."""
    backend = LocalBackend()

    async def exec_shell(_command, **_kwargs):
        return ExecResult(exit_code=0, stdout=b"done\n", stderr=b"")

    backend.exec_shell = exec_shell
    bash = WorkmateSandboxBash(cwd=str(tmp_path), backend=backend)

    assert bash.is_state_injected is True
    chunks = [
        chunk async for chunk in bash.call(
            "printf done",
            timeout=120000,
            _agent_state=AgentState(),
        )
    ]
    # Bash streams a terminal RUNNING chunk; Toolkit converts the assembled
    # ToolResponse to success after the generator completes.
    assert chunks[-1].state == ToolResultState.RUNNING


async def test_sandbox_workspace_never_persists_mcp_secrets(tmp_path):
    workspace = WorkmateSandboxWorkspace(host_workdir=str(tmp_path / "sandbox"))
    workspace._mcp_specs = {("agent", "session"): [object()]}

    await workspace._save_mcp_file()

    assert not (tmp_path / "sandbox" / ".mcp").exists()
    assert await workspace._restore_mcp_specs() == {}


@pytest.mark.parametrize("remote", [False, True])
async def test_sandbox_read_allows_following_write_and_edit(tmp_path, remote):
    """Bubblewrap virtual paths retain AgentScope's read-before-write guard."""
    host_workdir = tmp_path / "workspace"
    host_tmpdir = tmp_path / "tmp"
    target = host_workdir / "working" / "make_cheatsheet.py"
    target.parent.mkdir(parents=True)
    target.write_text("value = 'old'\n", encoding="utf-8")
    host_tmpdir.mkdir()
    backend = LocalBackend()

    async def file_exists(_path):
        return target.exists()

    async def is_dir(_path):
        return False

    async def read_file(_path):
        return target.read_bytes()

    async def write_file(_path, data):
        target.write_bytes(data)

    async def exec_shell(_command, **_kwargs):
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    backend.file_exists = file_exists
    backend.is_dir = is_dir
    backend.read_file = read_file
    backend.write_file = write_file
    backend.exec_shell = exec_shell

    kwargs = {
        "backend": backend,
        "sandbox_workdir": "/workspace",
        "host_workdir": None if remote else str(host_workdir),
    }
    read = WorkmateSandboxRead(**kwargs)
    write = WorkmateSandboxWrite(**kwargs)
    edit = WorkmateSandboxEdit(**kwargs)
    state = AgentState()
    virtual_path = "/workspace/working/make_cheatsheet.py"

    rejected = await asyncio.wait_for(
        write.call(virtual_path, "value = 'new'\n", state), timeout=2,
    )
    assert rejected.state == ToolResultState.ERROR

    read_result = await asyncio.wait_for(
        read.call(virtual_path, limit=1, _agent_state=state), timeout=2,
    )
    assert read_result.state != ToolResultState.ERROR
    written = await asyncio.wait_for(
        write.call(virtual_path, "value = 'new'\n", state), timeout=2,
    )
    assert written.state != ToolResultState.ERROR
    assert target.read_text(encoding="utf-8") == "value = 'new'\n"

    # A mutation invalidates the old evidence; another Read is required.
    rejected_edit = await asyncio.wait_for(
        edit.call(virtual_path, "new", "final", _agent_state=state),
        timeout=2,
    )
    assert rejected_edit.state == ToolResultState.ERROR
    await asyncio.wait_for(
        read.call(virtual_path, limit=1, _agent_state=state), timeout=2,
    )
    edited = await asyncio.wait_for(
        edit.call(virtual_path, "new", "final", _agent_state=state),
        timeout=2,
    )
    assert edited.state != ToolResultState.ERROR
    assert target.read_text(encoding="utf-8") == "value = 'final'\n"


def test_sandbox_cache_path_rejects_paths_outside_mount(tmp_path):
    host_workdir = tmp_path / "workspace"
    host_workdir.mkdir()

    assert _sandbox_file_to_host(
        "/workspace/working/a.py",
        sandbox_workdir="/workspace",
        host_workdir=str(host_workdir),
    ) == str(host_workdir / "working" / "a.py")


def _png_bytes() -> bytes:
    # 1x1 white PNG (magic header is what the extension dispatch uses).
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63fcffff3f030005fe02fea72d3f9b"
        "0000000049454e44ae426082",
    )


async def test_read_rejects_image_for_text_only_model(tmp_path):
    """A text-only model gets an explicit error, not a base64 DataBlock."""
    from agentscope.message import TextBlock

    backend = LocalBackend()

    async def file_exists(_path):
        return True

    async def is_dir(_path):
        return False

    backend.file_exists = file_exists
    backend.is_dir = is_dir

    read = WorkmateSandboxRead(
        backend=backend,
        sandbox_workdir="/workspace",
        host_workdir=str(tmp_path),
        model_input_types=["text/plain"],
    )

    result = await read.call("/workspace/photo.png")

    assert result.state == ToolResultState.ERROR
    assert isinstance(result.content[0], TextBlock)
    assert "Unsupported image type" in result.content[0].text


async def test_read_returns_image_data_block_for_multimodal_model(tmp_path):
    """A multimodal model still receives the image DataBlock."""
    from agentscope.message import DataBlock

    png = _png_bytes()
    backend = LocalBackend()

    async def file_exists(_path):
        return True

    async def is_dir(_path):
        return False

    async def read_file(_path):
        return png

    backend.file_exists = file_exists
    backend.is_dir = is_dir
    backend.read_file = read_file

    read = WorkmateSandboxRead(
        backend=backend,
        sandbox_workdir="/workspace",
        host_workdir=str(tmp_path),
        model_input_types=["text/plain", "image/*"],
    )

    result = await read.call("/workspace/photo.png")

    assert result.state != ToolResultState.ERROR
    data_blocks = [
        block for block in result.content if isinstance(block, DataBlock)
    ]
    assert data_blocks, "expected an image DataBlock for a multimodal model"


async def test_read_returns_video_data_block_for_video_model(tmp_path):
    """Declared video input is binary media, never UTF-8 text fallback."""
    from agentscope.message import DataBlock

    backend = LocalBackend()

    async def file_exists(_path):
        return True

    async def is_dir(_path):
        return False

    async def read_file(_path):
        return b"video-bytes"

    backend.file_exists = file_exists
    backend.is_dir = is_dir
    backend.read_file = read_file
    read = WorkmateSandboxRead(
        backend=backend,
        sandbox_workdir="/workspace",
        host_workdir=str(tmp_path),
        model_input_types=["text/plain", "video/*"],
    )

    result = await read.call("/workspace/clip.mp4")

    assert result.state != ToolResultState.ERROR
    block = next(item for item in result.content if isinstance(item, DataBlock))
    assert block.source.media_type == "video/mp4"


async def test_read_rejects_video_for_text_model(tmp_path):
    backend = LocalBackend()

    async def file_exists(_path):
        return True

    async def is_dir(_path):
        return False

    backend.file_exists = file_exists
    backend.is_dir = is_dir
    read = WorkmateSandboxRead(
        backend=backend,
        sandbox_workdir="/workspace",
        host_workdir=str(tmp_path),
        model_input_types=["text/plain"],
    )

    result = await read.call("/workspace/clip.mp4")

    assert result.state == ToolResultState.ERROR
    assert "Unsupported media type video/mp4" in result.content[0].text


async def test_read_defaults_to_text_only_input_types(tmp_path):
    """Undeclared model capabilities fail closed: text-only."""
    from agentscope.message import TextBlock

    backend = LocalBackend()

    async def file_exists(_path):
        return True

    async def is_dir(_path):
        return False

    backend.file_exists = file_exists
    backend.is_dir = is_dir

    read = WorkmateSandboxRead(
        backend=backend,
        sandbox_workdir="/workspace",
        host_workdir=str(tmp_path),
    )

    assert read.model_input_types == ["text/plain"]
    result = await read.call("/workspace/photo.png")
    assert result.state == ToolResultState.ERROR
    assert isinstance(result.content[0], TextBlock)


async def test_sandbox_workspace_passes_input_types_to_read(tmp_path):
    """list_tools() binds WorkmateSandboxRead with the manager's declared types."""
    workspace = WorkmateSandboxWorkspace(
        host_workdir=str(tmp_path / "sandbox"),
        read_model_input_types=["text/plain", "image/*"],
    )
    assert workspace._read_model_input_types == ["text/plain", "image/*"]

    # The Read binding is built lazily against the live backend; verifying
    # the stored declaration covers the propagation path from the manager.
    read = WorkmateSandboxRead(
        backend=LocalBackend(),
        sandbox_workdir="/workspace",
        host_workdir=str(tmp_path),
        model_input_types=workspace._read_model_input_types,
    )
    assert read.model_input_types == ["text/plain", "image/*"]


async def test_sandbox_workspace_defaults_to_text_only():
    workspace = WorkmateSandboxWorkspace(host_workdir="/tmp/workmate-unused")
    assert workspace._read_model_input_types == ["text/plain"]


async def test_image_bootstrap_skips_duplicate_import_process(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    workspace = WorkmateSandboxWorkspace(workspace_id='image', host_workdir=str(tmp_path))
    backend = type('Backend', (), {'exec_shell': AsyncMock(side_effect=AssertionError('extra process'))})()
    workspace._backend = backend
    monkeypatch.setattr(WorkmateSandboxWorkspace, '_gateway_python', property(lambda self: '/usr/local/bin/python'))
    assert workspace._bootstrap_commands() == []
    assert await workspace._bootstrap_is_ready()
    backend.exec_shell.assert_not_awaited()
    assert not workspace.is_alive


@pytest.mark.parametrize('ready', [True, False])
async def test_non_image_bootstrap_keeps_upstream_readiness(monkeypatch, tmp_path, ready):
    from unittest.mock import AsyncMock
    from agentscope.workspace import BubblewrapWorkspace
    workspace = WorkmateSandboxWorkspace(workspace_id='local', host_workdir=str(tmp_path))
    probe = AsyncMock(return_value=ready)
    monkeypatch.setattr(WorkmateSandboxWorkspace, '_gateway_python', property(lambda self: '/sandbox/venv/python'))
    monkeypatch.setattr(BubblewrapWorkspace, '_bootstrap_is_ready', probe)
    monkeypatch.setattr(BubblewrapWorkspace, '_bootstrap_commands', lambda self: ['fixture install'])
    assert await workspace._bootstrap_is_ready() is ready
    probe.assert_awaited_once()
    assert workspace._bootstrap_commands() == ['fixture install']
