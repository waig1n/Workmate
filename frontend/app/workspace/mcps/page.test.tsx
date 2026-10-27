import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import MCPsPage from "./page";
import { api } from "@/lib/api";
import { resourcePageMock } from "@/test/resourcePages";
vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }));
const connection = { id: "mcp", name: "reports", display_name: "财务报表连接", scope_id: "dept", scope_kind: "department", enabled: true, manageable: false, usable: true, metadata: { description: "查询报表" }, config: { token: "must-not-render" } };
beforeEach(() => vi.resetAllMocks());
function mount(manageable = false) {
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => {
  if (path === "/mcps") return [{ ...connection, manageable }];
  if (path === "/auth/me/permissions") return { permissions: [], personal_scope_id: "personal" };
  if (path === "/scopes/accessible") return [{ id: "dept", name: "财务部", kind: "department", permissions: [] }];
  if (path.endsWith("/access")) return { can_use: true, allow_download: false, sources: [{ target_scope_id: "dept", target_name: "财务部", target_kind: "department", include_descendants: false, inherited: false }] };
  if (path === "/sharing/mcps/mcp") return { targets: [{ id: "dept", name: "财务部", kind: "department" }], shared_scope_ids: [] };
  return [];
 }));
 render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MCPsPage /></QueryClientProvider>);
}
test("ordinary members see their connection access source without sharing controls or credentials", async () => {
 mount(); fireEvent.click(await screen.findByRole("button", { name: /财务报表连接/ }));
 expect(await screen.findByText(/财务部：所在部门或组织的共享/)).toBeVisible();
 expect(screen.queryByRole("button", { name: "共享" })).not.toBeInTheDocument();
 expect(screen.queryByText("must-not-render")).not.toBeInTheDocument();
 expect(api.get).not.toHaveBeenCalledWith("/sharing/mcps/mcp");
});
test("only connection managers can configure explicit sharing", async () => {
 mount(true); fireEvent.click(await screen.findByRole("button", { name: /财务报表连接/ }));
 fireEvent.click(await screen.findByRole("button", { name: "共享" }));
 expect(await screen.findByLabelText("财务部")).toBeVisible();
 expect(screen.getByText(/共享复用受管连接/)).toBeVisible();
});
test.each(["organization", "department"])("only organization management offers creation: %s", async (kind) => {
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => {
  if (path === "/auth/me/permissions") return { permissions: ["mcp.manage"] };
  if (path === "/scopes/accessible") return [{ id: "scope", name: "管理范围", kind, permissions: ["mcp.manage"] }];
  return [];
 }));
 render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MCPsPage /></QueryClientProvider>);
 if (kind === "organization") {
  fireEvent.click(await screen.findByRole("button", { name: "添加 MCP" }));
  expect(api.get).not.toHaveBeenCalledWith("/mcps/hubs");
  fireEvent.click(screen.getByRole("button", { name: /自定义连接/ }));
  fireEvent.change(screen.getByLabelText("连接名称"), { target: { value: "temporary-name" } });
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  fireEvent.click(screen.getByRole("button", { name: "添加 MCP" }));
  fireEvent.click(screen.getByRole("button", { name: /自定义连接/ }));
  expect((screen.getByLabelText("连接名称") as HTMLInputElement).value).toMatch(/^mcp-/);
 } else {
  await screen.findByText("还没有可用连接");
  expect(screen.queryByRole("button", { name: "添加 MCP" })).not.toBeInTheDocument();
 }
});
test("resource actions live in details and deletion requires confirmation", async () => {
 mount(true);
 expect(screen.queryByRole("button", { name: "删除连接" })).not.toBeInTheDocument();
 fireEvent.click(await screen.findByRole("button", { name: /财务报表连接/ }));
 fireEvent.click(screen.getByRole("button", { name: "删除连接" }));
 expect(api.delete).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
 await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/mcps/mcp"));
});
test("source filter is sent to server before pagination", async () => {
 mount(); await screen.findByRole("button", { name: /财务报表连接/ });
 fireEvent.change(screen.getByLabelText("连接来源"), { target: { value: "custom" } });
 await waitFor(() => expect(vi.mocked(api.get).mock.calls.some(([path]) => path.startsWith("/mcps?") && new URL(path, "http://test").searchParams.get("source") === "custom")).toBe(true));
});
test("catalog is opened on demand and saved connection opens details for sharing", async () => {
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => {
  if (path === "/scopes/accessible") return [{ id: "org", name: "组织", kind: "organization", permissions: ["mcp.manage"] }];
  if (path === "/mcps/hubs") return [{ hub_id: "hub", display_name: "MCP 服务库" }];
  if (path.startsWith("/mcps/catalog?")) return { cards: [{ hub_id: "hub", id: "card", name: "reports", display_name: "报表模板", inputs_schema: { properties: {} } }] };
  if (path.endsWith("/access")) return { can_use: false, sources: [] };
  return [];
 }));
 vi.mocked(api.post).mockResolvedValue({ ...connection, scope_id: "org", scope_kind: "organization", manageable: true });
 render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MCPsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByRole("button", { name: "添加 MCP" }));
 expect(api.get).not.toHaveBeenCalledWith("/mcps/hubs");
 fireEvent.click(screen.getByRole("button", { name: /从 MCP 服务库添加/ }));
 fireEvent.click(await screen.findByRole("button", { name: "配置连接" }));
 expect(screen.getByRole("dialog", { name: "配置 报表模板" })).toBeVisible();
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 expect(await screen.findByText(/连接已保存/)).toBeVisible();
 expect(screen.getByRole("button", { name: "共享" })).toBeVisible();
});
