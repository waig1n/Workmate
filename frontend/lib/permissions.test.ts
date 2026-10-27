import assert from "node:assert/strict";
import { test } from "vitest";

import {
  bindableCapabilities,
  adminNavigation,
  capabilityPickerState,
  can,
  canCreatePersonalAgent,
  canCreateOrganizationAgent,
  canManageCapability,
  canPublish,
  capabilityTargetScopes,
  conversationBlockState,
  degradationState,
} from "./permissions.ts";
import type {
  Agent,
  CapabilityListItem,
  Conversation,
  Permissions,
  Scope,
} from "./types.ts";

const admin: Permissions = {
  permissions: [
    "agent.view", "agent.use", "agent.create", "agent.update",
    "agent.delete", "agent.publish", "skill.manage", "mcp.manage",
    "role.bind", "scope.manage",
  ],
  personal_scope_id: "p-1",
  root_scope_id: "root",
  is_root_admin: true,
  root_permissions: ["user.read", "user.manage", "role.read", "role.manage", "role.bind", "scope.manage", "membership.manage"],
  managed_scope_ids: ["root"],
};

const member: Permissions = {
  permissions: [],
  personal_scope_id: "p-2",
  root_scope_id: "root",
  is_root_admin: false,
};

test("an authenticated user with a personal scope may create personal agents", () => {
  assert.equal(canCreatePersonalAgent(member), true);
  assert.equal(canCreatePersonalAgent(null), false);
});

const scopes: Scope[] = [
  { id: "personal", kind: "personal", name: "我的", parent_id: null, permissions: [] },
  { id: "root", kind: "organization", name: "总部", parent_id: null, permissions: [] },
  { id: "dept-a", kind: "department", name: "研发部", parent_id: "root", permissions: ["agent.create", "agent.publish", "skill.manage"] },
  { id: "team-a", kind: "department", name: "平台组", parent_id: "dept-a", permissions: [] },
  { id: "dept-b", kind: "department", name: "销售部", parent_id: "root", permissions: ["mcp.manage"] },
];

test("organization agent controls use the exact scope permission facts", () => {
  assert.equal(canCreateOrganizationAgent(scopes[2]), true);
  assert.equal(canPublish(scopes[2]), true);
  assert.equal(canCreateOrganizationAgent(scopes[4]), false);
  assert.equal(canPublish(scopes[4]), false);
});

test("capability creation targets exclude personal scopes and require kind permission", () => {
  assert.deepEqual(
    capabilityTargetScopes(scopes, "skill").map((scope) => scope.id),
    ["dept-a"],
  );
  assert.deepEqual(
    capabilityTargetScopes(scopes, "mcp").map((scope) => scope.id),
    ["dept-b"],
  );
});

const capability = (
  id: string,
  scopeId: string,
  scopeKind: CapabilityListItem["scope_kind"] = "department",
  enabled = true,
): CapabilityListItem => ({
  id,
  capability_type: "skill",
  scope_id: scopeId,
  name: id,
  display_name: null,
  enabled,
  metadata: {},
  manageable: true,
  usable: true,
  scope_kind: scopeKind,
});

test("legacy personal capabilities never expose management controls", () => {
  assert.equal(canManageCapability(capability("legacy", "personal", "personal")), false);
  assert.equal(canManageCapability(capability("managed", "dept-a")), true);
  assert.equal(
    canManageCapability({ ...capability("readonly", "dept-a"), manageable: false }),
    false,
  );
});

test("personal agents may bind accessible organization capabilities", () => {
  const choices = bindableCapabilities(
    scopes[0],
    [capability("root-skill", "root"), capability("dept-b-skill", "dept-b")],
    scopes,
  );
  assert.deepEqual(choices.map((item) => item.id), ["root-skill", "dept-b-skill"]);
});

test("manageable but unusable and revoked capabilities are not bindable", () => {
  const manageableOnly = { ...capability("managed", "dept-a"), usable: false };
  const revoked = capability("revoked", "dept-a");
  const choices = bindableCapabilities(
    scopes[0], [manageableOnly, revoked], scopes, ["revoked"],
  );
  assert.deepEqual(choices, []);
  assert.deepEqual(
    bindableCapabilities(scopes[2], [manageableOnly], scopes).map(
      (item) => item.id,
    ),
    ["managed"],
  );
});

