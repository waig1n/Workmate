// workmate frontend types - mirror of backend/app/api/schemas.py

export interface User {
  id: string;
  email: string;
  display_name: string;
  status: "active" | "disabled";
  must_change_password: boolean;
}

// GET /auth/me/permissions (RBAC design §11.2)
export interface Permissions {
  permissions: string[];
  personal_scope_id: string | null;
  root_scope_id: string | null;
  is_root_admin: boolean;
  root_permissions?: string[];
  managed_scope_ids?: string[];
}

// Public registration policy and first-time setup state; no tenant details.
export interface RegistrationStatus {
  registration_available: boolean;
  bootstrap_required: boolean;
}

export interface OrganizationSettings { id: string; name: string; tree_version: number; }

export type ScopeKind = "personal" | "organization" | "department";

export interface Scope {
  id: string;
  kind: ScopeKind;
  name: string;
  parent_id: string | null;
  permissions: string[];
}

export interface AdminScopeNode extends Scope {
  tree_version?: number;
  children: AdminScopeNode[];
}

export interface AdminUser extends User {
  access_version: number;
  profile_version: number;
  departments: { id: string; name: string }[];
  roles: { role_key: string; role_name: string; scope_id: string; scope_name: string; include_descendants: boolean }[];
  last_login_at: string | null;
  created_at: string | null;
  disabled_at: string | null;
}

export interface AdminUserPage {
  items: AdminUser[];
  next_cursor: string | null;
}

export interface AdminRoleBinding {
  scope_id: string;
  role_key: string;
  include_descendants: boolean;
}

export interface AdminUserDetail extends AdminUser {
  memberships: string[];
  role_bindings: AdminRoleBinding[];
  root_scope_id: string | null;
}

export interface AdminPermission {
  id: string;
  key: string;
  display_name: string;
  description: string;
  is_system: boolean;
  has_policy_consumer: boolean;
  role_count?: number;
  enforcement?: "rbac" | "visibility" | "unimplemented";
  applicable_scope?: "organization" | "all";
  created_at: string;
}

export interface AdminRole {
  version: number;
  delegatable: boolean;
  user_count: number;
  id: string;
  key: string;
  display_name: string;
  description: string;
  is_system: boolean;
  permission_keys: string[];
  binding_count: number;
  created_at: string;
  updated_at: string;
}

export interface AdminRoleBindingPage {
  items: { user_id: string; display_name: string; email: string; scope_id: string; scope_name: string; include_descendants: boolean }[];
  next_cursor: string | null;
}

