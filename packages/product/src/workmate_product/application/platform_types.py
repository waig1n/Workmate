"""Product platform tool transport-independent errors and strict inputs."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from workmate_product.persistence.models import Visibility

class PlatformToolRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True,revalidate_instances='always')
    actor_user_id:str=Field(min_length=1,max_length=128)
    agent_id:str=Field(min_length=1,max_length=128)
    conversation_id:str=Field(min_length=1,max_length=128)
    session_id:str=Field(min_length=1,max_length=128)
    task_id:str=Field(min_length=1,max_length=128)
    worker_id:str=Field(min_length=1,max_length=128)
    storage_node_id:str=Field(min_length=1,max_length=128)
    execution_epoch:int=Field(ge=1)
    reply_id:str=Field(min_length=1,max_length=128)
    tool_call_id:str=Field(min_length=1,max_length=128)
    tool_name:str=Field(min_length=1,max_length=64)
    arguments:dict=Field(default_factory=dict)


class PlatformToolError(ValueError):
    def __init__(self,status_code,detail):
        self.status_code=status_code
        self.detail=detail
        super().__init__(str(detail))

class _StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')


class _EmptyArgs(_StrictModel):
    pass


class _CapabilityListArgs(_StrictModel):
    scope_id: str | None = None
    agent_id: str | None = None


class _AgentListArgs(_StrictModel):
    scope: Literal["all", "personal", "organization"] = "all"


class _AgentGetArgs(_StrictModel):
    agent_id: str


class _AgentValidateArgs(_StrictModel):
    operation: Literal["create", "update"]
    draft: dict
    agent_id: str | None = None
    expected_updated_at: str | None = None


class _AgentCreateArgs(_StrictModel):
    description: str = Field(default="", max_length=2000)
    slug: str
    display_name: str
    system_prompt: str = ""
    scope_id: str | None = None
    visibility: Visibility = Visibility.PRIVATE
    include_descendants: bool = True
    skill_binding_ids: list[str] = Field(default_factory=list)
    mcp_binding_ids: list[str] = Field(default_factory=list)
    is_default: bool = False


class _AgentUpdateArgs(_StrictModel):
    description: str | None = Field(default=None, max_length=2000)
    agent_id: str
    expected_updated_at: str
    display_name: str | None = None
    system_prompt: str | None = None
    visibility: Visibility | None = None
    include_descendants: bool | None = None
    skill_binding_ids: list[str] | None = None
    mcp_binding_ids: list[str] | None = None
    is_default: bool | None = None


class _SkillInstallArgs(_StrictModel):
    scope_id: str | None = None
    filename: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}\.zip$")
    archive_base64: str
    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    display_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
