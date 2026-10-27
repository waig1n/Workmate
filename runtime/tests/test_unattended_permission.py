"""Unattended tasks must finish tool permission checks without parking."""
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolCallState, UserMsg
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode

from workmate_runtime.assembly.agent import _ExecutionConfiguration
from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware
from test_core_engine import ConfirmTool, collect, make_engine


@pytest.mark.parametrize('name', ['Bash', 'mcp__mail__send', 'agent_create', 'agent_update', 'skill_install'])
@pytest.mark.parametrize('safety', [False, True])
async def test_final_ask_is_denied_with_original_reason(name, safety):
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id='scheduled', approval_profile='unattended'))
    original = PermissionDecision(behavior=PermissionBehavior.ASK,
        message='Confirm dangerous operation', decision_reason='Safety: dangerous operation',
        bypass_immune=safety)

    async def ask(**kwargs):
        return original

    result = await middleware.on_check_permission(None, {
        'tool': SimpleNamespace(name=name), 'tool_input': {},
        'tool_call': SimpleNamespace(state=ToolCallState.PENDING)}, ask)
    assert result.behavior is PermissionBehavior.DENY
    assert 'unattended' in result.message.lower()
    assert 'Safety: dangerous operation' in result.decision_reason
    assert 'Confirm dangerous operation' in result.message
    assert original.behavior is PermissionBehavior.ASK


@pytest.mark.parametrize('name', ['agent_create', 'agent_update', 'skill_install'])
async def test_platform_confirmation_added_after_engine_allow_is_denied(name):
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id='scheduled', approval_profile='unattended'))

    async def allow(**kwargs):
        return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='allowed')

    result = await middleware.on_check_permission(None, {
        'tool': SimpleNamespace(name=name), 'tool_input': {},
        'tool_call': SimpleNamespace(state=ToolCallState.PENDING)}, allow)
    assert result.behavior is PermissionBehavior.DENY
    assert 'explicit resource submission confirmation' in result.decision_reason


@pytest.mark.parametrize('behavior', [PermissionBehavior.ALLOW, PermissionBehavior.DENY])
async def test_unattended_preserves_resolved_decision(behavior):
    middleware = ProductPermissionMiddleware(ExecutionPermissionPolicy(
        task_id='scheduled', approval_profile='unattended'))
    original = PermissionDecision(behavior=behavior, message='original', decision_reason='policy')

    async def resolved(**kwargs):
        return original

    result = await middleware.on_check_permission(None,
        {'tool': SimpleNamespace(name='Read'), 'tool_input': {}}, resolved)
    assert result.behavior is behavior
    assert original.message in result.message
    assert result.decision_reason == original.decision_reason


@pytest.mark.parametrize('name', ['write_record', 'agent_create', 'skill_install'])
async def test_real_core_unattended_denial_never_requests_confirmation(name):
    tool = ConfirmTool()
    tool.name = name
    tool.safety_ask = True
    async def safety(tool_input, context):
        return PermissionDecision(behavior=PermissionBehavior.ASK,
            message='Check safety', decision_reason='Safety: dangerous operation', bypass_immune=True)
    tool.check_permissions = safety
    # Allowing platform tool at engine level exercises Product's additional ASK.
    if name != 'write_record':
        async def allow(tool_input, context):
            return PermissionDecision(behavior=PermissionBehavior.ALLOW, message='allowed')
        tool.check_permissions = allow
    engine = make_engine([[ToolCallBlock(id='write', name=name, input='{}')],
        [TextBlock(text='Operation denied; finished remaining work')]], tool=tool,
        middlewares=[ProductPermissionMiddleware(ExecutionPermissionPolicy(
            task_id='scheduled', approval_profile='unattended')),
            _ExecutionConfiguration('unattended', '/workspace')])
    events = await collect(engine, UserMsg(name='user', content='do work'))
    assert tool.calls == 0
    denial_text = ''.join(event.delta for event in events if event.type == 'TOOL_RESULT_TEXT_DELTA')
    assert ('Safety: dangerous operation' if name == 'write_record'
            else 'explicit resource submission confirmation') in denial_text
    assert not any(event.type == 'REQUIRE_USER_CONFIRM' for event in events)
    assert any(event.type == 'TOOL_RESULT_END' and event.state == 'denied' for event in events)
    assert events[-1].finished_reason == 'completed'
    assert engine.agent.state.permission_context.mode is PermissionMode.DONT_ASK


async def test_unattended_cannot_match_full_access_upgrade():
    from workmate_runtime.transport.full_access import full_access_matches
    assert not await full_access_matches(None, None, None, None,
        {'approval_profile': 'unattended'})
