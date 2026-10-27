"""Shared Product knowledge configuration, independent of Runtime implementation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KnowledgeParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["agentic", "static"] = "agentic"
    top_k: int = Field(default=5, ge=1, le=50)
    score_threshold: float | None = Field(default=None, allow_inf_nan=False)


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    knowledge_base_ids: list[str] = Field(default_factory=list, max_length=20)
    parameters: KnowledgeParameters = Field(default_factory=KnowledgeParameters)

    @field_validator("knowledge_base_ids")
    @classmethod
    def unique_ids(cls, value):
        if any(not item or len(item) > 64 for item in value) or len(set(value)) != len(
            value
        ):
            raise ValueError("Knowledge base IDs must be nonempty and unique")
        return value


