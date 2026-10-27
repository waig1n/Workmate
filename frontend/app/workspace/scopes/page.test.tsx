import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import ScopesPage from "./page";
import { api } from "@/lib/api";

vi.mock("@/lib/api", () => ({ api: { adminScopeTree: vi.fn(), get: vi.fn(), patch: vi.fn(), post: vi.fn() } }));
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.get).mockResolvedValue({ root_permissions: [], items: [], next_cursor: null }); });
function mount() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ScopesPage /></QueryClientProvider>); }
test("组织查询失败提供错误和重试", async () => {
  vi.mocked(api.adminScopeTree).mockRejectedValue(new Error("无法加载组织"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("无法加载组织");
  expect(screen.getByText("重试")).toBeInTheDocument();
});

async function openMove() {
  vi.mocked(api.adminScopeTree).mockResolvedValue([
    { id: "dept", name: "财务部", kind: "department", parent_id: "root", permissions: ["scope.manage"], children: [] },
    { id: "other", name: "业务部", kind: "department", parent_id: "root", permissions: ["scope.manage"], children: [] },
  ]);
  mount();
  fireEvent.click(await screen.findByText("财务部"));
  fireEvent.click(screen.getByText("移动部门"));
  fireEvent.change(screen.getByLabelText("新的上级部门"), { target: { value: "other" } });
}

test("移动预览失败不能确认移动", async () => {
  vi.mocked(api.post).mockRejectedValue(new Error("移动影响检查失败"));
  await openMove();
  expect(await screen.findByRole("alert")).toHaveTextContent("移动影响检查失败");
  expect(screen.getByText("确认")).toBeDisabled();
  expect(api.post).toHaveBeenCalledTimes(1);
});

test("移动导致能力绑定失效时禁止提交", async () => {
  vi.mocked(api.post).mockResolvedValue({ tree_version: 2, descendant_count: 1, member_count: 3, agent_count: 1, removed_inherited_bindings: 0, added_inherited_bindings: 0, capability_blockers: 2 });
  await openMove();
  expect(await screen.findByRole("alert")).toHaveTextContent("2 个能力绑定会失效");
  expect(screen.getByText("确认")).toBeDisabled();
  expect(api.post).toHaveBeenCalledTimes(1);
});
test("部门结构管理员不会看到无权限的成员操作", async () => {
  vi.mocked(api.adminScopeTree).mockResolvedValue([{ id: "dept", name: "财务部", kind: "department", parent_id: "root", permissions: ["scope.manage"], children: [] }]);
  mount();
  fireEvent.click(await screen.findByText("财务部"));
  expect(screen.getByText("你没有此部门的成员查看权限。")).toBeInTheDocument();
  expect(screen.queryByText("添加成员")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("重命名"));
  expect(screen.getByRole("dialog", { name: "重命名部门" })).toBeInTheDocument();
});


test("移动预览区分管理授权与动态共享使用范围", async () => {
  vi.mocked(api.post).mockResolvedValue({ tree_version: 2, descendant_count: 2, member_count: 3, agent_count: 1, removed_inherited_bindings: 0, added_inherited_bindings: 0, capability_blockers: 0, shared_resource_changes: { agents: { gained: 1, lost: 2 }, skills: { gained: 3, lost: 4 }, mcps: { gained: 2, lost: 1 }, knowledge_bases: { gained: 5, lost: 6 }, knowledge_downloads: { gained: 0, lost: 1 } } });
  await openMove();
  expect(await screen.findByText("共享使用范围变化（按部门统计）")).toBeInTheDocument();
  expect(screen.getByText("Agent：新增 1，失去 2")).toBeInTheDocument();
  expect(screen.getByText("MCP 连接：新增 2，失去 1")).toBeInTheDocument();
  expect(screen.getByText("原文下载：新增 0，失去 1")).toBeInTheDocument();
  expect(screen.getByText("确认")).toBeEnabled();
});
