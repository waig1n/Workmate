# -*- coding: utf-8 -*-
"""Product-owned current execution policy for Runtime tool calls."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field

from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import ToolCallState

CONFIRMED_WRITE_TOOLS = frozenset({"agent_create", "agent_update", "skill_install"})


DeniedToolGroup = Literal[
    "shell",
    "workspace_write",
    "external_connectors",
    "artifact_delivery",
    "agent_management",
    "skill_management",
    "web_access",
]


class ExecutionPermissionPolicy(BaseModel):
    """Current Agent ceiling plus the active Product Task approval profile."""

    task_id: str = Field(min_length=1, max_length=64)
    approval_profile: Literal[
        "standard", "strict", "read_only", "full_access", "unattended",
    ]
    denied_groups: list[DeniedToolGroup] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list, max_length=64)


def _normalized_tool_name(tool) -> str:
    return str(getattr(tool, "name", "")).strip().casefold()


def _tool_group(tool) -> str | None:
    name = _normalized_tool_name(tool)
    module = type(tool).__module__.casefold()
    if name in {"bash", "workmatesandboxbash", "powershell"}:
        return "shell"
    if name in {"write", "edit", "workmatesandboxwrite", "workmatesandboxedit"}:
        return "workspace_write"
    if name == "present_files":
        return "artifact_delivery"
    if name in {
        "platform_get_context", "model_list", "capability_list",
        "agent_list", "agent_get", "agent_validate", "agent_create",
        "agent_update",
    }:
        return "agent_management"
    if name in {"skill_validate", "skill_install"}:
        return "skill_management"
    if name in {"web_search", "web_fetch"}:
        return "web_access"
    if (
        getattr(tool, "is_mcp", False)
        or getattr(tool, "is_external_tool", False)
        or name.startswith("mcp__")
        or ".mcp" in module
    ):
        return "external_connectors"
    return None


class ProductPermissionMiddleware(MiddlewareBase):
    """Enforce Agent DENY rules before applying the Task approval profile.

    ``full_access`` is therefore governed rather than raw BYPASS: Agent-level
    denials are evaluated before the AgentScope engine. ``standard`` only
    converts a non-safety fallback ASK for local sandbox execution to ALLOW;
    bypass-immune safety decisions and external/unknown actions still ASK.
    """

    def __init__(self, policy: ExecutionPermissionPolicy | None) -> None:
        self._policy = policy

    async def on_check_permission(
        self, agent, input_kwargs: dict, next_handler,
    ) -> PermissionDecision:
        del agent
        decision = await self._evaluate(input_kwargs, next_handler)
        if (
            self._policy is not None
            and self._policy.approval_profile == "unattended"
            and decision.behavior is PermissionBehavior.ASK
        ):
            # Run after Product's own confirmation rules as well as the Core
            # engine: DONT_ASK alone cannot catch middleware-added ASK decisions.
            decision = replace(
                decision,
                behavior=PermissionBehavior.DENY,
                message=f"Unattended task cannot request approval: {decision.message}",
                decision_reason=(
                    "Product: unattended ASK converted to DENY. "
                    f"Original reason: {decision.decision_reason or decision.message}"
                ),
            )
        if (
            self._policy is not None
            and self._policy.approval_profile == "unattended"
            and decision.behavior is PermissionBehavior.DENY
            and decision.decision_reason
            and decision.decision_reason not in decision.message
        ):
            # Core emits only message as the denied tool result. Carry the
            # reason there so persisted tool content and its audit preview
            # retain safety details (including DONT_ASK engine conversions).
            decision = replace(
                decision, message=f"{decision.message} ({decision.decision_reason})",
            )
        return decision

    async def _evaluate(self, input_kwargs: dict, next_handler) -> PermissionDecision:
        if self._policy is None:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="Current Product execution policy is missing",
                decision_reason="Product execution policy missing",
            )

        tool = input_kwargs["tool"]
        name = _normalized_tool_name(tool)
        denied_tools = {
            item.strip().casefold() for item in self._policy.denied_tools
        }
        if name in denied_tools:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"Agent policy denies tool {getattr(tool, 'name', name)}",
                decision_reason=f"Agent policy: denied tool {name}",
            )

        group = _tool_group(tool)
        if group and group in self._policy.denied_groups:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"Agent policy denies tool group {group}",
                decision_reason=f"Agent policy: denied group {group}",
            )

        decision = await next_handler(**input_kwargs)
        if name in CONFIRMED_WRITE_TOOLS:
            if decision.behavior is PermissionBehavior.DENY:
                return decision
            if (
                self._policy.approval_profile == "unattended"
                and decision.behavior is PermissionBehavior.ASK
            ):
                # Keep the underlying safety reason instead of replacing it
                # with the additional resource submission confirmation.
                return decision
            if self._policy.approval_profile == "read_only":
                return PermissionDecision(
                    behavior=PermissionBehavior.DENY,
                    message="Read-only tasks cannot submit resource changes",
                )
            call = input_kwargs.get("tool_call")
            if getattr(call, "state", None) != ToolCallState.ALLOWED:
                return PermissionDecision(
                    behavior=PermissionBehavior.ASK,
                    bypass_immune=True,
                    message="Confirm this Agent change or Skill installation before submission",
                    decision_reason="Product: explicit resource submission confirmation",
                )
            return decision
        if (
            self._policy.approval_profile == "standard"
            and group == "shell"
            and decision.behavior is PermissionBehavior.ASK
            and not decision.bypass_immune
            and decision.decision_reason == "Mode: accept_edits"
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Permission granted for sandboxed local execution",
                decision_reason="Risk policy: sandboxed local execution",
                updated_input=input_kwargs.get("tool_input"),
            )
        return decision
