# -*- coding: utf-8 -*-
"""API request/response schemas."""

from .knowledge_schemas import KnowledgeConfig

from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from workmate_product.persistence.models import (
    ApprovalProfile,
    CapabilityType,
    ScopeKind,
    TaskStatus,
    UserStatus,
    Visibility,
)


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str = Field(min_length=1, max_length=100)
    organization_name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("organization_name", mode="before")
    @classmethod
    def trim_organization_name(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value.casefold()) > 200:
                raise ValueError("Organization name is too long after normalization.")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: str
    status: UserStatus = UserStatus.ACTIVE
    must_change_password: bool = False


class UserUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


# ----------------------------------------------------------------------
# Admin user management (plan §5.2)
# ----------------------------------------------------------------------

class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    access_version: int = 1
    profile_version: int = 1
    departments: list[dict] = Field(default_factory=list)
    roles: list[dict] = Field(default_factory=list)
    email: str
    display_name: str
    status: UserStatus = UserStatus.ACTIVE
    must_change_password: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    disabled_at: datetime | None = None


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    next_cursor: str | None = None


class AdminUserCreateRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    initial_password: str = Field(min_length=8)
    must_change_password: bool = True
    membership_scope_ids: list[str] = Field(default_factory=list)
    role_bindings: list["AdminRoleBindingIn"] = Field(default_factory=list)


class AdminUserUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    email: EmailStr | None = None
    display_name: str | None = Field(
        default=None, min_length=1, max_length=100,
    )


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8)
    must_change_password: bool = True


class AdminRoleBindingOut(BaseModel):
    scope_id: str
    role_key: str
    include_descendants: bool


class AdminRoleBindingIn(BaseModel):
    scope_id: str
    role_key: str
    include_descendants: bool = False


class AdminAccessRequest(BaseModel):
    expected_version: int = Field(ge=1)
    membership_scope_ids: list[str]
    role_bindings: list[AdminRoleBindingIn] | None = None
    retain_departure_roles: bool = False


class AdminUserDetail(AdminUserOut):
    memberships: list[str] = Field(default_factory=list)
    role_bindings: list[AdminRoleBindingOut] = Field(default_factory=list)
    root_scope_id: str | None = None


# ----------------------------------------------------------------------
# Admin role and permission management (plan §5.3)
# ----------------------------------------------------------------------

class AdminPermissionCreateRequest(BaseModel):
    key: str = Field(
        min_length=2, max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{1,63}$",
    )
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class AdminPermissionUpdateRequest(BaseModel):
    display_name: str | None = Field(
        default=None, min_length=1, max_length=100,
    )
    description: str | None = Field(default=None, max_length=500)


class AdminPermissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    display_name: str
    description: str
    is_system: bool
    has_policy_consumer: bool = False
    enforcement: Literal["rbac", "visibility", "unimplemented"] = "unimplemented"
    applicable_scope: Literal["organization", "all"] = "all"
    # Number of roles currently granting this permission (plan §5.3
    # reference count).
    role_count: int = 0
    created_at: datetime


class AdminRoleCreateRequest(BaseModel):
    delegatable: bool = False
    key: str = Field(
        min_length=2, max_length=64,
        pattern=r"^[a-z][a-z0-9_.:-]{1,63}$",
    )
    display_name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    permission_keys: list[str] = Field(default_factory=list)


class AdminRoleUpdateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    permission_keys: list[str] | None = None
    delegatable: bool | None = None
    display_name: str | None = Field(
        default=None, min_length=1, max_length=100,
    )
    description: str | None = Field(default=None, max_length=500)


class AdminRolePermissionsRequest(BaseModel):
    expected_version: int = Field(ge=1)
    permission_keys: list[str]


class AdminRoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int = 1
    delegatable: bool = False
    user_count: int = 0
    key: str
    display_name: str
    description: str
    is_system: bool
    permission_keys: list[str] = Field(default_factory=list)
    binding_count: int = 0
    created_at: datetime
    updated_at: datetime


class PermissionsOut(BaseModel):
    """Effective RBAC facts for the current user (RBAC design §11.2)."""

    permissions: list[str]
    root_permissions: list[str] = Field(default_factory=list)
    managed_scope_ids: list[str] = Field(default_factory=list)
    personal_scope_id: str | None
    root_scope_id: str | None
    is_root_admin: bool


class RegistrationStatusOut(BaseModel):
    """Public self-registration availability (user management plan §3.1).

    Exposes only registration availability and whether first-account setup
    is required; never returns account counts, emails, or configured mode.
    """

    registration_available: bool
    bootstrap_required: bool


# ----------------------------------------------------------------------
# Scopes
# ----------------------------------------------------------------------

class ScopeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: ScopeKind
    name: str
    parent_id: str | None
    # Effective for the requesting user in this exact scope.  This is
    # populated by request-aware view builders, not ORM validation.
    permissions: list[str] = Field(default_factory=list)


class AdminScopeCreateRequest(BaseModel):
    parent_id: str
    name: str = Field(min_length=1, max_length=200)


class AdminScopeUpdateRequest(BaseModel):
    expected_tree_version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)


class AdminScopeMoveRequest(BaseModel):
    expected_tree_version: int = Field(ge=1)
    new_parent_id: str


class AdminScopeTreeNode(ScopeOut):
    tree_version: int = 1
    children: list["AdminScopeTreeNode"] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Agents (RBAC design §6/§11.1)
# ----------------------------------------------------------------------

AgentDeniedToolGroup = Literal[
    "shell",
    "workspace_write",
    "external_connectors",
    "artifact_delivery",
    "agent_management",
    "skill_management",
    "web_access",
]

PlatformToolProfile = Literal["none", "workspace"]


class AgentToolPolicy(BaseModel):
    """Hard tool ceiling that no Task approval profile may override."""

    denied_groups: list[AgentDeniedToolGroup] = Field(
        default_factory=list, max_length=7,
    )
    denied_tools: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("denied_groups")
    @classmethod
    def unique_groups(cls, value):
        return list(dict.fromkeys(value))

    @field_validator("denied_tools")
    @classmethod
    def valid_tool_names(cls, value):
        normalized = []
        for item in value:
            name = item.strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name):
                raise ValueError("denied tool names must be stable tool identifiers")
            if name.casefold() not in {current.casefold() for current in normalized}:
                normalized.append(name)
        return normalized

class AgentCreate(BaseModel):
    knowledge_config: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    description: str = Field(default="", max_length=2000)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(min_length=1, max_length=100)
    system_prompt: str = ""
    tool_policy: AgentToolPolicy = Field(default_factory=AgentToolPolicy)
    platform_tool_profile: PlatformToolProfile = "none"
    # Defaults to the user's personal scope (RBAC §11.1).
    scope_id: str | None = None
    visibility: Visibility = Visibility.PRIVATE
    include_descendants: bool = False
    skill_binding_ids: list[str] = Field(default_factory=list)
    mcp_binding_ids: list[str] = Field(default_factory=list)
    is_default: bool = False

    @model_validator(mode="after")
    def supply_description(self) -> "AgentCreate":
        if not self.description.strip():
            self.description = f"{self.display_name}，根据你的需求提供任务协助。"
        return self


class AgentUpdate(BaseModel):
    knowledge_config: KnowledgeConfig | None = None
    sharing_confirmation: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    display_name: str | None = None
    system_prompt: str | None = None
    tool_policy: AgentToolPolicy | None = None
    platform_tool_profile: PlatformToolProfile | None = None
    # Changing visibility private->public publishes (needs agent.publish).
    visibility: Visibility | None = None
    include_descendants: bool | None = None
    skill_binding_ids: list[str] | None = None
    mcp_binding_ids: list[str] | None = None
    is_default: bool | None = None


class AgentActions(BaseModel):
    use: bool = False
    update: bool = False
    delete: bool = False
    publish: bool = False


class AgentBindingIssue(BaseModel):
    id: str
    kind: Literal["skill", "mcp", "knowledge"]
    reason: Literal["missing", "disabled", "unshared", "unready", "service_disabled"]


class AgentOut(BaseModel):
    binding_issues: list[AgentBindingIssue] | None = None
    knowledge_config: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    unavailable_knowledge_base_ids: list[str] = Field(default_factory=list)
    description: str = ""
    id: str
    slug: str
    display_name: str
    system_prompt: str
    tool_policy: AgentToolPolicy
    system_key: str | None
    platform_tool_profile: PlatformToolProfile
    scope_id: str
    scope: ScopeOut
    visibility: Visibility
    include_descendants: bool
    created_by: str | None
    skill_binding_ids: list[str]
    mcp_binding_ids: list[str]
    unavailable_capability_ids: list[str]
    actions: AgentActions
    # Resolved for the requesting user (RBAC §6.1).
    is_default: bool
    created_at: datetime
    updated_at: datetime


# ----------------------------------------------------------------------
# Capabilities (RBAC design §8/§11.3)
# ----------------------------------------------------------------------

