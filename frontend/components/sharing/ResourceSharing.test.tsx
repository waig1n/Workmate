import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, test, vi } from "vitest";
import { ResourceSharing } from "./ResourceSharing";
import { api } from "@/lib/api";
vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() } }));
const plan = { resource_id: "agent-a", name: "报告助手", target_scope_id: "b", target_name: "部门B", skills: [{ id: "s", name: "报告技能", scope_id: "a" }], blockers: [], confirmation: "reviewed" };
function mount() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ResourceSharing kind="agents" resourceId="agent-a" /></QueryClientProvider>);
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B" }], shared_scope_ids: [] });
  vi.mocked(api.post).mockResolvedValue(plan);
});
test("preview discloses dependency grants and only confirmation submits", async () => {
  mount();
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("部门B"));
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  expect(await screen.findByText("同时共享技能：报告技能")).toBeInTheDocument();
  expect(screen.getByText(/接收部门有权编辑智能体的成员/)).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("dialog", { name: "确认共享" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/agents/agent-a/batch", { targets: [{ target_scope_id: "b", include_descendants: false, confirmation: "reviewed" }] }));
});
test("dependency blocker disables sharing", async () => {
  vi.mocked(api.post).mockResolvedValue({ ...plan, blockers: ["MCP 对目标部门不可用"] });
  mount();
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("部门B"));
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  expect(await screen.findByText("MCP 对目标部门不可用")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "确认共享" })).toBeDisabled();
});
test("revocation requires confirmation and explains retained skills", async () => {
  vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B" }], shared_scope_ids: ["b"] });
  mount();
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByText("取消共享"));
  expect(screen.getByText(/已共享的技能保持共享/)).toBeInTheDocument();
  expect(api.delete).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("确认取消共享"));
  await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/sharing/agents/agent-a/b"));
});

test("multiple departments require preview and one explicit batch confirmation", async () => {
  vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B" }, { id: "c", name: "部门C" }], shared_scope_ids: [] });
  vi.mocked(api.post).mockImplementation(async (_path, body) => ({ ...plan, target_scope_id: (body as { target_scope_id: string }).target_scope_id, target_name: (body as { target_scope_id: string }).target_scope_id === "b" ? "部门B" : "部门C" }));
  mount();
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("部门B"));
  fireEvent.click(screen.getByLabelText("部门C"));
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  await screen.findByText("共享给：部门C");
  expect(api.post).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/agents/agent-a/batch", { targets: [{ target_scope_id: "b", include_descendants: false, confirmation: "reviewed" }, { target_scope_id: "c", include_descendants: false, confirmation: "reviewed" }] }));
});


test("cancel confirmation retains selected departments without granting access", async () => {
  mount();
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("部门B"));
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  await screen.findByRole("dialog", { name: "确认共享" });
  fireEvent.click(screen.getByRole("button", { name: "返回修改" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByLabelText("部门B")).toBeChecked();
  expect(api.post).toHaveBeenCalledTimes(1);
});

test("skill sharing explicitly offers organization-wide access including future departments", async () => {
  vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "org", name: "全组织可用", kind: "organization" }, { id: "b", name: "部门B", kind: "department" }], shared_scope_ids: [] });
  vi.mocked(api.post).mockResolvedValue({ ...plan, skills: [], target_scope_id: "org", target_name: "全组织可用" });
  render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="skills" resourceId="s1" /></QueryClientProvider>);
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("全组织可用"));
  expect(screen.getByText(/包括未来新增部门/)).toBeVisible();
  expect(screen.getByText(/不包含下级部门/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  await screen.findByText("共享给：全组织可用");
  fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/skills/s1/batch", { targets: [{ target_scope_id: "org", include_descendants: true, confirmation: "reviewed" }] }));
});

test("legacy organization direct grant remains visible and can be upgraded explicitly", async () => {
  vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "org", name: "全组织可用", kind: "organization" }], shared_scope_ids: ["org"], grants: [{ target_scope_id: "org", include_descendants: false }] });
  render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="skills" resourceId="s1" /></QueryClientProvider>);
  fireEvent.click(screen.getByText("共享"));
  expect(await screen.findByText("组织成员（历史授权）")).toBeVisible();
  expect(screen.getByLabelText("全组织可用")).not.toBeChecked();
});

