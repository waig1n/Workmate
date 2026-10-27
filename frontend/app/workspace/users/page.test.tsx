import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import UsersPage from "./page";
import { api } from "@/lib/api";
import type { AdminUserDetail } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { adminUsers: vi.fn(), adminRoles: vi.fn(), adminScopeTree: vi.fn(), adminUser: vi.fn(), get: vi.fn(), patch: vi.fn(), createAdminUser: vi.fn(), updateAdminAccess: vi.fn() } }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.adminRoles).mockResolvedValue([]);
  vi.mocked(api.adminScopeTree).mockResolvedValue([]);
  vi.mocked(api.get).mockResolvedValue({ root_permissions: ["scope.manage", "role.bind"], permissions: ["scope.manage", "role.bind"], is_root_admin: true });
});

test("资料版本冲突自动获取最新数据但保留当前草稿直到管理员确认", async () => {
  const user: AdminUserDetail = { id: "one", display_name: "初始姓名", email: "one@example.com", status: "active", must_change_password: false, last_login_at: null, created_at: null, disabled_at: null, access_version: 1, profile_version: 1, departments: [], roles: [], memberships: [], role_bindings: [], root_scope_id: "root" };
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [user], next_cursor: null });
  vi.mocked(api.adminUser).mockResolvedValue(user);
  vi.mocked(api.get).mockResolvedValue({ root_permissions: ["user.read", "user.manage"], root_scope_id: "root" });
  mount();
  fireEvent.click(await screen.findByText("初始姓名"));
  fireEvent.change(await screen.findByLabelText("姓名"), { target: { value: "本地草稿" } });
  vi.mocked(api.adminUser).mockResolvedValue({ ...user, display_name: "其他管理员修改", profile_version: 2 });
  vi.mocked(api.patch).mockRejectedValue({ code: "version_conflict", message: "Conflict" });
  fireEvent.click(screen.getByText("保存资料"));
  expect(await screen.findByText("放弃草稿并加载最新版本")).toBeInTheDocument();
  expect(screen.getByLabelText("姓名")).toHaveValue("本地草稿");
  expect(api.patch).toHaveBeenCalledWith("/admin/users/one", { display_name: "本地草稿", email: "one@example.com", expected_version: 1 });
  fireEvent.click(screen.getByText("放弃草稿并加载最新版本"));
  expect(screen.getByLabelText("姓名")).toHaveValue("其他管理员修改");
});

test("仅账号管理权限创建用户不受部门候选加载失败阻塞", async () => {
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(api.adminScopeTree).mockRejectedValue(new Error("无部门管理权限"));
  vi.mocked(api.get).mockResolvedValue({ root_permissions: ["user.read", "user.manage"], root_scope_id: "root" });
  mount();
  fireEvent.click(await screen.findByText("创建用户"));
  expect(screen.getByText("创建企业账号")).toBeEnabled();
  expect(screen.queryByLabelText("生效范围")).not.toBeInTheDocument();
});
function mount() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><UsersPage /></QueryClientProvider>); }
test("连续加载第三页后保留第二页用户", async () => {
  vi.mocked(api.adminUsers).mockImplementation(async (query = "") => {
    const page = query.includes("cursor=three") ? 3 : query.includes("cursor=two") ? 2 : 1;
    return { items: [{ id: `${page}`, display_name: `用户${page}`, email: `${page}@example.com`, status: "active", must_change_password: false, last_login_at: null, created_at: null, disabled_at: null, access_version: 1, profile_version: 1, departments: [], roles: [] }], next_cursor: page === 1 ? "two" : page === 2 ? "three" : null };
  });
  mount();
  await screen.findByText("用户1");
  fireEvent.click(screen.getByText("加载更多"));
  await screen.findByText("用户2");
  fireEvent.click(screen.getByText("加载更多"));
  await screen.findByText("用户3");
  expect(screen.getByText("用户2")).toBeInTheDocument();
});
test("查询失败显示可重试错误而非空列表", async () => {
  vi.mocked(api.adminUsers).mockRejectedValue(new Error("网络暂时不可用"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("网络暂时不可用");
  expect(screen.getByText("重试")).toBeInTheDocument();
});

test("当前权限显示全部内置权限的中文名称，技术标识默认折叠", async () => {
  const user: AdminUserDetail = { id: "one", display_name: "权限用户", email: "one@example.com", status: "active", must_change_password: false, last_login_at: null, created_at: null, disabled_at: null, access_version: 1, profile_version: 1, departments: [], roles: [], memberships: [], role_bindings: [], root_scope_id: "root" };
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [user], next_cursor: null });
  vi.mocked(api.adminUser).mockResolvedValue(user);
  vi.mocked(api.adminScopeTree).mockResolvedValue([{ id: "root", kind: "organization", parent_id: null, name: "总公司", permissions: [], children: [] }]);
  vi.mocked(api.get).mockImplementation(async (path) => path.includes("effective-access") ? { permissions: ["knowledge.manage", "mcp.manage", "usage.read"], sources: [{ role_name: "能力管理员", scope_name: "总公司", inherited: true }], resource_access_note: "资源使用仍遵循共享规则" } : { root_permissions: ["user.read"], root_scope_id: "root" });
  mount();
  fireEvent.click(await screen.findByText("权限用户"));
  fireEvent.click(await screen.findByText("当前权限"));
  expect(await screen.findByText("管理知识库")).toBeInTheDocument();
  expect(screen.getByText("管理工具连接")).toBeInTheDocument();
  expect(screen.getByText("查看范围用量与成本")).toBeInTheDocument();
  expect(screen.getByText("已授予的权限（3）")).toBeInTheDocument();
  expect(screen.getByText(/从上级部门或组织继承/)).toBeInTheDocument();
  expect(screen.getByText("查看权限技术标识（3）").closest("details")).not.toHaveAttribute("open");
});


