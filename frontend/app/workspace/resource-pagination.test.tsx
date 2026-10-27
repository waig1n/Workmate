import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import { api } from "@/lib/api";
import Skills from "./skills/page";
import Agents from "./agents/page";
import Knowledge from "./knowledge-bases/page";
import Mcps from "./mcps/page";
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/components/workspace/WorkspaceDraftProvider", () => ({ useWorkspaceDraft: () => ({ selectAgent: vi.fn(), sending: false }) }));
vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }));
beforeEach(() => vi.resetAllMocks());
const item = (id: string) => ({ id, name: id, display_name: id, description: "", scope_id: "org", scope: { id: "org", kind: "organization", name: "总部" }, scope_kind: "organization", enabled: true, metadata: {}, manageable: false, actions: { use: true, update: false, manage: false, download: false }, skill_binding_ids: [], mcp_binding_ids: [] });
test.each([
 ["skills", Skills, "搜索技能库"], ["agents", Agents, "搜索智能体"], ["knowledge-bases", Knowledge, "搜索知识库"], ["mcps", Mcps, "搜索已配置连接"],
] as const)("%s uses server pagination and resets search to page one", async (resource, Page, searchLabel) => {
 let fail = false;
 vi.mocked(api.get).mockImplementation(async path => {
  const url = new URL(path, "http://test");
  if (url.pathname === `/${resource}` && url.searchParams.has("page")) {
   if (fail) throw new Error("暂不可用");
   const page = Number(url.searchParams.get("page"));
   return { items: [item(page === 1 ? "第一页资源" : "第二页资源")], page, page_size: 12, total: 13 };
  }
  if (url.pathname === "/auth/me/permissions") return { permissions: [] };
  return [];
 });
 render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Page /></QueryClientProvider>);
 await screen.findByText("第一页资源");
 expect(screen.getByText("第 1 / 2 页 · 共 13 条")).toBeVisible();
 fireEvent.click(screen.getByRole("button", { name: "下一页" }));
 await screen.findByText("第二页资源");
 expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
 fireEvent.change(screen.getByLabelText(searchLabel), { target: { value: "报告" } });
 await waitFor(() => expect(api.get).toHaveBeenCalledWith(expect.stringMatching(new RegExp(`/${resource}\\?page=1&page_size=12&q=`))));
 await screen.findByText("第一页资源");
 fail = true;
 fireEvent.click(screen.getByRole("button", { name: "下一页" }));
 await screen.findByRole("alert");
 fail = false;
 fireEvent.click(screen.getByRole("button", { name: /重试/ }));
 await screen.findByText("第二页资源");
});

test.each([["agents", Agents], ["knowledge-bases", Knowledge]] as const)("%s keeps selected details and editing state when the list changes page", async (resource, Page) => {
 const selected = { ...item("保留详情"), id: "selected-id", actions: { use: true, update: true, manage: true, publish: true }, visibility: "private", system_prompt: "原始草稿" };
 vi.mocked(api.get).mockImplementation(async path => {
  const url = new URL(path, "http://test");
  if (url.pathname === `/${resource}/selected-id`) return selected;
  if (url.pathname === `/${resource}`) return { items: Number(url.searchParams.get("page")) === 2 ? [item("另一页")] : [selected], total: 13, page: Number(url.searchParams.get("page")), page_size: 12 };
  if (path.endsWith("/documents")) return { items: [], total: 0 };
  if (path === "/auth/me/permissions") return { permissions: [] };
  return [];
 });
 render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Page /></QueryClientProvider>);
 fireEvent.click(await screen.findByRole("button", { name: /保留详情/ }));
 if (resource === "agents") {
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "尚未保存" } });
 }
 fireEvent.click(screen.getByRole("button", { name: "下一页" }));
 await screen.findByText("另一页");
 expect(screen.getByRole("dialog", { name: "保留详情" })).toBeVisible();
 if (resource === "agents") expect(screen.getByLabelText("显示名称")).toHaveValue("尚未保存");
});

test("deleting the only connection on the last page returns to the remaining page", async () => {
 let removed = false;
 vi.mocked(api.get).mockImplementation(async path => {
  const url = new URL(path, "http://test");
  if (url.pathname === "/mcps") {
   const page = Number(url.searchParams.get("page"));
   return { items: page === 1 ? [item("首页连接")] : removed ? [] : [{ ...item("末页连接"), manageable: true }], total: removed ? 12 : 13, page, page_size: 12 };
  }
  if (path === "/auth/me/permissions") return { permissions: [] };
  return [];
 });
 vi.mocked(api.delete).mockImplementation(async () => { removed = true; });
 render(<QueryClientProvider client={new QueryClient()}><Mcps /></QueryClientProvider>);
 await screen.findByText("首页连接");
 fireEvent.click(screen.getByRole("button", { name: "下一页" }));
 await screen.findByText("末页连接");
 fireEvent.click(screen.getByRole("button", { name: /末页连接/ }));
 fireEvent.click(screen.getByRole("button", { name: "删除连接" }));
 expect(api.delete).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
 await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/mcps/末页连接"));
 await screen.findByText("首页连接");
 expect(screen.getByText("第 1 / 1 页 · 共 12 条")).toBeVisible();
});

