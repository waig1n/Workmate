// Permission helpers on top of GET /auth/me/permissions (RBAC §11.2).
// Permission keys mirror backend app/auth/permissions.py.

import type {
  Agent,
  CapabilityListItem,
  Conversation,
  Permissions,
  Scope,
} from "./types";

export const PERMISSION_KEYS = {
  agentView: "agent.view",
  agentUse: "agent.use",
  agentCreate: "agent.create",
  agentUpdate: "agent.update",
  agentDelete: "agent.delete",
  agentPublish: "agent.publish",
  skillManage: "skill.manage",
  mcpManage: "mcp.manage",
  roleBind: "role.bind",
  scopeManage: "scope.manage",
  userRead: "user.read",
  userManage: "user.manage",
  membershipRead: "membership.read",
  membershipManage: "membership.manage",
  roleRead: "role.read",
  roleManage: "role.manage",
  auditRead: "audit.read",
} as const;

/** Personal agents need no grant - every user owns their scope. */
export function canCreatePersonalAgent(
  permissions: Permissions | null | undefined,
): boolean {
  return Boolean(permissions?.personal_scope_id);
}

/** Organization agents: agent.create in the target scope. */
export function can(
  permissions: Permissions | null | undefined,
  key: string,
): boolean {
  return permissions?.permissions?.includes(key) ?? false;
}

export function adminNavigation(
  permissions: Permissions | null | undefined,
): { users: boolean; roles: boolean; scopes: boolean } {
  return {
    users: canRoot(permissions, PERMISSION_KEYS.userRead),
    roles: canRoot(permissions, PERMISSION_KEYS.roleRead),
    scopes: Boolean(permissions?.managed_scope_ids?.length),
  };
}

/** Global menus require exact root facts; missing/new API facts fail closed. */
export function canRoot(permissions: Permissions | null | undefined, key: string): boolean {
  return Boolean(permissions?.root_scope_id && permissions.root_permissions?.includes(key));
}

/** Organization actions deliberately use separate permission keys. */
export function scopeCan(scope: Scope | null | undefined, key: string): boolean {
  return scope?.permissions?.includes(key) ?? false;
}

export function canCreateOrganizationAgent(scope: Scope | null | undefined): boolean {
  return Boolean(scope && scope.kind !== "personal" && scopeCan(scope, PERMISSION_KEYS.agentCreate));
}

export function canPublish(scope: Scope | null | undefined): boolean {
  return Boolean(scope && scope.kind !== "personal" && scopeCan(scope, PERMISSION_KEYS.agentPublish));
}

export function capabilityTargetScopes(
  scopes: Scope[],
  type: "skill" | "mcp",
): Scope[] {
  const permission = type === "skill"
    ? PERMISSION_KEYS.skillManage
    : PERMISSION_KEYS.mcpManage;
  return scopes.filter(
    (scope) => scope.kind !== "personal" && scopeCan(scope, permission),
  );
}

export function canManageCapability(item: CapabilityListItem): boolean {
  return item.scope_kind !== "personal" && item.manageable;
}

export function bindableCapabilities(
  agentScope: Scope,
  capabilities: CapabilityListItem[],
  scopes: Scope[],
  unavailableIds: string[] = [],
): CapabilityListItem[] {
  const unavailable = new Set(unavailableIds);
  const organizational = capabilities.filter(
    (item) => item.scope_kind !== "personal"
      && item.enabled
      && !unavailable.has(item.id),
  );
  if (agentScope.kind === "personal") {
    return organizational.filter((item) => item.usable);
  }

  const byId = new Map(scopes.map((scope) => [scope.id, scope]));
  const allowed = new Set<string>();
  let current: Scope | undefined = agentScope;
  while (current && !allowed.has(current.id)) {
    allowed.add(current.id);
    current = current.parent_id ? byId.get(current.parent_id) : undefined;
  }
  return organizational.filter((item) => allowed.has(item.scope_id));
}

export function degradationState(agent: Agent | null | undefined): {
  degraded: boolean;
  unavailableIds: string[];
} {
  const unavailableIds = [...(agent?.unavailable_capability_ids ?? [])];
  return { degraded: unavailableIds.length > 0, unavailableIds };
}

export function conversationBlockState(conversation: Conversation): {
  blocked: boolean;
  kind: "capability_revoked" | "access_revoked" | null;
  unavailableIds: string[];
} {
  if (!conversation.agent) {
    return { blocked: true, kind: "access_revoked", unavailableIds: [] };
  }
  const { degraded, unavailableIds } = degradationState(conversation.agent);
  if (degraded) {
    return { blocked: true, kind: "capability_revoked", unavailableIds };
  }
  if (conversation.read_only) {
    return { blocked: true, kind: "access_revoked", unavailableIds: [] };
  }
  return { blocked: false, kind: null, unavailableIds: [] };
}

export function capabilityPickerState(
  loading: boolean,
  failed: boolean,
  unavailableIds: string[],
): { showCandidates: boolean; showUnavailable: boolean } {
  return {
    showCandidates: !loading && !failed,
    // Recovery controls remain available even when candidate discovery fails.
    showUnavailable: unavailableIds.length > 0,
  };
}
