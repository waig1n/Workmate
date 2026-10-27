"""Product knowledge HTTP schemas and shared configuration exports."""
from pydantic import BaseModel, ConfigDict, Field
from workmate_product.config.knowledge import KnowledgeConfig, KnowledgeParameters  # noqa: F401


class KnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    scope_id: str | None = None


class KnowledgeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class KnowledgeSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)
