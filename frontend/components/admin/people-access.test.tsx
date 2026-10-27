import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { AccessFields, flattenScopes } from "./people-access";
import type { AdminRole, AdminScopeNode } from "@/lib/types";

const scopes: AdminScopeNode[] = [{ id: "root", kind: "organization", parent_id: null, name: "总公司", permissions: [], children: [{ id: "finance", kind: "department", parent_id: "root", name: "财务部", permissions: [], children: [] }] }];
const roles = [{ id: "editor", key: "editor", display_name: "编辑者" }] as AdminRole[];

test("建号默认普通成员，单部门自动带入且多部门必须明确范围", () => {
  const onBindings = vi.fn();
  const allScopes = [...flattenScopes(scopes), { id: "sales", kind: "department", name: "销售部", permissions: [], children: [] } as unknown as AdminScopeNode];
  const props = { creation: true, scopes: allScopes, roles, bindings: [], onMemberships: vi.fn(), onBindings };
  const { rerender } = render(<AccessFields {...props} memberships={["finance"]} />);
  expect(screen.getByText(/作为普通成员，不授予管理权限/)).toBeInTheDocument();
  const summary = screen.getByText("管理职责（可选）");
  expect(summary.closest("details")).not.toHaveAttribute("open");
  fireEvent.click(summary);
  fireEvent.click(screen.getByText("新增管理职责"));
  expect(screen.getByLabelText("生效范围")).toHaveValue("finance");
  expect(onBindings).not.toHaveBeenCalled();
  rerender(<AccessFields {...props} memberships={["finance", "sales"]} />);
  expect(screen.getByLabelText("生效范围")).toHaveValue("");
  expect(screen.getByText("加入待保存列表")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("生效范围"), { target: { value: "sales" } });
  fireEvent.click(screen.getByText("加入待保存列表"));
  expect(onBindings).toHaveBeenCalledWith([{ scope_id: "sales", role_key: "editor", include_descendants: false }]);
});

test("异步候选到达后可添加角色且默认不继承下级", () => {
  const onBindings = vi.fn();
  const props = { memberships: [], bindings: [], onMemberships: vi.fn(), onBindings };
  const { rerender } = render(<AccessFields {...props} scopes={[]} roles={[]} />);
  expect(screen.getByText("新增管理职责")).toBeDisabled();
  rerender(<AccessFields {...props} scopes={flattenScopes(scopes)} roles={roles} />);
  fireEvent.click(screen.getByText("新增管理职责"));
  expect(screen.getByText("加入待保存列表")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("生效范围"), { target: { value: "root" } });
  fireEvent.click(screen.getByText("加入待保存列表"));
  expect(onBindings).toHaveBeenCalledWith([{ scope_id: "root", role_key: "editor", include_descendants: false }]);
});

test("部门路径区分同名部门且个人归属不会推导角色", () => {
  const onMemberships = vi.fn(); const onBindings = vi.fn();
  render(<AccessFields scopes={flattenScopes(scopes)} roles={roles} memberships={[]} bindings={[]} onMemberships={onMemberships} onBindings={onBindings} />);
  fireEvent.click(screen.getByLabelText("总公司 / 财务部"));
  expect(onMemberships).toHaveBeenCalledWith(["finance"]);
  expect(onBindings).not.toHaveBeenCalled();
});

test("候选加载失败状态禁用整个授权表单", () => {
  render(<AccessFields disabled scopes={flattenScopes(scopes)} roles={roles} memberships={[]} bindings={[]} onMemberships={vi.fn()} onBindings={vi.fn()} />);
  expect(screen.getByLabelText("搜索部门")).toBeDisabled();
  expect(screen.getByText("新增管理职责")).toBeDisabled();
});

