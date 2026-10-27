"""Executable contracts against real AgentScope Core; no App or network calls."""
import asyncio
import json

import pytest
from pydantic import BaseModel
from agentscope.agent import Agent, InjectionConfig
from agentscope.credential import CredentialBase
from agentscope.event import ConfirmResult, UserConfirmResultEvent, UserInterruptEvent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionRule
from agentscope.state import AgentState
from agentscope.tool import ToolBase, ToolChunk, Toolkit

from workmate_runtime.engines.agentscope import CoreEngine, CoreSnapshot


class Parameters(BaseModel):
    pass


class ScriptModel(ChatModelBase):
    def __init__(self, responses, entered=None):
        super().__init__(CredentialBase(), "deterministic", Parameters(), stream=True)
        self.formatter = OpenAIChatFormatter()
        self.responses = iter(responses)
        self.entered = entered

    async def _call_api(self, *args, **kwargs):
        async def stream():
            if self.entered:
                self.entered.set()
                await asyncio.Event().wait()
            yield ChatResponse(content=next(self.responses), is_last=True)
        return stream()


class ConfirmTool(ToolBase):
    name: str = "write_record"
    description: str = "Write a test record after approval."
    input_schema: dict = {"type": "object", "properties": {}}
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    is_external_tool: bool = False
    is_mcp: bool = False

    def __init__(self):
        self.calls = 0

    async def check_permissions(self, tool_input, context):
        return PermissionDecision(
            behavior=PermissionBehavior.ASK, message="Confirm write",
            bypass_immune=getattr(self, "safety_ask", False),
        )

    async def __call__(self, **kwargs):
        self.calls += 1
        yield ToolChunk(content=[TextBlock(text="written")], is_last=True)


def make_engine(responses, *, state=None, tool=None, entered=None, middlewares=None):
    return CoreEngine(Agent(
        name="test", system_prompt="Test", model=ScriptModel(responses, entered),
        state=state or AgentState(session_id="session-1"),
        toolkit=Toolkit(tools=[tool] if tool else []),
        middlewares=middlewares,
        injection_config=InjectionConfig(inject_runtime_state=False),
    ))


async def collect(engine, inputs):
    return [event async for event in engine.reply_stream(inputs)]


async def park():
    tool = ConfirmTool()
    engine = make_engine([[ToolCallBlock(id="call-1", name=tool.name, input="{}")]], tool=tool)
    events = await collect(engine, UserMsg(name="user", content="write"))
    required = next(e for e in events if e.type == "REQUIRE_USER_CONFIRM")
    assert tool.calls == 0
    return engine, required


async def test_reply_and_json_state_round_trip():
    engine = make_engine([[TextBlock(text="first")]])
    events = await collect(engine, UserMsg(name="user", content="hello"))
    assert events[0].type == "REPLY_START"
    assert events[-1].type == "REPLY_END"
    snapshot = CoreSnapshot.model_validate_json(engine.snapshot().model_dump_json())
    restored = make_engine([[TextBlock(text="second")]], state=snapshot.to_state())
    await collect(restored, UserMsg(name="user", content="continue"))
    dumped = json.dumps(restored.snapshot().state)
    assert "first" in dumped and "second" in dumped and "hello" in dumped


async def test_approval_resumes_same_reply_after_state_round_trip():
    engine, required = await park()
    tool = ConfirmTool()
    restored = make_engine([[TextBlock(text="done")]], state=engine.snapshot().to_state(), tool=tool)
    events = await collect(restored, UserConfirmResultEvent(
        reply_id=required.reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=required.tool_calls[0])],
    ))
    assert tool.calls == 1
    assert events[-1].type == "REPLY_END"
    assert all(e.reply_id == required.reply_id for e in events)


async def test_parked_interrupt_does_not_execute_tool():
    engine, required = await park()
    tool = ConfirmTool()
    restored = make_engine([], state=engine.snapshot().to_state(), tool=tool)
    events = await collect(restored, UserInterruptEvent(reply_id=required.reply_id))
    assert tool.calls == 0
    assert events[-1].type == "REPLY_END"
    assert events[-1].finished_reason == "interrupted"