test("organization agents may bind only same-scope and ancestor capabilities", () => {
  const choices = bindableCapabilities(
    scopes[2],
    [
      capability("root-skill", "root"),
      capability("same-skill", "dept-a"),
      capability("descendant-skill", "team-a"),
      capability("sibling-skill", "dept-b"),
      capability("disabled-skill", "root", "organization", false),
    ],
    scopes,
  );
  assert.deepEqual(choices.map((item) => item.id), ["root-skill", "same-skill"]);
});

test("can() checks a single permission key", () => {
  assert.equal(can(admin, "scope.manage"), true);
  assert.equal(can(member, "scope.manage"), false);
  assert.equal(can(undefined, "agent.view"), false);
});

test("admin navigation follows independent backend permission keys", () => {
  assert.deepEqual(adminNavigation(admin), {
    users: true, roles: true, scopes: true,
  });
  assert.deepEqual(adminNavigation({
    ...member, permissions: ["role.read"], root_permissions: ["role.read"],
  }), { users: false, roles: true, scopes: false });
});

test("department grants never expose global user or role management", () => {
  assert.deepEqual(adminNavigation({
    ...member, permissions: ["scope.manage", "role.bind"],
    root_permissions: [], managed_scope_ids: ["finance"],
  }), { users: false, roles: false, scopes: true });
  assert.deepEqual(adminNavigation({ ...admin, root_permissions: undefined, managed_scope_ids: undefined }),
    { users: false, roles: false, scopes: false });
});

function agentWithUnavailable(ids: string[]): Agent {
  return {
    id: "agent-1",
    slug: "assistant",
    display_name: "助手",
    system_prompt: "help",
    system_key: null,
    platform_tool_profile: "none",
    scope_id: "personal",
    scope: scopes[0],
    visibility: "private",
    include_descendants: true,
    created_by: "user-1",
    skill_binding_ids: ["lost-skill"],
    mcp_binding_ids: [],
    tool_policy: { denied_groups: [], denied_tools: [] },
    unavailable_capability_ids: ids,
    actions: { use: true, update: true, delete: true, publish: false },
    is_default: false,
    created_at: "2026-08-24T00:00:00Z",
    updated_at: "2026-08-24T00:00:00Z",
  };
}

test("degraded agent presentation preserves unavailable installation ids", () => {
  assert.deepEqual(degradationState(agentWithUnavailable(["lost-skill", "lost-mcp"])), {
    degraded: true,
    unavailableIds: ["lost-skill", "lost-mcp"],
  });
  assert.deepEqual(degradationState(agentWithUnavailable([])), {
    degraded: false,
    unavailableIds: [],
  });
});

test("candidate failure never hides unavailable binding recovery", () => {
  assert.deepEqual(capabilityPickerState(false, true, ["lost-skill"]), {
    showCandidates: false,
    showUnavailable: true,
  });
});

test("conversation composer distinguishes capability revocation from lost agent access", () => {
  const base = {
    id: "conversation-1",
    agent_id: "agent-1",
    title: "历史会话",
    model_profile: null,
    created_at: "2026-08-24T00:00:00Z",
    updated_at: "2026-08-24T00:00:00Z",
  };
  const capabilityRevoked = {
    ...base,
    agent: agentWithUnavailable(["lost-skill"]),
    read_only: true,
    read_only_reason: "capability unavailable",
  } satisfies Conversation;
  assert.deepEqual(conversationBlockState(capabilityRevoked), {
    blocked: true,
    kind: "capability_revoked",
    unavailableIds: ["lost-skill"],
  });

  assert.deepEqual(
    conversationBlockState({
      ...capabilityRevoked,
      agent: agentWithUnavailable([]),
      read_only_reason: "lost access",
    }),
    { blocked: true, kind: "access_revoked", unavailableIds: [] },
  );
  assert.deepEqual(
    conversationBlockState({
      ...capabilityRevoked,
      agent: null,
      read_only_reason: "lost access",
    }),
    { blocked: true, kind: "access_revoked", unavailableIds: [] },
  );
});
