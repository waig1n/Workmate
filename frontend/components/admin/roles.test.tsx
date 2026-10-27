import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PermissionMatrix, RoleEditor } from "./roles-editor";
import { api } from "@/lib/api";
import type { AdminPermission, AdminRole } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { patch: vi.fn(), post: vi.fn() } }));
const role = { id: "r", key: "editor", display_name: "编辑者", description: "说明", is_system: false, permission_keys: ["agent.view"], binding_count: 2, user_count: 1, version: 3, created_at: "", updated_at: "" } as AdminRole;
const permissions = [
  { id: "p1", key: "agent.view", display_name: "查看 Agent", description: "", is_system: true, has_policy_consumer: false, enforcement: "visibility", applicable_scope: "all", created_at: "" },
  { id: "p2", key: "agent.create", display_name: "创建 Agent", description: "", is_system: true, has_policy_consumer: true, enforcement: "rbac", applicable_scope: "all", created_at: "" },
] as AdminPermission[];
function mount(props: Partial<React.ComponentProps<typeof RoleEditor>> = {}) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}><RoleEditor role={role} permissions={permissions} onClose={vi.fn()} onDone={vi.fn()} {...props} /></QueryClientProvider>);
}
describe("角色权限编辑", () => {
  beforeEach(() => vi.clearAllMocks());
  it("权限说明默认收起，可按需展开且不改变选择", () => {
    const onChange = vi.fn();
    render(<PermissionMatrix permissions={permissions} selected={["agent.view"]} readOnly={false} onChange={onChange} />);
    expect(screen.queryByText("agent.create")).not.toBeInTheDocument();
    expect(screen.getByText("由可见范围决定")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "显示权限说明" }));
    expect(screen.getByText("agent.create")).toBeVisible();
    expect(screen.getByText("在授权范围内生效")).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  });
  it("角色标识使用有效的HTML v模式并限制合法字符", () => {
    mount({ role: null });
    const input = screen.getByRole("textbox", { name: "角色标识" }) as HTMLInputElement;
    const pattern = new RegExp(`^(?:${input.pattern})$`, "v");
    for (const value of ["finance_editor", "finance-editor", "team.editor:v2"]) expect(pattern.test(value)).toBe(true);
    for (const value of ["a", "Admin", "finance editor", "editor/finance", "a".repeat(65)]) expect(pattern.test(value)).toBe(false);
  });
  it("系统权限技术名称和空名称回退中文，保留自定义显示名", () => {
    const items = [
      { ...permissions[1], id: "system", key: "user.manage", display_name: "user.manage" },
      { ...permissions[1], id: "empty", key: "membership.read", display_name: "" },
      { ...permissions[1], id: "named", key: "role.bind", display_name: "内部授权职责" },
      { ...permissions[1], id: "custom", key: "audit.read", display_name: "audit.read", is_system: false },
    ];
    render(<PermissionMatrix permissions={items} selected={[]} readOnly={false} onChange={vi.fn()} />);
    expect(screen.getByText("管理用户账号")).toBeInTheDocument();
    expect(screen.getByText("查看部门成员")).toBeInTheDocument();
    expect(screen.getByText("内部授权职责")).toBeInTheDocument();
    expect(screen.queryByText("查看管理审计")).not.toBeInTheDocument();
    expect(screen.getAllByText("audit.read")).toHaveLength(1);
  });
  it("保留资源可见性权限并原子保存版本、资料和权限", async () => {
    vi.mocked(api.patch).mockResolvedValue(role);
    mount();
    expect(screen.getByRole("checkbox", { name: /查看 Agent/ })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "角色名称" }), { target: { value: "新编辑者" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /创建 Agent/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存角色" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/admin/roles/r", { display_name: "新编辑者", description: "说明", permission_keys: ["agent.view", "agent.create"], delegatable: false, expected_version: 3 }));
  });
  it("版本冲突保留草稿且错误在弹窗内", async () => {
    vi.mocked(api.patch).mockRejectedValue({ code: "version_conflict", message: "版本冲突" });
    mount();
    fireEvent.change(screen.getByRole("textbox", { name: "角色名称" }), { target: { value: "未保存草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "保存角色" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/版本|修改/);
    expect(screen.getByRole("textbox", { name: "角色名称" })).toHaveValue("未保存草稿");
  });
  it("系统角色只读，复制时创建新角色", async () => {
    mount({ role: { ...role, is_system: true } });
    expect(screen.getByRole("textbox", { name: "角色名称" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "保存角色" })).not.toBeInTheDocument();
  });
  it("未保存更改关闭前显示站内提示", () => {
    mount();
    fireEvent.change(screen.getByRole("textbox", { name: "角色名称" }), { target: { value: "草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByText("有未保存的修改。是否放弃草稿？")).toBeVisible();
  });
  it("选择模板填充职责和权限，允许继续编辑后创建", async () => {
    vi.mocked(api.post).mockResolvedValue(role);
    mount({ role: null, templates: [{ key: "agent_editor", display_name: "Agent 管理员", permission_keys: ["agent.create"], delegatable: true }] });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "agent_editor" } });
    expect(screen.getByRole("textbox", { name: "角色名称" })).toHaveValue("Agent 管理员");
    expect(screen.getByRole("checkbox", { name: /创建 Agent/ })).toBeChecked();
    fireEvent.change(screen.getByRole("textbox", { name: "角色名称" }), { target: { value: "财务 Agent 管理员" } });
    fireEvent.click(screen.getByRole("button", { name: "保存角色" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/admin/roles", { key: "agent_editor", display_name: "财务 Agent 管理员", description: "", permission_keys: ["agent.create"], delegatable: true }));
  });
});
it('labels the knowledge management system permission in Chinese',()=>{
 render(<PermissionMatrix permissions={[{...permissions[0],key:'knowledge.manage',display_name:'knowledge.manage'}]} selected={[]} readOnly={false} onChange={vi.fn()}/>);
 expect(screen.getByText('管理知识库')).toBeVisible();
});
