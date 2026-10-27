"""Direct Core execution boundary; persistence and distributed ownership live outside."""
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any, Literal

from agentscope.agent import Agent
from agentscope.event import (
    AgentEvent, ExternalExecutionResultEvent, UserConfirmResultEvent, UserInterruptEvent,
)
from agentscope.message import Msg
from agentscope.state import AgentState
from pydantic import BaseModel, ConfigDict
from workmate_contracts import BoundaryEvidence, PendingToolCall, snapshot_digest


class CoreSnapshot(BaseModel):
    """Initial persisted state format. Unknown versions require explicit migration.

    Only Agent state is captured. Model/tool/middleware configuration and workspace
    references must be reconstructed by the caller from trusted product data.
    """

    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1
    engine_version: Literal["2.0.7"] = "2.0.7"
    state: dict[str, Any]

    def to_state(self) -> AgentState:
        """Validate and rebuild before constructing Agent (including its permission engine)."""
        return AgentState.model_validate(self.state)


    def boundary_evidence(self, agent_name: str) -> BoundaryEvidence:
        """Summarize pending calls from this exact complete, version-checked state."""
        calls = self.to_state().get_awaiting_tool_calls(agent_name)
        return BoundaryEvidence(snapshot_sha256=snapshot_digest(self.model_dump(mode='json')),
            pending_tools=[PendingToolCall(id=call.id,name=call.name,input=call.input,
                kind={'asking':'permission','submitted':'external_execution'}[call.state])
                for call in calls])


class CoreEngine:
    """Consume one Core reply at a time without AgentScope App services.

    The Worker owns this iterator's task and must drain it after cancellation to
    capture Core's INTERRUPTED end event. A stop request alone is not completion.
    This local guard is not a substitute for a durable Worker lease/fencing token.
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._active = False
        self._incomplete = False

    async def reply_stream(
        self,
        inputs: Msg | list[Msg] | UserConfirmResultEvent | UserInterruptEvent
        | ExternalExecutionResultEvent | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if self._active:
            raise RuntimeError("Core execution is active")
        if self._incomplete:
            raise RuntimeError("Core execution is incomplete; reconstruct from durable state")
        self._active = True
        self._incomplete = True
        try:
            async with aclosing(self.agent.reply_stream(inputs)) as stream:
                async for event in stream:
                    yield event
            self._incomplete = False
        finally:
            self._active = False

    def snapshot(self) -> CoreSnapshot:
        """Capture only a drained/parked boundary, never a half-mutated execution."""
        if self._active:
            raise RuntimeError("Cannot snapshot an active Core execution")
        if self._incomplete:
            raise RuntimeError("Cannot snapshot an incomplete Core execution")
        return CoreSnapshot(state=self.agent.state.model_dump(mode="json"))