test.each([["skills", Skills, "搜索技能"], ["mcps", Mcps, "搜索 MCP 服务"]] as const)("%s catalog consumes installed flags and cursor pages independently of the main list", async (resource, Page, placeholder) => {
 vi.mocked(api.get).mockImplementation(async path => {
  const url = new URL(path, "http://test");
  if (url.pathname === `/${resource}`) return { items: [], total: 0, page: 1, page_size: 12 };
  if (url.pathname === "/scopes/accessible") return [{ id: "org", kind: "organization", name: "总部", permissions: ["skill.manage", "mcp.manage"] }];
  if (url.pathname.endsWith("/hubs")) return [{ hub_id: "external", display_name: "第三方" }];
  if (url.pathname.endsWith("/catalog")) return { cards: [{ id: "card", hub_id: "external", name: "card", display_name: url.searchParams.has("cursor") ? "目录第二页" : "目录第一页", installed: true, inputs_schema: {} }], next_cursor: url.searchParams.has("cursor") ? null : "next token" };
  if (path === "/auth/me/permissions") return { permissions: [] };
  return [];
 });
 render(<QueryClientProvider client={new QueryClient()}><Page /></QueryClientProvider>);
 if (resource === "skills") fireEvent.click(await screen.findByRole("button", { name: "添加技能" }));
 else {
  fireEvent.click(await screen.findByRole("button", { name: "添加 MCP" }));
  fireEvent.click(screen.getByRole("button", { name: /从 MCP 服务库添加/ }));
 }
 await screen.findByText("目录第一页");
 expect(screen.getByRole("button", { name: resource === "skills" ? "已入库" : "已添加" })).toBeDisabled();
 await waitFor(() => expect(screen.getByRole("button", { name: "下一页目录" })).toBeEnabled());
 fireEvent.click(screen.getByRole("button", { name: "下一页目录" }));
 await screen.findByText("目录第二页");
 expect(api.get).toHaveBeenCalledWith(expect.stringContaining("cursor=next%20token"));
 if (resource === "mcps") expect(api.get).toHaveBeenCalledWith(expect.stringContaining("scope_id=org"));
 fireEvent.click(screen.getByRole("button", { name: "上一页目录" }));
 await screen.findByText("目录第一页");
 await waitFor(() => expect(screen.getByRole("button", { name: "下一页目录" })).toBeEnabled());
 fireEvent.click(screen.getByRole("button", { name: "下一页目录" }));
 await screen.findByText("目录第二页");
 fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value: "新搜索" } });
 fireEvent.click(screen.getByRole("button", { name: "搜索" }));
 await screen.findByText("目录第一页");
 expect(screen.getByText("目录第 1 页")).toBeVisible();
});

test("configuring a connection from a later page returns the installed list to page one", async () => {
 let created = false;
 vi.mocked(api.get).mockImplementation(async path => {
  const url = new URL(path, "http://test");
  if (url.pathname === "/mcps") { const page = Number(url.searchParams.get("page")); return { items: [item(created ? "新连接" : page === 2 ? "末页旧连接" : "首页旧连接")], total: 13, page, page_size: 12 }; }
  if (path === "/auth/me/permissions") return { permissions: [] };
  if (path === "/scopes/accessible") return [{ id: "org", kind: "organization", name: "总部", permissions: ["mcp.manage"] }];
  if (path === "/mcps/hubs") return [{ hub_id: "external", display_name: "第三方" }];
  if (url.pathname === "/mcps/catalog") return { cards: [{ id: "card", hub_id: "external", name: "external-card", display_name: "待配置连接", installed: created, inputs_schema: {} }], next_cursor: null };
  return [];
 });
 vi.mocked(api.post).mockImplementation(async () => { created = true; return item("新连接"); });
 render(<QueryClientProvider client={new QueryClient()}><Mcps /></QueryClientProvider>);
 await screen.findByText("首页旧连接");
 fireEvent.click(screen.getByRole("button", { name: "下一页" }));
 await screen.findByText("末页旧连接");
 fireEvent.click(await screen.findByRole("button", { name: "添加 MCP" }));
 fireEvent.click(screen.getByRole("button", { name: /从 MCP 服务库添加/ }));
 fireEvent.click(await screen.findByRole("button", { name: "配置连接" }));
 fireEvent.change(screen.getByLabelText("连接名称（可选）"), { target: { value: "new-connection" } });
 fireEvent.click(screen.getByRole("button", { name: "保存连接" }));
 const details = await screen.findByRole("dialog", { name: "新连接" });
 fireEvent.click(within(details).getByRole("button", { name: "关闭" }));
 await screen.findByText("新连接");
 expect(screen.getByText("第 1 / 2 页 · 共 13 条")).toBeVisible();
 expect(api.post).toHaveBeenCalledWith("/mcps", { hub_id: "external", card_id: "card", name: "new-connection", values: {}, scope_id: "org" });
 fireEvent.click(screen.getByRole("button", { name: "添加 MCP" }));
 fireEvent.click(screen.getByRole("button", { name: /从 MCP 服务库添加/ }));
 expect(await screen.findByRole("button", { name: "已添加" })).toBeDisabled();
});
