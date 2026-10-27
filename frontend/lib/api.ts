// REST client with JWT auth (token stored in localStorage).
import { notifyAdministrationChanged } from "./administrationEvents";

import type {
  AdminPermission,
  AdminRole,
  AdminRoleBindingPage,
  AdminScopeNode,
  AdminUser,
  AdminUserDetail,
  AdminUserPage,
  ModelProfile,
  Permissions,
  RegistrationStatus,
  OrganizationSettings,
  User,
} from "./types";

const TOKEN_KEY = "workmate_token";
const USER_KEY = "workmate_user";
const PERMISSIONS_KEY = "workmate_permissions";

export type ApiError = Error & {
  status: number;
  code: string | null;
  detail: Record<string, unknown> | null;
};

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setAuth(token: string, user: unknown) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function setPermissions(permissions: Permissions) {
  localStorage.setItem(PERMISSIONS_KEY, JSON.stringify(permissions));
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(PERMISSIONS_KEY);
}

const validationFieldLabels: Record<string, string> = {
  email: "邮箱", display_name: "姓名", name: "名称", password: "密码",
  initial_password: "初始密码", new_password: "新密码", current_password: "当前密码",
  description: "描述", key: "标识", role_key: "角色", scope_id: "生效范围",
  parent_id: "上级部门", new_parent_id: "新的上级部门", permission_keys: "权限",
  membership_scope_ids: "所属部门", role_bindings: "角色授权", include_descendants: "下级继承设置",
  expected_version: "数据版本", expected_tree_version: "组织版本", q: "搜索条件",
  organization_name: "组织名称",
  status: "状态", limit: "每页数量", cursor: "分页位置",
};