async def test_running_cancel_drains_interrupted_end_and_rejects_snapshot():
    entered = asyncio.Event()
    engine = make_engine([], entered=entered)
    task = asyncio.create_task(collect(engine, UserMsg(name="user", content="wait")))
    await asyncio.wait_for(entered.wait(), timeout=3)
    with pytest.raises(RuntimeError, match="active"):
        engine.snapshot()
    with pytest.raises(RuntimeError, match="active"):
        await collect(engine, UserMsg(name="user", content="duplicate"))
    task.cancel()
    events = await asyncio.wait_for(task, timeout=3)
    assert events[-1].type == "REPLY_END"
    assert events[-1].finished_reason == "interrupted"
    assert engine.snapshot().state


@pytest.mark.parametrize("field,value", [("format_version", 2), ("engine_version", "9.0")])
async def test_unknown_snapshot_versions_fail_closed(field, value):
    data = make_engine([]).snapshot().model_dump()
    data[field] = value
    with pytest.raises(ValueError):
        CoreSnapshot.model_validate(data)


async def test_denial_resumes_without_business_write():
    engine, required = await park()
    tool = ConfirmTool()
    restored = make_engine([[TextBlock(text="declined")]], state=engine.snapshot().to_state(), tool=tool)
    events = await collect(restored, UserConfirmResultEvent(
        reply_id=required.reply_id,
        confirm_results=[ConfirmResult(confirmed=False, tool_call=required.tool_calls[0])],
    ))
    assert tool.calls == 0
    assert events[-1].finished_reason == "completed"
    assert events[-1].reply_id == required.reply_id


