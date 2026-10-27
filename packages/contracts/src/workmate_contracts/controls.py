"""Strict payloads for trusted Product-to-Worker control commands."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workmate_contracts import Identity


class ControlPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class ToolDecision(ControlPayload):
    # Original names, inputs and permission rules must come from saved state.
    tool_call_id: Identity
    approved: bool


class ResumePayload(ControlPayload):
    decision_version: Identity
    snapshot_id: Identity
    state_version: Annotated[int, Field(ge=1)]
    decisions: list[ToolDecision] = Field(min_length=1)
    full_access: bool = False

    @model_validator(mode='after')
    def unique_calls(self):
        ids = [item.tool_call_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError('Decision tool call identities must be unique')
        return self


class StopPayload(ControlPayload):
    reason: Literal['user_requested', 'account_disabled', 'approval_expired'] = 'user_requested'
