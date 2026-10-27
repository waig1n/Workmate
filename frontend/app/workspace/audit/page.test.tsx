import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import AuditPage from "./page";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: { adminScopeTree: vi.fn(), get: vi.fn() } }));
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.adminScopeTree).mockResolvedValue([]); });
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AuditPage /></QueryClientProvider>); }
test.each([
  ["role_binding.set", "分配角色"], ["role_binding.remove", "撤销角色"],
  ["user.access_update", "修改用户授权"], ["role.permissions_update", "修改角色权限"],
  ["permission.create", "创建权限定义"], ["user.access_update_denied", "用户授权修改被拒绝"],
])("审计操作 %s 使用实际后端事件键筛选", async (eventType, label) => {
  vi.mocked(api.get).mockResolvedValue({ items: [], next_cursor: null });
  mount();
  await screen.findByText("当前筛选下没有审计记录。");
  const option = screen.getByRole("option", { name: label }) as HTMLOptionElement;
  expect(option.value).toBe(eventType);
  fireEvent.change(screen.getByLabelText("操作类型"), { target: { value: option.value } });
  await waitFor(() => expect(api.get).toHaveBeenLastCalledWith(`/admin/security-audit-events?event_type=${eventType}`));
});
test("审计查询失败不显示为空记录并可重试", async () => {
  vi.mocked(api.get).mockRejectedValue(new Error("审计服务暂不可用"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("审计服务暂不可用");
  expect(screen.queryByText("当前筛选下没有审计记录。")).not.toBeInTheDocument();
  vi.mocked(api.get).mockResolvedValue({ items: [], next_cursor: null });
  fireEvent.click(screen.getByText("重试"));
  expect(await screen.findByText("当前筛选下没有审计记录。")).toBeInTheDocument();
});
test("审计第三页保留前两页且可查看变更详情", async () => {
  vi.mocked(api.get).mockImplementation(async (path) => {
    const page = path.includes("cursor=three") ? 3 : path.includes("cursor=two") ? 2 : 1;
    return { items: [{ id: `audit${page}`, created_at: "2026-09-11T00:00:00Z", event_type: "user.create", actor_name: `操作者${page}`, target_name: `对象${page}`, scope_name: "财务部", request_id: `request${page}`, metadata: { changed: true } }], next_cursor: page === 1 ? "two" : page === 2 ? "three" : null };
  });
  mount();
  await screen.findByText("操作者1");
  fireEvent.click(screen.getByText("加载更多记录"));
  await screen.findByText("操作者2");
  fireEvent.click(screen.getByText("加载更多记录"));
  await screen.findByText("操作者3");
  expect(screen.getByText("操作者1")).toBeInTheDocument();
  expect(screen.getByText("操作者2")).toBeInTheDocument();
  fireEvent.click(screen.getAllByText("查看变更")[1]);
  expect(screen.getByRole("dialog", { name: "管理变更详情" })).toHaveTextContent("request2");
});