class CapabilityOut(BaseModel):
    """Sanitized installation view - MCP configs never leave here."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    capability_type: CapabilityType
    scope_id: str
    name: str
    display_name: str | None
    enabled: bool
    metadata: dict = Field(default={}, validation_alias="metadata_")


class CapabilityListItem(CapabilityOut):
    """List view with per-user manage rights and scope context."""

    manageable: bool
    usable: bool
    scope_kind: ScopeKind | None = None


class SkillInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False

    hub_id: str = Field(min_length=1, max_length=64)
    card_id: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    scope_id: str | None = None


class MCPInstallRequest(BaseModel):
    hub_id: str = Field(min_length=1, max_length=64)
    card_id: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    values: dict = Field(default_factory=dict)
    scope_id: str


class MCPUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    values: dict | None = None
    enabled: bool | None = None


# ----------------------------------------------------------------------
# Conversations
# ----------------------------------------------------------------------

class ConversationCreate(BaseModel):
    agent_id: str
    title: str = Field(default="New conversation", min_length=1, max_length=255)
    # Profile name from workmate.yaml; None = the deployment default.
    model_profile: str | None = Field(default=None, max_length=64)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ConversationModelUpdate(BaseModel):
    """Switch a conversation's chat model (rejected while a task runs)."""
    model_profile: str | None = Field(default=None, max_length=64)


class ModelProfileOut(BaseModel):
    """Selectable chat model profile (product-facing projection).

    Physical deployment details (provider model id, provider type,
    context size) stay internal — users see product profiles only.
    ``available`` is false when the profile's key/URL configuration is
    incomplete in this deployment.
    """
    id: str
    display_name: str
    media_profile: str
    supports_multimodal_input: bool
    supports_images: bool
    supports_audio: bool
    supports_video: bool
    supports_documents: bool
    supports_native_pdf: bool
    supports_thinking: bool
    is_default: bool
    available: bool


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    title: str
    # Revocation semantics (RBAC design §10): history stays readable.
    read_only: bool
    read_only_reason: str | None
    # Selected chat model profile (None = deployment default).
    model_profile: str | None
    created_at: datetime
    updated_at: datetime
    # Hidden after authorization loss so history access cannot disclose the
    # Agent's current mutable prompt, bindings, scope or creator.
    agent: AgentOut | None


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    role: str
    content: dict
    created_at: datetime


# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------

class TaskCreate(BaseModel):
    message: str = Field(min_length=1)
    title: str | None = None
    approval_profile: Literal[ApprovalProfile.STANDARD, ApprovalProfile.STRICT,
        ApprovalProfile.READ_ONLY, ApprovalProfile.FULL_ACCESS] = ApprovalProfile.STANDARD


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    title: str
    status: TaskStatus
    execution_phase: Literal["queued", "preparing", "running"] | None = None
    run_id: str | None
    approval_profile: ApprovalProfile
    origin: Literal['user', 'schedule', 'channel'] = 'user'
    schedule_occurrence_id: str | None = None
    error: dict | None
    duration_ms: int | None
    steps: int | None
    model_name: str | None
    token_usage: dict | None
    created_at: datetime
    updated_at: datetime


class TaskCreatedOut(BaseModel):
    task: TaskOut
    queued: bool = False


class ConfirmRequest(BaseModel):
    """HITL decision for one pending tool call (design §18).

    Only ``tool_call_id`` + ``approved`` are required; the original
    tool-call block is recovered from the pending-confirm state.
    """

    approved: bool
    tool_call_id: str
    tool_call_name: str = ""
    tool_call_input: dict = Field(default_factory=dict)


class ConfirmBatchRequest(BaseModel):
    """Versioned, idempotent HITL decision batch."""

    decisions: list[ConfirmRequest] = Field(min_length=1)
    decision_version: str = Field(min_length=1, max_length=128)


class PendingConfirmOut(BaseModel):
    """Approval card rebuilt from the audit log (restart-safe).

    Carries the same camelCase shape the SSE ``HITL_APPROVAL_REQUEST``
    event uses, and the same ``decisionVersion`` the confirm endpoint
    expects, so a hydrated card is indistinguishable from a live one.
    """

    toolCalls: list[dict] = Field(min_length=1)
    decisionVersion: str
    taskId: str
    # Task-frozen approval profile + its AgentScope mode, so the card can
    # explain why this call needed confirmation (audit doc §13.1).
    approvalProfile: str = "standard"
    permissionMode: str = "accept_edits"


# ----------------------------------------------------------------------
# Artifacts / events
# ----------------------------------------------------------------------

class ArtifactOut(BaseModel):
    """Delivery view: task_id/created_at describe this delivery; id/version the content."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    conversation_id: str
    name: str
    mime_type: str
    size: int
    version: int
    change_type: str
    metadata: dict = Field(default={}, validation_alias="metadata_")
    created_at: datetime


class TaskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    seq: int
    event_type: str
    payload: dict
    created_at: datetime


TokenResponse.model_rebuild()
ConversationOut.model_rebuild()