function validationFields(detail: unknown[]): { field: string; message: string }[] {
  return detail.map((item) => {
    const error = item && typeof item === "object" ? item as Record<string, unknown> : {};
    const field = Array.isArray(error.loc)
      ? [...error.loc].reverse().find((part): part is string => typeof part === "string" && Object.hasOwn(validationFieldLabels, part)) ?? "form"
      : "form";
    const label = validationFieldLabels[field] ?? "表单字段";
    // Validation msg/input/ctx may contain passwords or other submitted values.
    // Only emit controlled copy derived from known field names and error types.
    let message = `${label}内容无效，请检查后重试。`;
    if (error.type === "missing") message = `请填写${label}。`;
    else if (error.type === "string_too_short" || error.type === "string_too_long") message = `${label}长度不符合要求，请检查后重试。`;
    else if (field === "email") message = "邮箱格式不正确，请检查后重试。";
    return { field, message };
  });
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const resp = await fetch(`/api${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  // Failed logins/registrations return 401/409 too - only redirect for
  // 401 on non-auth endpoints (an expired session).
  if (resp.status === 401 && !path.startsWith("/auth/") && typeof window !== "undefined") {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    let code: string | null = null;
    let detailData: Record<string, unknown> | null = null;
    try {
      const body = await resp.json();
      if (Array.isArray(body?.detail)) {
        const fields = validationFields(body.detail);
        detail = fields.map((field) => field.message).join(" ") || "提交内容未通过校验，请检查后重试。";
        code = "validation_error";
        detailData = { fields };
      } else if (typeof body?.detail === "string") {
        detail = body.detail;
      } else if (body?.detail && typeof body.detail === "object") {
        // Stable error-code shape: {code, message} (plan §5.2).
        detail = body.detail.message ?? JSON.stringify(body.detail);
        code = body.detail.code ?? null;
        detailData = body.detail;
      }
    } catch {
      /* keep statusText */
    }
    const error = new Error(String(detail)) as ApiError;
    error.status = resp.status;
    error.code = code;
    error.detail = detailData;
    throw error;
  }
  if (method !== "GET" && path.startsWith("/admin/") && !path.endsWith("-preview")) {
    notifyAdministrationChanged();
  }
  if (resp.status === 204) return undefined as T;
  return resp.json();
}

async function upload<T>(path: string, body: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(`/api${path}`, { method: "POST", headers, body });
  if (resp.status === 401 && typeof window !== "undefined") {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {}
    throw new Error(String(detail));
  }
  return resp.json();
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, body: FormData) => upload<T>(path, body),

  registrationStatus: () =>
    request<RegistrationStatus>("GET", "/auth/registration-status"),

  me: () => request<User>("GET", "/auth/me"),
  updateProfile: (displayName: string) =>
    request<User>("PATCH", "/auth/me", { display_name: displayName }),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ access_token: string; user: User }>(
      "POST",
      "/auth/me/change-password",
      { current_password: currentPassword, new_password: newPassword },
    ),

  adminUsers: (query = "") =>
    request<AdminUserPage>("GET", `/admin/users${query ? `?${query}` : ""}`),
  adminUser: (id: string) =>
    request<AdminUserDetail>("GET", `/admin/users/${id}`),
  createAdminUser: (body: unknown) =>
    request<AdminUser>("POST", "/admin/users", body),
  updateAdminAccess: (id: string, body: unknown) =>
    request<AdminUserDetail>("PUT", `/admin/users/${id}/access`, body),
  adminRoles: () => request<AdminRole[]>("GET", "/admin/roles"),
  adminRoleBindings: (id: string, cursor?: string | null) =>
    request<AdminRoleBindingPage>("GET", `/admin/roles/${encodeURIComponent(id)}/bindings${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`),
  adminPermissions: () =>
    request<AdminPermission[]>("GET", "/admin/permissions"),
  adminScopeTree: () =>
    request<AdminScopeNode[]>("GET", "/admin/scopes/tree"),

  async login(email: string, password: string) {
    const res = await request<{ access_token: string; user: unknown }>(
      "POST",
      "/auth/login",
      { email, password },
    );
    // Persist the token before hydrating permissions so the follow-up
    // request carries the Authorization header.
    setAuth(res.access_token, res.user);
    await hydratePermissions();
    return res;
  },
  organization: () => request<OrganizationSettings>("GET", "/admin/organization"),
  renameOrganization: (name: string, treeVersion: number) => request<OrganizationSettings>("PATCH", "/admin/organization", { name, expected_tree_version: treeVersion }),

  async register(email: string, password: string, displayName: string, organizationName?: string) {
    const res = await request<{ access_token: string; user: unknown }>(
      "POST",
      "/auth/register",
      { email, password, display_name: displayName, ...(organizationName !== undefined ? { organization_name: organizationName } : {}) },
    );
    setAuth(res.access_token, res.user);
    await hydratePermissions();
    return res;
  },
};

/** Fetch and cache the caller's permissions (same lifecycle as the
 * token - RBAC §12). */
export async function hydratePermissions(): Promise<Permissions> {
  const permissions = await request<Permissions>(
    "GET", "/auth/me/permissions",
  );
  setPermissions(permissions);
  return permissions;
}

export async function downloadArtifact(
  artifactId: string,
  filename: string,
): Promise<void> {
  const token = getToken();
  const resp = await fetch(`/api/artifacts/${artifactId}/download`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new Error(`下载失败 (${resp.status})`);
  const url = URL.createObjectURL(await resp.blob());
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

export async function fetchArtifactBlob(
  artifactId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const token = getToken();
  const resp = await fetch(
    `/api/artifacts/${encodeURIComponent(artifactId)}/download?inline=true`,
    {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: "no-store",
      signal,
    },
  );
  if (resp.status === 401 && typeof window !== "undefined") {
    window.location.href = "/login";
    throw new Error("登录状态已过期");
  }
  if (!resp.ok) {
    const message = resp.status === 410
      ? "成果文件已不存在，请联系管理员检查存储"
      : resp.status === 404
        ? "成果不存在或无权访问"
        : `预览加载失败 (${resp.status})`;
    throw new Error(message);
  }
  return resp.blob();
}

export function streamUrl(conversationId: string): string {
  return `/api/tasks/conversations/${conversationId}/events`;
}

export function listModelProfiles(): Promise<ModelProfile[]> {
  return api.get<ModelProfile[]>("/models");
}
