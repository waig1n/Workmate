from types import SimpleNamespace

import pytest

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.message import ToolCallState

from workmate_runtime.permission_policy import (
    ExecutionPermissionPolicy,
    ProductPermissionMiddleware,
)


@pytest.mark.parametrize("name", ["agent_create", "agent_update", "skill_install"])
@pytest.mark.parametrize("profile", ["standard", "strict", "full_access", "read_only"])
async def test_resource_submission_requires_one_confirmation(name, profile):
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id="t", approval_profile=profile,
    ))
    async def allowed(**kwargs):
        return await _decision(PermissionBehavior.ALLOW)
    kwargs = {"tool": SimpleNamespace(name=name), "tool_input": {},
              "tool_call": SimpleNamespace(state=ToolCallState.PENDING)}
    decision = await middleware.on_check_permission(None, kwargs, allowed)
    if profile == "read_only":
        assert decision.behavior is PermissionBehavior.DENY
    else:
        assert decision.behavior is PermissionBehavior.ASK
        assert decision.bypass_immune
        kwargs["tool_call"].state = ToolCallState.ALLOWED
        decision = await middleware.on_check_permission(None, kwargs, allowed)
        assert decision.behavior is PermissionBehavior.ALLOW
        middleware._policy.denied_tools = [name]
        decision = await middleware.on_check_permission(None, kwargs, allowed)
        assert decision.behavior is PermissionBehavior.DENY


async def _decision(behavior, *, bypass_immune=False):
    return PermissionDecision(
        behavior=behavior,
        message="decision",
        bypass_immune=bypass_immune,
    )


async def test_agent_deny_overrides_full_access():
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id="task-1",
        approval_profile="full_access",
        denied_groups=["shell"],
    ))
    delegated = False

    async def next_handler(**_kwargs):
        nonlocal delegated
        delegated = True
        return await _decision(PermissionBehavior.ALLOW)

    result = await middleware.on_check_permission(
        agent=SimpleNamespace(),
        input_kwargs={
            "tool": SimpleNamespace(name="Bash"),
            "tool_input": {"command": "python build.py"},
        },
        next_handler=next_handler,
    )

    assert result.behavior is PermissionBehavior.DENY
    assert result.decision_reason == "Agent policy: denied group shell"
    assert delegated is False


@pytest.mark.parametrize("tool_name", ["Bash", "WorkmateSandboxBash"])
async def test_standard_auto_allows_non_safety_local_execution(tool_name):
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id="task-1",
        approval_profile="standard",
    ))

    async def next_handler(**_kwargs):
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="decision",
            decision_reason="Mode: accept_edits",
        )

    result = await middleware.on_check_permission(
        agent=SimpleNamespace(),
        input_kwargs={
            "tool": SimpleNamespace(name=tool_name),
            "tool_input": {"command": "python build.py"},
        },
        next_handler=next_handler,
    )

    assert result.behavior is PermissionBehavior.ALLOW
    assert result.decision_reason == "Risk policy: sandboxed local execution"


async def test_standard_preserves_safety_and_external_asks():
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id="task-1",
        approval_profile="standard",
    ))

    async def safety(**_kwargs):
        return await _decision(
            PermissionBehavior.ASK,
            bypass_immune=True,
        )

    unsafe = await middleware.on_check_permission(
        agent=SimpleNamespace(),
        input_kwargs={
            "tool": SimpleNamespace(name="Bash"),
            "tool_input": {"command": "rm -rf /"},
        },
        next_handler=safety,
    )
    assert unsafe.behavior is PermissionBehavior.ASK

    async def external(**_kwargs):
        return await _decision(PermissionBehavior.ASK)

    connector = await middleware.on_check_permission(
        agent=SimpleNamespace(),
        input_kwargs={
            "tool": SimpleNamespace(name="mcp__mail__send"),
            "tool_input": {"to": "outside@example.com"},
        },
        next_handler=external,
    )
    assert connector.behavior is PermissionBehavior.ASK


async def test_missing_product_policy_fails_closed():
    middleware = ProductPermissionMiddleware(None)

    async def next_handler(**_kwargs):
        return await _decision(PermissionBehavior.ALLOW)

    result = await middleware.on_check_permission(
        agent=SimpleNamespace(),
        input_kwargs={"tool": SimpleNamespace(name="Read"), "tool_input": {}},
        next_handler=next_handler,
    )
    assert result.behavior is PermissionBehavior.DENY
    assert result.decision_reason == "Product execution policy missing"


async def test_standard_preserves_explicit_ask_rule():
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id="task-1",
        approval_profile="standard",
    ))

    async def next_handler(**_kwargs):
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message="decision",
            decision_reason="Rule: always confirm this command",
        )

    result = await middleware.on_check_permission(
        agent=SimpleNamespace(),
        input_kwargs={
            "tool": SimpleNamespace(name="Bash"),
            "tool_input": {"command": "python build.py"},
        },
        next_handler=next_handler,
    )
    assert result.behavior is PermissionBehavior.ASK