def test_core_import_does_not_load_app_services():
    import subprocess
    import sys

    probe = """
import importlib.abc
import sys
class RejectApp(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agentscope.app' or fullname.startswith('agentscope.app.'):
            raise AssertionError('App dependency: ' + fullname)
sys.meta_path.insert(0, RejectApp())
from workmate_runtime.engines.agentscope import CoreEngine, CoreSnapshot
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


async def test_early_stream_close_forbids_snapshot_and_reuse():
    engine = make_engine([[TextBlock(text="unfinished")]])
    stream = engine.reply_stream(UserMsg(name="user", content="start"))
    assert (await anext(stream)).type == "REPLY_START"
    await stream.aclose()
    with pytest.raises(RuntimeError, match="incomplete"):
        engine.snapshot()
    with pytest.raises(RuntimeError, match="incomplete"):
        await collect(engine, UserMsg(name="user", content="next"))


async def test_consumer_failure_forbids_partial_snapshot():
    engine = make_engine([[TextBlock(text="unfinished")]])
    stream = engine.reply_stream(UserMsg(name="user", content="start"))
    await anext(stream)
    with pytest.raises(ValueError, match="storage failed"):
        await stream.athrow(ValueError("storage failed"))
    with pytest.raises(RuntimeError, match="incomplete"):
        engine.snapshot()



def json_state(engine):
    return CoreSnapshot.model_validate_json(engine.snapshot().model_dump_json()).to_state()


async def test_multiple_approval_rounds_preserve_reply_across_json_restore():
    engine, first = await park()
    tool = ConfirmTool()
    second_engine = make_engine(
        [[ToolCallBlock(id="call-2", name=tool.name, input="{}")]],
        state=json_state(engine), tool=tool,
    )
    second_events = await collect(second_engine, UserConfirmResultEvent(
        reply_id=first.reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=first.tool_calls[0])],
    ))
    second = next(e for e in second_events if e.type == "REQUIRE_USER_CONFIRM")
    assert second.reply_id == first.reply_id
    assert second.tool_calls[0].id == "call-2"
    assert tool.calls == 1
    assert not any(e.type == "REPLY_END" for e in second_events)
    restored_tool = ConfirmTool()
    third_engine = make_engine([[TextBlock(text="both written")]],
                               state=json_state(second_engine), tool=restored_tool)
    final_events = await collect(third_engine, UserConfirmResultEvent(
        reply_id=second.reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=second.tool_calls[0])],
    ))
    assert restored_tool.calls == 1
    assert final_events[-1].finished_reason == "completed"
    assert all(e.reply_id == first.reply_id for e in final_events)


async def test_partial_confirmation_preserves_unanswered_call_across_json_restore():
    tool = ConfirmTool()
    tool.is_concurrency_safe = True
    # Safety ASK prompts cannot be deduplicated by batch allow suggestions.
    tool.safety_ask = True
    engine = make_engine([[
        ToolCallBlock(id="call-1", name=tool.name, input="{}"),
        ToolCallBlock(id="call-2", name=tool.name, input="{}"),
    ]], tool=tool)
    events = await collect(engine, UserMsg(name="user", content="write twice"))
    requests = {call.id: call for event in events
                if event.type == "REQUIRE_USER_CONFIRM" for call in event.tool_calls}
    assert set(requests) == {"call-1", "call-2"}
    reply_id = engine.agent.state.reply_id
    partial_tool = ConfirmTool()
    partial_tool.is_concurrency_safe = True
    partial_tool.safety_ask = True
    partial = make_engine([], state=json_state(engine), tool=partial_tool)
    partial_events = await collect(partial, UserConfirmResultEvent(
        reply_id=reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=requests["call-1"])],
    ))
    assert partial_tool.calls == 1
    assert not any(e.type in {"REPLY_END", "REQUIRE_USER_CONFIRM"} for e in partial_events)
    state = json_state(partial)
    pending = state.get_awaiting_tool_calls("test")
    evidence = partial.snapshot().boundary_evidence("test")
    assert [(call.id, call.kind, call.input) for call in evidence.pending_tools] == [("call-2", "permission", "{}")]
    assert [(call.id, call.state) for call in pending] == [("call-2", "asking")]
    final_tool = ConfirmTool()
    final_tool.is_concurrency_safe = True
    final_tool.safety_ask = True
    final = make_engine([[TextBlock(text="both written")]], state=state, tool=final_tool)
    final_events = await collect(final, UserConfirmResultEvent(
        reply_id=reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=requests["call-2"])],
    ))
    assert final_tool.calls == 1
    assert final_events[-1].finished_reason == "completed"
    assert final_events[-1].reply_id == reply_id


async def test_current_product_deny_overrides_restored_allow_and_confirmation():
    from workmate_runtime.permission_policy import ExecutionPermissionPolicy, ProductPermissionMiddleware

    engine, required = await park()
    state = json_state(engine)
    state.permission_context.allow_rules["write_record"] = [PermissionRule(
        tool_name="write_record", rule_content=None, behavior=PermissionBehavior.ALLOW,
        source="old-user-confirmation",
    )]
    tool = ConfirmTool()
    current_policy = ExecutionPermissionPolicy(
        task_id="task-1", approval_profile="full_access", denied_tools=[tool.name],
    )
    restored = make_engine([[TextBlock(text="now denied")]], state=state, tool=tool,
                           middlewares=[ProductPermissionMiddleware(current_policy)])
    events = await collect(restored, UserConfirmResultEvent(
        reply_id=required.reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=required.tool_calls[0])],
    ))
    assert tool.calls == 0
    assert any(e.type == "TOOL_RESULT_END" and e.state == "denied" for e in events)
    assert events[-1].finished_reason == "completed"
    assert events[-1].reply_id == required.reply_id


def load_golden_snapshot(name):
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "core_state_v1" / f"{name}.json"
    return CoreSnapshot.model_validate_json(path.read_text())


async def test_golden_normal_snapshot_preserves_context_for_next_reply():
    snapshot = load_golden_snapshot("normal")
    assert snapshot.format_version == 1 and snapshot.engine_version == "2.0.7"
    state = snapshot.to_state()
    assert state.reply_id == "cd915b9fc23e43af8455763482da8ee4"
    assert state.session_id == "session-1"
    original_context = [message.model_dump(mode="json") for message in state.context]
    assert "Golden first question" in json.dumps(original_context)
    assert "Golden first answer" in json.dumps(original_context)
    engine = make_engine([[TextBlock(text="Golden next answer")]], state=state)
    events = await collect(engine, UserMsg(name="user", content="Golden next question"))
    assert events[-1].finished_reason == "completed"
    assert events[-1].reply_id != snapshot.to_state().reply_id
    assert engine.snapshot().state["context"][:len(original_context)] == original_context


@pytest.mark.parametrize("name,reply_id,call_id", [
    ("parked", "764b311a65f84d36a730e7f3b32711a8", "call-1"),
    ("partial", "810d39a48aba4942b7405578b90657ec", "call-2"),
])
async def test_golden_pending_snapshot_resumes_only_unanswered_tool(name, reply_id, call_id):
    snapshot = load_golden_snapshot(name)
    state = snapshot.to_state()
    pending = state.get_awaiting_tool_calls("test")
    assert state.reply_id == reply_id
    assert [(call.id, call.state) for call in pending] == [(call_id, "asking")]
    if name == "partial":
        assert "written" in json.dumps(snapshot.state)
    tool = ConfirmTool()
    tool.is_concurrency_safe = name == "partial"
    tool.safety_ask = name == "partial"
    engine = make_engine([[TextBlock(text="Golden resumed")]], state=state, tool=tool)
    events = await collect(engine, UserConfirmResultEvent(
        reply_id=reply_id,
        confirm_results=[ConfirmResult(confirmed=True, tool_call=pending[0])],
    ))
    assert tool.calls == 1
    assert events[-1].reply_id == reply_id
    assert events[-1].finished_reason == "completed"
    assert {event.tool_call_id for event in events if event.type == "TOOL_RESULT_END"} == {call_id}
    assert engine.snapshot().to_state().get_awaiting_tool_calls("test") == []


@pytest.mark.parametrize("name", ["normal", "parked", "partial"])
@pytest.mark.parametrize("field,value", [("format_version", 2), ("engine_version", "2.0.8")])
def test_golden_snapshot_version_gate_rejects_unknown_formats(name, field, value):
    payload = load_golden_snapshot(name).model_dump()
    payload[field] = value
    with pytest.raises(ValueError):
        CoreSnapshot.model_validate(payload)


@pytest.mark.parametrize('name,ids',[('normal',[]),('parked',['call-1']),('partial',['call-2'])])
def test_boundary_evidence_reads_full_restored_core_state(name,ids):
    from workmate_contracts import snapshot_digest
    snapshot=load_golden_snapshot(name)
    evidence=snapshot.boundary_evidence('test')
    assert [call.id for call in evidence.pending_tools]==ids
    assert all(call.kind=='permission' for call in evidence.pending_tools)
    assert evidence.snapshot_sha256==snapshot_digest(snapshot.model_dump(mode='json'))


async def test_boundary_evidence_distinguishes_real_external_execution():
    tool=ConfirmTool()
    tool.is_external_tool=True
    engine=make_engine([[ToolCallBlock(id='external',name=tool.name,input='{}')]],tool=tool)
    events=await collect(engine,UserMsg(name='user',content='external write'))
    request=next(event for event in events if event.type=='REQUIRE_USER_CONFIRM')
    resumed=make_engine([],state=json_state(engine),tool=tool)
    events=await collect(resumed,UserConfirmResultEvent(reply_id=request.reply_id,
        confirm_results=[ConfirmResult(confirmed=True,tool_call=request.tool_calls[0])]))
    assert any(event.type=='REQUIRE_EXTERNAL_EXECUTION' for event in events)
    assert tool.calls==0
    evidence=resumed.snapshot().boundary_evidence('test')
    assert [(call.id,call.kind) for call in evidence.pending_tools]==[('external','external_execution')]
