import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RolesPage from "./page";
import { api } from "@/lib/api";
import { usePermissions } from "@/lib/usePermissions";
import type { AdminRole } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), adminRoles: vi.fn(), adminPermissions: vi.fn(), adminRoleBindings: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }));
vi.mock("@/lib/usePermissions", () => ({ usePermissions: vi.fn() }));
const role: AdminRole = { id: "r", key: "member", display_name: "普通成员", description: "可使用公开资源", is_system: true, permission_keys: [], binding_count: 3, user_count: 2, version: 1, delegatable: false, created_at: "", updated_at: "" };
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><RolesPage /></QueryClientProvider>); }
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(usePermissions).mockReturnValue({ data: { root_scope_id: "root", root_permissions: ["role.read", "role.manage"] }, isPending: false, isError: false } as unknown as ReturnType<typeof usePermissions>);
  vi.mocked(api.adminRoles).mockResolvedValue([role]);
  vi.mocked(api.adminPermissions).mockResolvedValue([]);
  vi.mocked(api.get).mockResolvedValue([]);
});
describe("角色管理页面", () => {
  it("目录查询失败显示错误与重试，不伪装为空列表", async () => {
    vi.mocked(api.adminRoles).mockRejectedValue(new Error("目录请求失败"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("目录请求失败");
    expect(screen.queryByText("尚无角色。创建角色后即可为用户分配权限。")).not.toBeInTheDocument();
    vi.mocked(api.adminRoles).mockResolvedValue([role]);
    fireEvent.click(screen.getByRole("button", { name: "重试加载" }));
    expect(await screen.findByRole("heading", { name: "普通成员" })).toBeVisible();
  });
  it("复制系统角色提交新角色且不更新原角色", async () => {
    vi.mocked(api.post).mockResolvedValue(role);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "复制角色" }));
    const dialog = screen.getByRole("dialog", { name: "复制角色" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "角色标识" }), { target: { value: "custom_member" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存角色" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/admin/roles", expect.objectContaining({ key: "custom_member", permission_keys: [], delegatable: false })));
    expect(api.patch).not.toHaveBeenCalled();
  });
  it("三页授权引用持续保留已加载结果", async () => {
    vi.mocked(api.adminRoleBindings).mockImplementation(async (_id, cursor) => ({ items: [{ user_id: cursor ?? "1", display_name: `用户${cursor ?? "1"}`, email: "user@test.dev", scope_id: "s", scope_name: "财务部", include_descendants: false }], next_cursor: cursor === "3" ? null : cursor === "2" ? "3" : "2" }));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "授权范围" }));
    await screen.findByText("用户1");
    fireEvent.click(await screen.findByRole("button", { name: "加载更多授权" }));
    await screen.findByText("用户2");
    fireEvent.click(await screen.findByRole("button", { name: "加载更多授权" }));
    await screen.findByText("用户3");
    expect(screen.getByText("用户1")).toBeVisible();
    expect(screen.getByText("用户2")).toBeVisible();
  });
  it("目录只读管理员看不到创建复制删除入口", async () => {
    vi.mocked(usePermissions).mockReturnValue({ data: { root_scope_id: "root", root_permissions: ["role.read"] }, isPending: false, isError: false } as unknown as ReturnType<typeof usePermissions>);
    mount();
    await screen.findByRole("heading", { name: "普通成员" });
    expect(screen.queryByRole("button", { name: "创建角色" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "复制角色" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看权限" }));
    expect(screen.queryByRole("button", { name: "保存角色" })).not.toBeInTheDocument();
  });
});