test("knowledge grants retrieval by default and previews required dependencies", async () => {
  vi.mocked(api.post).mockResolvedValue({ ...plan, skills: [], knowledge_bases: [{ id: "kb", name: "制度库", scope_id: "a" }] });
  render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>);
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("部门B"));
  expect(screen.getByLabelText("允许下载原文")).not.toBeChecked();
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  expect(await screen.findByText("同时共享知识库：制度库")).toBeVisible();
  expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/preview", { target_scope_id: "b", include_descendants: false, allow_download: false });
  expect(screen.getByText("仅检索，不允许下载原文")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/batch", { targets: [{ target_scope_id: "b", include_descendants: false, confirmation: "reviewed", allow_download: false }] }));
});
test("existing knowledge retrieval grant can explicitly upgrade download and preserves failure feedback", async () => {
  vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B", kind: "department" }], shared_scope_ids: ["b"], grants: [{ target_scope_id: "b", include_descendants: false, allow_download: false }] });
  vi.mocked(api.post).mockImplementation(async (path) => { if (path.endsWith("/batch")) throw new Error("共享权限已撤销"); return plan; });
  render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>);
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("允许下载原文"));
  fireEvent.click(screen.getByLabelText("部门B"));
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  await screen.findByRole("dialog", { name: "确认共享" });
  expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/preview", { target_scope_id: "b", include_descendants: false, allow_download: true });
  fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
  expect(await screen.findByText("共享权限已撤销")).toBeVisible();
  expect(api.delete).not.toHaveBeenCalled();
});

test("knowledge download can be revoked without removing retrieval authorization", async () => {
  let download = true;
  vi.mocked(api.get).mockImplementation(async () => ({ targets: [{ id: "b", name: "部门B", kind: "department" }], shared_scope_ids: ["b"], grants: [{ target_scope_id: "b", include_descendants: false, allow_download: download }] }));
  vi.mocked(api.post).mockImplementation(async (path, body) => { if (path.endsWith("/batch")) download = (body as { targets: { allow_download: boolean }[] }).targets[0].allow_download; return { ...plan, skills: [] }; });
  render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>);
  fireEvent.click(screen.getByText("共享"));
  fireEvent.click(await screen.findByLabelText("部门B"));
  expect(screen.getByLabelText("部门B：允许下载原文")).toBeChecked();
  fireEvent.click(screen.getByLabelText("部门B：允许下载原文"));
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  expect(await screen.findByText("仅检索，不允许下载原文")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
  await waitFor(() => expect(download).toBe(false));
  expect(await screen.findByText("仅可检索")).toBeVisible();
  expect(api.delete).not.toHaveBeenCalled();
});

test("shows the existing use range without opening sharing settings", async () => {
 vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B" }], shared_scope_ids: ["b"] });
 mount();
 expect(await screen.findByText("使用范围：部门B（仅本部门）")).toBeVisible();
});
test("draft publication opens use selection and keeps dependency confirmation", async () => {
 render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="agents" resourceId="agent-a" publishOnShare initiallyOpen /></QueryClientProvider>);
 fireEvent.click(await screen.findByLabelText("部门B"));
 fireEvent.click(screen.getByRole("button", { name: "发布并设置使用范围" }));
 expect(await screen.findByText(/确认共享将同时发布/)).toBeVisible();
 expect(api.post).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/agents/agent-a/batch", { targets: [{ target_scope_id: "b", include_descendants: false, confirmation: "reviewed" }] }));
});

test("department subtree choices are independent and included in preview and confirmation", async () => {
 vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B", kind: "department" }, { id: "c", name: "部门C", kind: "department", parent_id: "b" }], shared_scope_ids: [] });
 vi.mocked(api.post).mockImplementation(async (_path, body) => ({ ...plan, ...(body as object) }));
 mount(); fireEvent.click(screen.getByText("共享"));
 fireEvent.click(await screen.findByLabelText("部门B"));
 fireEvent.click(screen.getByLabelText("部门B：同时包含下级部门"));
 fireEvent.click(screen.getByLabelText("部门B / 部门C"));
 expect(screen.getByLabelText("部门B / 部门C：同时包含下级部门")).not.toBeChecked();
 fireEvent.click(screen.getByRole("button", { name: "共享" }));
 await screen.findByRole("dialog", { name: "确认共享" });
 expect(api.post).toHaveBeenCalledWith("/sharing/agents/agent-a/preview", { target_scope_id: "b", include_descendants: true });
 expect(api.post).toHaveBeenCalledWith("/sharing/agents/agent-a/preview", { target_scope_id: "c", include_descendants: false });
 fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/agents/agent-a/batch", { targets: [{ target_scope_id: "b", include_descendants: true, confirmation: "reviewed" }, { target_scope_id: "c", include_descendants: false, confirmation: "reviewed" }] }));
});