test("保存部门变更后刷新当前权限，提交保留访问版本", async () => {
  const user: AdminUserDetail = { id: "one", display_name: "授权用户", email: "one@example.com", status: "active", must_change_password: false, last_login_at: null, created_at: null, disabled_at: null, access_version: 1, profile_version: 1, departments: [], roles: [], memberships: [], role_bindings: [], root_scope_id: "root" };
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [user], next_cursor: null });
  vi.mocked(api.adminUser).mockResolvedValue(user);
  vi.mocked(api.adminScopeTree).mockResolvedValue([{ id: "root", kind: "organization", parent_id: null, name: "总公司", permissions: [], children: [{ id: "dept", kind: "department", parent_id: "root", name: "财务部", permissions: [], children: [] }] }]);
  let saved = false;
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.includes("assignable-roles")) return [];
    if (path.includes("effective-access")) return { permissions: saved ? ["agent.create"] : [], sources: [], resource_access_note: "资源使用仍遵循共享规则" };
    return { root_permissions: ["user.read", "role.bind", "membership.manage"], root_scope_id: "root" };
  });
  vi.mocked(api.updateAdminAccess).mockImplementation(async () => { saved = true; return { ...user, memberships: ["dept"], access_version: 2 }; });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  render(<QueryClientProvider client={client}><UsersPage /></QueryClientProvider>);
  fireEvent.click(await screen.findByText("授权用户"));
  fireEvent.click(await screen.findByText("当前权限"));
  await screen.findByText("已授予的权限（0）");
  fireEvent.click(screen.getByText("部门与职责"));
  fireEvent.click(await screen.findByLabelText("总公司 / 财务部"));
  fireEvent.click(screen.getByText("保存部门与职责"));
  await screen.findByText("部门和管理职责已保存。");
  expect(api.updateAdminAccess).toHaveBeenCalledWith("one", { membership_scope_ids: ["dept"], role_bindings: [], retain_departure_roles: false, expected_version: 1 });
  fireEvent.click(screen.getByText("当前权限"));
  expect(await screen.findByText("已授予的权限（1）")).toBeInTheDocument();
});

test("只有成员管理权限也能在建号时选择部门，无需角色授权权限", async () => {
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(api.get).mockResolvedValue({ root_permissions: ["user.read", "user.manage", "membership.manage"], root_scope_id: "root" });
  vi.mocked(api.adminScopeTree).mockResolvedValue([{ id: "root", kind: "organization", parent_id: null, name: "总公司", permissions: [], children: [{ id: "finance", kind: "department", parent_id: "root", name: "财务部", permissions: [], children: [] }] }]);
  vi.mocked(api.createAdminUser).mockRejectedValue(new Error("验证请求"));
  mount();
  fireEvent.click(await screen.findByText("创建用户"));
  fireEvent.change(screen.getByLabelText("姓名"), { target: { value: "张三" } });
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "zhang@example.com" } });
  fireEvent.change(screen.getByLabelText("临时密码（至少 8 位）"), { target: { value: "temporary-password" } });
  fireEvent.click(await screen.findByLabelText("总公司 / 财务部"));
  expect(screen.queryByText("新增管理职责")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("创建企业账号"));
  await screen.findByText("验证请求");
  expect(api.createAdminUser).toHaveBeenCalledWith(expect.objectContaining({ membership_scope_ids: ["finance"], role_bindings: [] }));
});

