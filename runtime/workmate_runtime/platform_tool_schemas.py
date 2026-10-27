"""Pure model-facing platform tool inputs, used by the Worker adapter.

No transport, execution policy registry or AgentScope App dependency belongs here.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyInput(_StrictModel):
    pass


class CapabilityListInput(_StrictModel):
    scope_id: str | None = None
    agent_id: str | None = None


class AgentListInput(_StrictModel):
    scope: Literal["all", "personal", "organization"] = "all"


class AgentGetInput(_StrictModel):
    agent_id: str


class AgentValidateInput(_StrictModel):
    operation: Literal["create", "update"]
    draft: dict
    agent_id: str | None = None
    expected_updated_at: str | None = None


class AgentCreateInput(_StrictModel):
    description: str = Field(
        default="", max_length=2000,
        description="Public summary of intended users and tasks; provide a non-empty description, not internal prompt text.",
    )
    slug: str
    display_name: str
    system_prompt: str = ""
    scope_id: str | None = None
    visibility: Literal["private", "public"] = "private"
    include_descendants: bool = True
    skill_binding_ids: list[str] = Field(default_factory=list)
    mcp_binding_ids: list[str] = Field(default_factory=list)
    is_default: bool = False


class AgentUpdateInput(_StrictModel):
    description: str | None = Field(default=None, max_length=2000)
    agent_id: str
    expected_updated_at: str
    display_name: str | None = None
    system_prompt: str | None = None
    visibility: Literal["private", "public"] | None = None
    include_descendants: bool | None = None
    skill_binding_ids: list[str] | None = None
    mcp_binding_ids: list[str] | None = None
    is_default: bool | None = None


class SkillValidateInput(_StrictModel):
    skill_directory: str


class SkillInstallInput(_StrictModel):
    skill_directory: str
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_id: str | None = None
    display_name: str | None = None
    description: str | None = None
