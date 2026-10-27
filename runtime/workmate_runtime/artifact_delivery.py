# -*- coding: utf-8 -*-
"""Explicit artifact delivery tool for the workmate hybrid projection mode."""

from __future__ import annotations

import json
import uuid
from pathlib import PurePosixPath
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool

PRESENTATION_MANIFEST_PATH = ".workmate/presented_files.json"
MAX_PRESENTED_FILES = 100


def _normalize_output_path(value: object) -> str | None:
    """Return an output-relative POSIX path, rejecting traversal."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or "\\" in raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute():
        return None
    parts = path.parts
    if parts and parts[0] == "output":
        parts = parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


class PresentFilesTool(FunctionTool):
    """A safe metadata-only tool that never needs HITL approval."""

    async def check_permissions(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Recording deliverable file paths is safe.",
        )


def make_present_files_tool_factory(resolve_workspace, *, enabled: bool = True):
    """Build AgentScope's per-session ``extra_agent_tools`` factory.

    ``resolve_workspace(user_id, agent_id, session_id)`` must return the
    session's *persisted* workspace binding. Recomputing a workspace id
    from the session id selects a different sandbox (AgentScope mints
    and stores the binding at session creation), and every declaration
    would be rejected against an empty directory.
    """

    async def factory(user_id: str, agent_id: str, session_id: str):
        if not enabled:
            return []
        workspace = await resolve_workspace(user_id, agent_id, session_id)
        backend = workspace.get_backend()

        async def present_files(files: list[str]) -> dict:
            """Declare final deliverables from output/ for this run.

            Pass the complete list of files the user should receive. Paths may
            be relative to output/ or start with output/. Only existing files
            are recorded. Call this after all deliverables have been written.
            """
            accepted: list[str] = []
            rejected: dict[str, str] = {}
            seen: set[str] = set()
            for original in files[:MAX_PRESENTED_FILES]:
                display_path = str(original)
                normalized = _normalize_output_path(original)
                if normalized is None:
                    rejected[display_path] = "path must be inside output/"
                    continue
                if normalized in seen:
                    continue
                target = backend.join_path(
                    workspace.workdir,
                    "output",
                    *normalized.split("/"),
                )
                if not await backend.file_exists(target):
                    rejected[display_path] = "file does not exist"
                    continue
                if await backend.is_dir(target):
                    rejected[display_path] = "path is a directory"
                    continue
                seen.add(normalized)
                accepted.append(normalized)

            if len(files) > MAX_PRESENTED_FILES:
                rejected["<additional files>"] = (
                    f"only the first {MAX_PRESENTED_FILES} entries were checked"
                )
            if accepted:
                manifest_path = backend.join_path(
                    workspace.workdir,
                    *PRESENTATION_MANIFEST_PATH.split("/"),
                )
                manifest = {
                    "version": 1,
                    "call_id": str(uuid.uuid4()),
                    "files": accepted,
                }
                await backend.write_file(
                    manifest_path,
                    json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                )

            return {
                "recorded": bool(accepted),
                "presented": accepted,
                "rejected": rejected,
            }

        return [
            PresentFilesTool(
                present_files,
                name="present_files",
                description=(
                    "Declare the final files to deliver to the user. Call once "
                    "after writing all finished deliverables under output/."
                ),
                is_concurrency_safe=False,
                is_read_only=False,
            ),
        ]

    return factory