test("成员管理员只提交部门，不替换其他管理职责", async () => {
  const user: AdminUserDetail = { id: "one", display_name: "张三", email: "one@example.com", status: "active", must_change_password: false, last_login_at: null, created_at: null, disabled_at: null, access_version: 1, profile_version: 1, departments: [], roles: [], memberships: ["root"], role_bindings: [], root_scope_id: "root" };
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [user], next_cursor: null });
  vi.mocked(api.adminUser).mockResolvedValue(user);
  vi.mocked(api.get).mockResolvedValue({ root_permissions: ["user.read", "membership.manage"], root_scope_id: "root" });
  vi.mocked(api.adminScopeTree).mockResolvedValue([{ id: "root", kind: "organization", parent_id: null, name: "总公司", permissions: [], children: [{ id: "finance", kind: "department", parent_id: "root", name: "财务部", permissions: [], children: [] }] }]);
  vi.mocked(api.updateAdminAccess).mockResolvedValue({ ...user, memberships: ["root", "finance"], access_version: 2 });
  mount();
  fireEvent.click(await screen.findByText("张三"));
  fireEvent.click(await screen.findByText("部门与职责"));
  fireEvent.click(await screen.findByLabelText("总公司 / 财务部"));
  fireEvent.click(screen.getByText("保存部门与职责"));
  await screen.findByText("部门和管理职责已保存。");
  expect(api.updateAdminAccess).toHaveBeenCalledWith("one", { membership_scope_ids: ["root", "finance"], retain_departure_roles: false, expected_version: 1 });
});

test("调岗保存前确认默认撤销职责，保留选择显式提交", async () => {
  const user: AdminUserDetail = { id: "one", display_name: "调岗用户", email: "one@example.com", status: "active", must_change_password: false, last_login_at: null, created_at: null, disabled_at: null, access_version: 1, profile_version: 1, departments: [{ id: "finance", name: "财务部" }], roles: [{ role_key: "editor", role_name: "Agent 管理员", scope_id: "finance", scope_name: "财务部", include_descendants: false }], memberships: ["root", "finance"], role_bindings: [{ scope_id: "finance", role_key: "editor", include_descendants: false }], root_scope_id: "root" };
  vi.mocked(api.adminUsers).mockResolvedValue({ items: [user], next_cursor: null });
  vi.mocked(api.adminUser).mockResolvedValue(user);
  vi.mocked(api.get).mockImplementation(async (path) => path.includes("assignable-roles") ? [] : { root_permissions: ["user.read", "membership.manage", "role.bind"], root_scope_id: "root" });
  vi.mocked(api.adminScopeTree).mockResolvedValue([{ id: "root", kind: "organization", parent_id: null, name: "总公司", permissions: [], children: [{ id: "finance", kind: "department", parent_id: "root", name: "财务部", permissions: [], children: [] }] }]);
  vi.mocked(api.updateAdminAccess).mockResolvedValue({ ...user, memberships: ["root"], access_version: 2 });
  mount();
  fireEvent.click(await screen.findByText("调岗用户"));
  fireEvent.click(await screen.findByText("部门与职责"));
  fireEvent.click(await screen.findByLabelText("总公司 / 财务部"));
  fireEvent.click(screen.getByText("保存部门与职责"));
  const dialog = screen.getByRole("dialog", { name: "确认调岗" });
  expect(api.updateAdminAccess).not.toHaveBeenCalled();
  const retain = within(dialog).getByLabelText("保留上述职责，继续跨部门负责");
  expect(retain).not.toBeChecked();
  fireEvent.click(retain);
  fireEvent.click(within(dialog).getByText("确认调整部门与职责"));
  await screen.findByText("部门和管理职责已保存。");
  expect(api.updateAdminAccess).toHaveBeenCalledWith("one", expect.objectContaining({ membership_scope_ids: ["root"], retain_departure_roles: true, expected_version: 1 }));
});
