import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { DepartmentPeople } from "./people-department";
import { api } from "@/lib/api";
import type { AdminScopeNode } from "@/lib/types";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), delete: vi.fn().mockResolvedValue({}) } }));
test("根组织显示未分配部门，不把组织身份当作可移除部门归属", async () => {
  const root = { id: "root", kind: "organization", name: "组织", permissions: ["membership.read", "membership.manage"], children: [] } as unknown as AdminScopeNode;
  vi.mocked(api.get).mockImplementation(async (path) => path.includes("permissions") ? { root_permissions: [] } : { items: [{ id: "u", display_name: "未分配人员", membership_scope_ids: ["root"] }], counts: { direct: 1, total: 2 }, next_cursor: null });
  render(<QueryClientProvider client={new QueryClient()}><DepartmentPeople scope={root} scopes={[root]} /></QueryClientProvider>);
  expect(screen.getByRole("button", { name: "未分配部门" })).toHaveAttribute("aria-pressed", "true");
  expect(await screen.findByText(/未分配部门 1 人/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "移出部门" })).not.toBeInTheDocument();
});

test("直属默认、汇总显示真实路径且只在所属部门管理", async () => {
  const parent = { id: "l1", name: "L1", permissions: ["membership.read", "membership.manage"], children: [] } as unknown as AdminScopeNode;
  const child = { id: "l3", name: "L1 / L2 / L3", permissions: ["membership.read", "membership.manage"], children: [] } as unknown as AdminScopeNode;
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.includes("permissions")) return { root_permissions: [] };
    return { items: path.includes("include_descendants=true") ? [{ id: "a", display_name: "人员A", email: "a@test.dev", status: "active", membership_scope_ids: ["l3"] }] : [], next_cursor: null, counts: { direct: 0, total: 1, limited: false } };
  });
  const select = vi.fn();
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><DepartmentPeople scope={parent} scopes={[parent, child]} onSelectScope={select} /></QueryClientProvider>);
  expect(screen.getByRole("button", { name: "直属成员" })).toHaveAttribute("aria-pressed", "true");
  expect(await screen.findByText(/直属 0 人 · 含下级 1 人/)).toBeInTheDocument();
  expect(screen.queryByText("人员A")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "全部成员（含下级）" }));
  expect(await screen.findByText("归属：L1 / L2 / L3")).toBeInTheDocument();
  expect(screen.queryByText("移出部门")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /进入所属部门管理/ }));
  await waitFor(() => expect(select).toHaveBeenCalledWith("l3"));
});

test("移出部门先展示职责清单并默认同步撤销", async () => {
  const department = { id: "finance", kind: "department", name: "财务部", permissions: ["membership.read", "membership.manage", "role.bind"], children: [] } as unknown as AdminScopeNode;
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path.includes("permissions")) return { root_permissions: [] };
    if (path.includes("effective-access")) return { sources: [{ role_key: "editor", role_name: "Agent 管理员", scope_id: "finance", scope_name: "财务部", inherited: false, include_descendants: false }] };
    return { items: path.includes("role-bindings") ? [] : [{ id: "u", display_name: "张三", access_version: 3, membership_scope_ids: ["finance"] }], next_cursor: null };
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><DepartmentPeople scope={department} scopes={[department]} /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "移出部门" }));
  expect(await screen.findByText(/Agent 管理员 · 财务部/)).toBeVisible();
  expect(screen.getByLabelText("保留上述职责，继续跨部门负责")).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "确认移出" }));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/admin/scopes/finance/members/u?expected_version=3&retain_departure_roles=false"));
});