export interface SecurityAuditEvent {
  id: string;
  event_type: string;
  actor_user_id: string | null;
  actor_name: string | null;
  target_user_id: string | null;
  target_name: string | null;
  scope_id: string | null;
  scope_name: string | null;
  request_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface SecurityAuditPage {
  items: SecurityAuditEvent[];
  next_cursor: string | null;
}

export type Visibility = "private" | "public";
export type AgentDeniedToolGroup =
  | "shell"
  | "workspace_write"
  | "external_connectors"
  | "artifact_delivery"
  | "agent_management"
  | "skill_management"
  | "web_access";
export type PlatformToolProfile = "none" | "workspace";

export interface AgentToolPolicy {
  denied_groups: AgentDeniedToolGroup[];
  denied_tools: string[];
}

export interface Agent {
  knowledge_config?: import("./knowledge").KnowledgeConfig;
  unavailable_knowledge_base_ids?: string[];
  description?: string;
  id: string;
  slug: string;
  display_name: string;
  system_prompt: string;
  tool_policy: AgentToolPolicy;
  system_key: string | null;
  platform_tool_profile: PlatformToolProfile;
  scope_id: string;
  scope: Scope;
  visibility: Visibility;
  include_descendants: boolean;
  created_by: string | null;
  // Capability bindings by stable installation id (RBAC §8).
  skill_binding_ids: string[];
  mcp_binding_ids: string[];
  // Bindings that are disabled, removed, or no longer accessible to
  // the requesting user. They remain visible until the owner unbinds them.
  binding_issues?: { id: string; kind: "skill" | "mcp" | "knowledge"; reason: "missing" | "disabled" | "unshared" | "unready" | "service_disabled" }[] | null;
  unavailable_capability_ids: string[];
  actions: {
    use: boolean;
    update: boolean;
    delete: boolean;
    publish: boolean;
  };
  // Resolved for the requesting user (RBAC §6.1).
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ModelProfile {
  id: string;
  display_name: string;
  media_profile: string;
  supports_multimodal_input: boolean;
  supports_images: boolean;
  supports_audio: boolean;
  supports_video: boolean;
  supports_documents: boolean;
  supports_native_pdf: boolean;
  supports_thinking: boolean;
  is_default: boolean;
  // false when the profile's key/URL configuration is incomplete.
  available: boolean;
}

export interface Conversation {
  id: string;
  agent_id: string;
  title: string;
  // Selected chat model profile (null = deployment default).
  model_profile: string | null;
  created_at: string;
  updated_at: string;
  // Redacted after Agent access is revoked; conversation history remains.
  agent: Agent | null;
  // Revocation semantics (RBAC §10): history stays readable, new
  // tasks are disabled.
  read_only: boolean;
  read_only_reason: string | null;
}

export interface Message {
  id: string;
  task_id: string;
  role: string;
  content: {
    presentation?: { version: number; process_end: number };
    text?: string;
    attachments?: {
      name: string;
      path: string;
      mime_type: string;
      size: number;
    }[];
  };
  created_at: string;
}

export type TaskStatus =
  | "created"
  | "running"
  | "awaiting_permission"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type ApprovalProfile = "standard" | "strict" | "read_only" | "full_access";

export type TaskApprovalProfile = ApprovalProfile | "unattended";

export interface Task {
  origin?: "user" | "schedule" | "channel";
  schedule_occurrence_id?: string | null;
  id: string;
  conversation_id: string;
  title: string;
  status: TaskStatus;
  execution_phase?: "queued" | "preparing" | "running" | null;
  run_id: string | null;
  approval_profile: TaskApprovalProfile;
  error: { code?: string; message?: string } | null;
  duration_ms: number | null;
  steps: number | null;
  model_name: string | null;
  token_usage: { input_tokens?: number; output_tokens?: number; complete?: boolean } | null;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  id: string;
  task_id: string;
  conversation_id: string;
  name: string;
  mime_type: string;
  size: number;
  version: number;
  change_type: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface TaskEvent {
  id: number;
  task_id: string;
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

// ----------------------------------------------------------------------
// AG-UI events emitted by the product layer (lib/stream.ts parses them)
// ----------------------------------------------------------------------

export interface ToolCallState {
  toolCallId: string;
  toolCallName: string;
  args: string;
  result: string;
  state: "running" | "success" | "error" | string;
}

export interface PendingConfirm {
  toolCalls: {
    id: string;
    name: string;
    input: string;
  }[];
  decisionVersion: string;
  /** Product Task that owns the parked AgentScope reply. */
  taskId?: string;
  /** Task-frozen approval profile / AgentScope mode, explaining the card. */
  approvalProfile?: TaskApprovalProfile;
  permissionMode?: string;
}

export interface SkillHub {
  hub_id: string;
  display_name: string;
  description: string;
  icon_url: string | null;
}

// ----------------------------------------------------------------------
// Capabilities (RBAC design §8/§11.3) - sanitized installation views
// ----------------------------------------------------------------------

export interface CapabilityMetadata {
  central_library_origin?: { scope_id: string; scope_name: string };
  description?: string;
  tags?: string[];
  hub_id?: string | null;
  card_id?: string | null;
  version?: string | null;
  is_stateful?: boolean | null;
  creation_channel?: "upload" | "assistant";
  [key: string]: unknown;
}

export interface CapabilityListItem {
  id: string;
  capability_type: "skill" | "mcp";
  scope_id: string;
  name: string;
  display_name: string | null;
  enabled: boolean;
  metadata: CapabilityMetadata;
  // Per-requesting-user manage rights and scope context.
  manageable: boolean;
  // Membership-derived availability is separate from management rights.
  usable: boolean;
  scope_kind: ScopeKind;
}

/** Kept as an alias so the pages read naturally. */
export type InstalledSkill = CapabilityListItem;
export type InstalledMCP = CapabilityListItem;

export interface SkillCard {
  installed?: boolean;
  hub_id: string;
  id: string;
  name: string;
  display_name: string | null;
  description: string;
  tags: string[];
  version: string | null;
  author?: string | null;
  installs?: number | null;
}

export interface SkillCatalogPage {
  cards: SkillCard[];
  next_cursor: string | null;
}

export interface MCPCard {
  installed?: boolean;
  hub_id: string;
  id: string;
  name: string;
  display_name: string | null;
  description: string;
  tags: string[];
  version: string | null;
  is_stateful: boolean;
  inputs_schema: {
    properties?: Record<string, {
      title?: string;
      description?: string;
      type?: string;
      writeOnly?: boolean;
      format?: string;
      default?: unknown;
    }>;
    required?: string[];
  };
}

export interface MCPCatalogPage {
  cards: MCPCard[];
  next_cursor: string | null;
}

export interface ResourcePage<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