test("切换到部门时不提供组织专属角色并回退到可用角色", () => {
  const onBindings = vi.fn();
  const candidates = [{ id: "admin", key: "admin", display_name: "组织管理员", permission_keys: ["scope.manage"] }, { id: "account", key: "account", display_name: "账号管理员", permission_keys: ["user.manage"] }, ...roles] as AdminRole[];
  render(<AccessFields scopes={flattenScopes(scopes)} roles={candidates} memberships={[]} bindings={[]} onMemberships={vi.fn()} onBindings={onBindings} />);
  fireEvent.click(screen.getByText("新增管理职责"));
  expect(screen.getByRole("option", { name: "组织管理员" })).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("生效范围"), { target: { value: "finance" } });
  expect(screen.queryByRole("option", { name: "组织管理员" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "账号管理员" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("加入待保存列表"));
  expect(onBindings).toHaveBeenCalledWith([{ scope_id: "finance", role_key: "editor", include_descendants: false }]);
});

 test("搜索部门不会隐藏已选摘要，取消新增授权不会修改列表", () => {
  const onBindings = vi.fn();
  render(<AccessFields scopes={flattenScopes(scopes)} roles={roles} memberships={["finance"]} bindings={[]} onMemberships={vi.fn()} onBindings={onBindings} />);
  fireEvent.change(screen.getByLabelText("搜索部门"), { target: { value: "不存在" } });
  expect(screen.getByText("已选部门（1）")).toBeInTheDocument();
  expect(screen.getByText("没有匹配的部门")).toBeInTheDocument();
  fireEvent.click(screen.getByText("新增管理职责"));
  expect(onBindings).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("取消添加"));
  expect(screen.queryByLabelText("生效范围")).not.toBeInTheDocument();
  expect(onBindings).not.toHaveBeenCalled();
});

test("大量部门分批显示，编辑部门草稿不立即改写职责列表", () => {
  const onBindings = vi.fn(); const onMemberships = vi.fn();
  const many = Array.from({ length: 25 }, (_, index) => ({ id: `dept-${index}`, kind: "department" as const, parent_id: "root", name: `部门${index + 1}`, permissions: [], children: [] }));
  render(<AccessFields scopes={many} roles={roles} memberships={["dept-0"]} bindings={[{ scope_id: "dept-0", role_key: "editor", include_descendants: true }]} onMemberships={onMemberships} onBindings={onBindings} />);
  expect(screen.queryByLabelText("部门25")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("显示更多部门（还有 5 个）"));
  expect(screen.getByLabelText("部门25")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "取消部门 部门1" }));
  expect(onMemberships).toHaveBeenCalledWith([]);
  expect(onBindings).not.toHaveBeenCalled();
  expect(screen.getByLabelText("管理范围包含下级部门")).toBeChecked();
  expect(screen.getByText("此人员的上述管理职责同时适用于全部下级部门，包括未来新增部门。")).toBeVisible();
  expect(screen.queryByText("组织成员设置")).not.toBeInTheDocument();
});

test("企业成员身份自动维护，不展示组织成员开关或普通成员角色", () => {
  render(<AccessFields scopes={flattenScopes(scopes)} roles={[...roles, { id: "member", key: "member", display_name: "Member" } as AdminRole]} memberships={["root"]} bindings={[]} onMemberships={vi.fn()} onBindings={vi.fn()} />);
  expect(screen.queryByText("组织成员设置")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("新增管理职责"));
  expect(screen.queryByRole("option", { name: "Member" })).not.toBeInTheDocument();
});

test("成员管理员可维护部门但不能添加管理职责", () => {
  const onMemberships = vi.fn();
  render(<AccessFields canBindRoles={false} scopes={flattenScopes(scopes)} roles={[]} memberships={[]} bindings={[]} onMemberships={onMemberships} onBindings={vi.fn()} />);
  fireEvent.click(screen.getByLabelText("总公司 / 财务部"));
  expect(onMemberships).toHaveBeenCalledWith(["finance"]);
  expect(screen.queryByText("新增管理职责")).not.toBeInTheDocument();
});
