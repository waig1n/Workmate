# -*- coding: utf-8 -*-
"""Explicit artifact delivery tool tests."""

import json

from agentscope.permission import PermissionBehavior

from workmate_runtime.artifact_delivery import (
    PRESENTATION_MANIFEST_PATH,
    make_present_files_tool_factory,
)
from pathlib import Path
from agentscope.workspace import LocalWorkspace
from workmate_runtime.workspaces import ensure_convention_dirs


class WorkspaceFixture:
    """Test filesystem binding independent of any production manager."""
    def __init__(self, basedir):
        self.root = Path(basedir)

    async def get_workspace(self, user_id, agent_id, session_id):
        path = self.root / user_id / agent_id / session_id
        ensure_convention_dirs(str(path))
        return LocalWorkspace(workdir=str(path))

    async def close_all(self):
        pass


def _manager_resolver(manager):
    async def resolve(user_id, agent_id, session_id):
        return await manager.get_workspace(user_id, agent_id, session_id)

    return resolve


async def test_present_files_records_only_existing_output_files(tmp_path):
    manager = WorkspaceFixture(basedir=str(tmp_path))
    workspace = await manager.get_workspace("user", "agent", "session")
    report = tmp_path / "user" / "agent" / "session" / "output" / "report.md"
    report.write_text("done", encoding="utf-8")

    factory = make_present_files_tool_factory(_manager_resolver(manager))
    tools = await factory("user", "agent", "session")
    chunk = await tools[0].call(
        files=["output/report.md", "../escape.txt", "output/missing.pdf"],
    )
    result = json.loads(chunk.content[0].text)

    assert result["recorded"] is True
    assert result["presented"] == ["report.md"]
    assert set(result["rejected"]) == {"../escape.txt", "output/missing.pdf"}
    manifest = json.loads(
        (tmp_path / "user" / "agent" / "session" /
         PRESENTATION_MANIFEST_PATH).read_text(encoding="utf-8"),
    )
    assert manifest["version"] == 1
    assert manifest["files"] == ["report.md"]
    assert manifest["call_id"]

    decision = await tools[0].check_permissions({}, None)
    assert decision.behavior is PermissionBehavior.ALLOW
    await manager.close_all()


async def test_present_files_uses_the_session_bound_workspace(tmp_path):
    """The workspace must come from the session's persisted binding, not
    from recomputing an id out of (user, agent, session): AgentScope
    mints the binding at session creation, so recomputation selects a
    different (empty) sandbox and every declaration is rejected."""
    manager = WorkspaceFixture(basedir=str(tmp_path))
    bound = await manager.get_workspace("user", "agent", "minted-binding-id")
    report = (
        tmp_path / "user" / "agent" / "minted-binding-id" /
        "output" / "report.md"
    )
    report.write_text("done", encoding="utf-8")

    async def resolve(user_id, agent_id, session_id):
        assert (user_id, agent_id, session_id) == ("user", "agent", "session")
        return bound

    factory = make_present_files_tool_factory(resolve)
    tools = await factory("user", "agent", "session")
    chunk = await tools[0].call(files=["output/report.md"])
    result = json.loads(chunk.content[0].text)

    assert result["recorded"] is True
    assert result["presented"] == ["report.md"]
    assert (
        tmp_path / "user" / "agent" / "minted-binding-id" /
        PRESENTATION_MANIFEST_PATH
    ).exists()
    # Nothing leaked into the directory a recomputed id would select.
    assert not (tmp_path / "user" / "agent" / "session").exists()
    await manager.close_all()


async def test_present_files_does_not_replace_manifest_when_all_paths_invalid(
    tmp_path,
):
    manager = WorkspaceFixture(basedir=str(tmp_path))
    factory = make_present_files_tool_factory(_manager_resolver(manager))
    tool = (await factory("user", "agent", "session"))[0]

    chunk = await tool.call(files=["output/missing.txt"])
    result = json.loads(chunk.content[0].text)

    assert result["recorded"] is False
    manifest = (
        tmp_path / "user" / "agent" / "session" /
        PRESENTATION_MANIFEST_PATH
    )
    assert not manifest.exists()
    await manager.close_all()


async def test_present_files_factory_can_be_disabled(tmp_path):
    manager = WorkspaceFixture(basedir=str(tmp_path))
    factory = make_present_files_tool_factory(
        _manager_resolver(manager), enabled=False,
    )
    assert await factory("user", "agent", "session") == []
    await manager.close_all()