test("narrowing an existing department grant explicitly confirms retained independent grants", async () => {
 vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B", kind: "department" }], shared_scope_ids: ["b"], grants: [{ target_scope_id: "b", include_descendants: true }] });
 vi.mocked(api.post).mockImplementation(async (_path, body) => ({ ...plan, ...(body as object) }));
 mount(); fireEvent.click(screen.getByText("共享"));
 fireEvent.click(await screen.findByLabelText("部门B"));
 expect(screen.getByLabelText("部门B：同时包含下级部门")).toBeChecked();
 fireEvent.click(screen.getByLabelText("部门B：同时包含下级部门"));
 fireEvent.click(screen.getByRole("button", { name: "共享" }));
 expect(await screen.findByText(/收窄为仅本部门/)).toBeVisible();
 expect(screen.getByText(/独立授权和来自祖先部门的授权不会撤销/)).toBeVisible();
 expect(api.delete).not.toHaveBeenCalled();
});

test("updating a knowledge subtree preserves its existing download grant", async () => {
 vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B", kind: "department" }], shared_scope_ids: ["b"], grants: [{ target_scope_id: "b", include_descendants: true, allow_download: true }] });
 render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>);
 fireEvent.click(screen.getByText("共享"));
 fireEvent.click(await screen.findByLabelText("部门B"));
 fireEvent.click(screen.getByLabelText("部门B：同时包含下级部门"));
 fireEvent.click(screen.getByRole("button", { name: "共享" }));
 await screen.findByRole("dialog", { name: "确认共享" });
 expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/preview", { target_scope_id: "b", include_descendants: false, allow_download: true });
});

test("batch download changes preserve each target's inherited range", async () => {
 vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "b", name: "部门B", kind: "department" }, { id: "c", name: "部门C", kind: "department" }], shared_scope_ids: ["b", "c"], grants: [{ target_scope_id: "b", include_descendants: true, allow_download: false }, { target_scope_id: "c", include_descendants: false, allow_download: false }] });
 vi.mocked(api.post).mockImplementation(async (_path, body) => ({ ...plan, ...(body as object) }));
 render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>);
 fireEvent.click(screen.getByText("共享"));
 fireEvent.click(await screen.findByLabelText("部门B"));
 fireEvent.click(screen.getByLabelText("部门C"));
 fireEvent.click(screen.getByLabelText("允许下载原文"));
 fireEvent.click(screen.getByLabelText("部门C：允许下载原文"));
 fireEvent.click(screen.getByRole("button", { name: "共享" }));
 await screen.findByRole("dialog", { name: "确认共享" });
 expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/preview", { target_scope_id: "b", include_descendants: true, allow_download: true });
 expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/preview", { target_scope_id: "c", include_descendants: false, allow_download: false });
 fireEvent.click(screen.getByRole("button", { name: "确认共享" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/batch", { targets: [{ target_scope_id: "b", include_descendants: true, allow_download: true, confirmation: "reviewed" }, { target_scope_id: "c", include_descendants: false, allow_download: false, confirmation: "reviewed" }] }));
});

test("editing a historical organization download grant does not expand its audience", async () => {
 vi.mocked(api.get).mockResolvedValue({ targets: [{ id: "org", name: "全组织可用", kind: "organization" }], shared_scope_ids: ["org"], grants: [{ target_scope_id: "org", include_descendants: false, allow_download: false }] });
 vi.mocked(api.post).mockImplementation(async (_path, body) => ({ ...plan, ...(body as object) }));
 render(<QueryClientProvider client={new QueryClient()}><ResourceSharing kind="knowledge-bases" resourceId="kb" /></QueryClientProvider>);
 fireEvent.click(screen.getByText("共享"));
 fireEvent.click(await screen.findByLabelText("全组织可用"));
 fireEvent.click(screen.getByLabelText("全组织可用：允许下载原文"));
 fireEvent.click(screen.getByRole("button", { name: "共享" }));
 await screen.findByRole("dialog", { name: "确认共享" });
 expect(api.post).toHaveBeenCalledWith("/sharing/knowledge-bases/kb/preview", { target_scope_id: "org", include_descendants: false, allow_download: true });
});

test("Agent dependency confirmation discloses managed MCP reuse without credential transfer", async () => {
 vi.mocked(api.post).mockResolvedValue({ ...plan, mcps: [{ id: "mcp", name: "财务报表连接", scope_id: "a" }] });
 mount(); fireEvent.click(screen.getByText("共享"));
 fireEvent.click(await screen.findByLabelText("部门B"));
 fireEvent.click(screen.getByRole("button", { name: "共享" }));
 expect(await screen.findByText("同时共享 MCP 连接：财务报表连接")).toBeVisible();
 expect(screen.getByText(/复用来源维护的受管连接/)).toBeVisible();
 expect(api.post).toHaveBeenCalledTimes(1);
});
