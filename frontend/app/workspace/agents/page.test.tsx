import { resourcePageMock } from "@/test/resourcePages";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import AgentsPage from "./page";
import { api } from "@/lib/api";
const { push, selectAgent } = vi.hoisted(() => ({ push: vi.fn(), selectAgent: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/components/workspace/WorkspaceDraftProvider", () => ({ useWorkspaceDraft: () => ({ selectAgent, sending: false }) }));
vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() } }));
const personal = { id: "personal", name: "我的", kind: "personal", permissions: [] };
const org = { id: "org", name: "研发中心", kind: "organization", permissions: [] };
const own = { id: "own", display_name: "个人写作助手", slug: "writer", description: "整理写作材料", scope: personal, scope_id: "personal", visibility: "private", system_prompt: "帮助写作", tool_policy: { denied_groups: [], denied_tools: [] }, actions: { use: true, update: true, delete: true }, skill_binding_ids: [], mcp_binding_ids: [], unavailable_capability_ids: [], is_default: false };
const shared = { ...own, id: "shared", display_name: "共享分析助手", scope: org, scope_id: "org", actions: { use: true, update: false, delete: false } };
beforeEach(() => {
 vi.clearAllMocks();
 vi.mocked(api.get).mockImplementation(resourcePageMock(async (path) => {
  if (path === "/auth/me/permissions") return { permissions: [], personal_scope_id: "personal" };
  if (path === "/scopes/accessible") return [personal, org];
  if (path === "/agents") return [own, shared];
  return [];
 }));
 vi.mocked(api.post).mockResolvedValue(own);
 vi.mocked(api.patch).mockResolvedValue(own);
});
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AgentsPage /></QueryClientProvider>); }
it("lets an ordinary user create a private personal agent without organization grants", async () => {
 mount();
 fireEvent.click(await screen.findByRole("button", { name: "创建个人智能体" }));
 expect(screen.queryByRole("button", { name: "创建组织智能体" })).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "我的助手" } });
 fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/agents", expect.objectContaining({ display_name: "我的助手", visibility: "private", platform_tool_profile: "none", slug: expect.stringMatching(/^agent-/) })));
 expect(vi.mocked(api.post).mock.calls[0][1]).not.toHaveProperty("scope_id");
});
it("opens details on demand and allows editing only the owner's agent", async () => {
 mount();
 await screen.findByText("个人写作助手");
 expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
 fireEvent.click(screen.getByText("个人写作助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "修改后的助手" } });
 fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/agents/own", expect.objectContaining({ display_name: "修改后的助手" })));
 fireEvent.click(screen.getByRole("button", { name: "关闭" }));
 fireEvent.click(screen.getByText("共享分析助手"));
 expect(within(screen.getByRole("dialog")).queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole("button", { name: "试用此智能体" }));
 expect(selectAgent).toHaveBeenCalledWith("shared");
 expect(push).toHaveBeenCalledWith("/workspace");
 expect(api.post).not.toHaveBeenCalled();
});
it("filters the catalog by ownership and text", async () => {
 mount(); await screen.findByText("共享分析助手");
 fireEvent.click(screen.getByRole("button", { name: "我的" }));
 expect(screen.queryByText("共享分析助手")).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText("搜索智能体"), { target: { value: "不存在" } });
 expect(await screen.findByText(/没有找到匹配/)).toBeInTheDocument();
});
it("copies permitted configuration into a private draft without platform identity or bindings", async () => {
 mount(); fireEvent.click(await screen.findByText("个人写作助手"));
 fireEvent.click(await screen.findByRole("button", { name: "复制为个人智能体" }));
 expect(screen.getByLabelText("显示名称")).toHaveValue("个人写作助手（副本）");
 fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/agents", expect.objectContaining({ visibility: "private", platform_tool_profile: "none", skill_binding_ids: [], mcp_binding_ids: [], system_prompt: "帮助写作" })));
 expect(vi.mocked(api.post).mock.calls[0][1]).not.toHaveProperty("system_key");
});
it("keeps the entered form when creation fails", async () => {
 vi.mocked(api.post).mockRejectedValue(new Error("暂时无法保存"));
 mount(); fireEvent.click(await screen.findByRole("button", { name: "创建个人智能体" }));
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "保留草稿" } });
 fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法保存");
 expect(screen.getByLabelText("显示名称")).toHaveValue("保留草稿");
});
it('sends an explicit empty knowledge configuration for an unbound new agent', async () => {
 mount();fireEvent.click(await screen.findByRole('button',{name:'创建个人智能体'}));
 expect(screen.queryByLabelText('由 Agent 自主决定何时检索')).toBeNull();
 fireEvent.change(screen.getByLabelText('显示名称'),{target:{value:'普通助手'}});
 fireEvent.click(screen.getByRole('button',{name:'创建智能体'}));
 await waitFor(()=>expect(api.post).toHaveBeenCalledWith('/agents',expect.objectContaining({knowledge_config:{knowledge_base_ids:[],parameters:{mode:'agentic',top_k:5}}})));
});

it("publishing does not grant ancestry access and returning private discloses revocation", async () => {
 const managed = { ...shared, visibility: "public", include_descendants: true, actions: { use: false, update: true, publish: true, delete: true } };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/agents" ? [managed] : path === "/scopes/accessible" ? [{ ...org, permissions: ["agent.create", "agent.update", "agent.publish"] }] : path === "/auth/me/permissions" ? { permissions: [], personal_scope_id: "personal" } : []));
 mount();
 fireEvent.click(await screen.findByText("共享分析助手"));
 expect(screen.queryByRole("button", { name: "使用此智能体" })).not.toBeInTheDocument();
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 expect(screen.queryByLabelText("包含下级部门")).not.toBeInTheDocument();
 expect(screen.queryByRole("radio")).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole("button", { name: "撤回为草稿" }));
 expect(screen.getByText(/撤销全部智能体共享/)).toBeVisible();
 vi.mocked(api.patch).mockRejectedValueOnce(new Error("权限已变更，请重新加载"));
 fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
 expect(await screen.findByRole("alert")).toHaveTextContent("权限已变更");
 expect(api.patch).toHaveBeenCalledWith("/agents/shared", expect.objectContaining({ visibility: "private" }));
 expect(vi.mocked(api.patch).mock.calls[0][1]).not.toHaveProperty("include_descendants");
 expect(screen.getByText(/撤销全部智能体共享/)).toBeVisible();
});

it("ordinary Agent users see only summary even when an old editable detail is cached", async () => {
 const summary = { ...shared, description: "可使用的公开简介", system_prompt: "服务端已移除", skill_binding_ids: ["hidden-skill"], mcp_binding_ids: ["hidden-mcp"], knowledge_config: { knowledge_base_ids: ["hidden-kb"], parameters: { mode: "static", top_k: 5 } } };
 vi.mocked(api.get).mockImplementation(async path => {
  if (path.startsWith("/agents?")) return { items: [summary], total: 1, page: 1, page_size: 12 };
  if (path === "/agents/shared") return new Promise(() => {});
  if (path === "/auth/me/permissions") return { permissions: [], personal_scope_id: "personal" };
  if (path === "/scopes/accessible") return [personal, org];
  return [];
 });
 const client = new QueryClient();
 client.setQueryData(["agents", "detail", "shared"], { ...summary, actions: { use: true, update: true, delete: true, publish: true }, system_prompt: "管理员缓存的机密提示词" });
 render(<QueryClientProvider client={client}><AgentsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByText("共享分析助手"));
 expect(screen.getAllByText("可使用的公开简介").length).toBeGreaterThan(0);
 expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
 expect(screen.queryByText("系统提示词")).not.toBeInTheDocument();
 expect(screen.queryByText("关联能力")).not.toBeInTheDocument();
 expect(screen.queryByText(/每轮自动检索/)).not.toBeInTheDocument();
 expect(api.get).not.toHaveBeenCalledWith("/skills");
 expect(api.get).not.toHaveBeenCalledWith("/mcps");
 expect(screen.queryByDisplayValue("管理员缓存的机密提示词")).not.toBeInTheDocument();
 expect(screen.queryByRole("button", { name: "复制为个人智能体" })).not.toBeInTheDocument();
});
it("Agent owners can still read and edit their system prompt", async () => {
 mount();
 fireEvent.click(await screen.findByText("个人写作助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 const prompt = screen.getByDisplayValue("帮助写作");
 expect(prompt).toBeEnabled();
 fireEvent.change(prompt, { target: { value: "新的内部工作指令" } });
 fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/agents/own", expect.objectContaining({ system_prompt: "新的内部工作指令" })));
});

it("revoking Agent management removes an already open prompt editor", async () => {
 const client = new QueryClient();
 render(<QueryClientProvider client={client}><AgentsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByText("个人写作助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 expect(screen.getByLabelText("系统提示词")).toBeVisible();
 await act(async () => { await client.cancelQueries({ queryKey: ["agents", "detail", "own"] }); client.setQueryData(["agents", "detail", "own"], { ...own, actions: { use: true, update: false, delete: false }, system_prompt: "" }); });
 await waitFor(() => expect(screen.queryByLabelText("系统提示词")).not.toBeInTheDocument());
 expect(screen.queryByRole("button", { name: "保存配置" })).not.toBeInTheDocument();
 expect(screen.getByRole("dialog", { name: "个人写作助手" })).toBeVisible();
});

it.each([403, 404])("Agent detail recheck %s hides stale prompt even when the selected Agent is off-page", async (status) => {
 let pendingDetail: Promise<unknown> | undefined;
 let rejectDetail!: (error: Error) => void;
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 vi.mocked(api.get).mockImplementation(async path => {
  if (path.startsWith("/agents?")) { const page = Number(new URL(path, "http://test").searchParams.get("page")); return { items: page === 1 ? [own] : [{ ...own, id: "other", display_name: "另一页智能体" }], total: 13, page, page_size: 12 }; }
  if (path === "/agents/own") return pendingDetail ?? own;
  if (path === "/auth/me/permissions") return { permissions: [], personal_scope_id: "personal" };
  if (path === "/scopes/accessible") return [personal, org];
  return [];
 });
 render(<QueryClientProvider client={client}><AgentsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByText("个人写作助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 expect(screen.getByLabelText("系统提示词")).toHaveValue("帮助写作");
 fireEvent.click(screen.getByRole("button", { name: "下一页" }));
 await screen.findByText("另一页智能体");
 pendingDetail = new Promise((_resolve, reject) => { rejectDetail = reject; });
 act(() => { void client.invalidateQueries({ queryKey: ["agents", "detail", "own"] }); });
 await waitFor(() => expect(screen.queryByLabelText("系统提示词")).not.toBeInTheDocument());
 expect(screen.queryByRole("button", { name: "保存配置" })).not.toBeInTheDocument();
 await act(async () => rejectDetail(Object.assign(new Error("无权查看"), { status })));
 await screen.findByRole("button", { name: "重试详情" });
 expect(screen.queryByLabelText("系统提示词")).not.toBeInTheDocument();
 expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
 expect(screen.queryByDisplayValue("帮助写作")).not.toBeInTheDocument();
});

it("a successful background detail recheck restores the unsaved draft without resetting it", async () => {
 let pendingDetail: Promise<unknown> | undefined;
 let resolveDetail!: (value: unknown) => void;
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 vi.mocked(api.get).mockImplementation(async path => {
  if (path.startsWith("/agents?")) return { items: [own], total: 1, page: 1, page_size: 12 };
  if (path === "/agents/own") return pendingDetail ?? own;
  if (path === "/auth/me/permissions") return { permissions: [], personal_scope_id: "personal" };
  if (path === "/scopes/accessible") return [personal, org];
  return [];
 });
 render(<QueryClientProvider client={client}><AgentsPage /></QueryClientProvider>);
 fireEvent.click(await screen.findByText("个人写作助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 fireEvent.change(screen.getByLabelText("系统提示词"), { target: { value: "尚未保存的工作指令" } });
 pendingDetail = new Promise(resolve => { resolveDetail = resolve; });
 act(() => { void client.invalidateQueries({ queryKey: ["agents", "detail", "own"] }); });
 await waitFor(() => expect(screen.queryByLabelText("系统提示词")).not.toBeInTheDocument());
 await act(async () => resolveDetail(own));
 // The editor is remounted only after the fresh permission check succeeds.
 fireEvent.click(await screen.findByText("高级配置：指令、技能与权限"));
 expect(screen.getByLabelText("系统提示词")).toHaveValue("尚未保存的工作指令");
});

it("creates an organization draft and immediately asks for its use range", async () => {
 const created = { ...shared, visibility: "private", actions: { use: true, update: true, publish: true, delete: true } };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => {
  if (path === "/auth/me/permissions") return { permissions: [], personal_scope_id: "personal" };
  if (path === "/scopes/accessible") return [{ ...org, permissions: ["agent.create", "agent.update", "agent.publish"] }];
  if (path === "/agents") return [created];
  if (path === "/sharing/agents/shared") return { targets: [{ id: "finance", name: "财务部" }], shared_scope_ids: [] };
  return [];
 }));
 vi.mocked(api.post).mockResolvedValue(created);
 mount();
 fireEvent.click(await screen.findByRole("button", { name: "创建组织智能体" }));
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "共享分析助手" } });
 fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
 expect(await screen.findByLabelText("财务部")).toBeVisible();
 expect(api.post).toHaveBeenCalledWith("/agents", expect.objectContaining({ visibility: "private" }));
 expect(screen.getByRole("button", { name: "发布并设置使用范围" })).toBeVisible();
 expect(screen.getByRole("button", { name: "试用此智能体" })).toBeEnabled();
 expect(api.post).toHaveBeenCalledTimes(1);
});

it("does not allow trial when a bound dependency is unavailable", async () => {
 const degraded = { ...own, unavailable_capability_ids: ["missing"], skill_binding_ids: ["missing"] };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/agents" ? [degraded] : path === "/auth/me/permissions" ? { permissions: [], personal_scope_id: "personal" } : path === "/scopes/accessible" ? [personal, org] : path === "/skills" ? [{ id: "missing", name: "财务报告技能" }] : []));
 mount(); fireEvent.click(await screen.findByText("个人写作助手"));
 const use = await screen.findByRole("button", { name: "使用此智能体" });
 expect(use).toBeDisabled();
 fireEvent.click(use);
 expect(selectAgent).not.toHaveBeenCalled();
});

it("only publishers can withdraw and can cancel withdrawal without losing edits", async () => {
 const managed = { ...shared, visibility: "public", actions: { use: false, update: true, publish: true, delete: true } };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/agents" ? [managed] : path === "/scopes/accessible" ? [org] : path === "/auth/me/permissions" ? { permissions: [], personal_scope_id: "personal" } : []));
 vi.mocked(api.patch).mockResolvedValue(managed);
 mount(); fireEvent.click(await screen.findByText("共享分析助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "保留编辑" } });
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 fireEvent.click(screen.getByRole("button", { name: "撤回为草稿" }));
 fireEvent.click(screen.getByRole("button", { name: "取消撤回" }));
 expect(screen.getByLabelText("显示名称")).toHaveValue("保留编辑");
 fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/agents/shared", expect.objectContaining({ visibility: "public", display_name: "保留编辑" })));
});

it("does not offer withdrawal to editors without publication permission", async () => {
 const managed = { ...shared, visibility: "public", actions: { use: false, update: true, publish: false, delete: false } };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => path === "/agents" ? [managed] : path === "/scopes/accessible" ? [org] : path === "/auth/me/permissions" ? { permissions: [], personal_scope_id: "personal" } : []));
 mount(); fireEvent.click(await screen.findByText("共享分析助手"));
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 expect(screen.queryByRole("button", { name: "撤回为草稿" })).not.toBeInTheDocument();
});
it("hides built-in prompts even for administrators and never saves hidden prompt fields", async () => {
 const builtin = { ...own, id: "builtin", system_key: "workspace_assistant", display_name: "平台助手", system_prompt: "内置机密指令", scope: org, scope_id: "org" };
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => {
  if (path === "/auth/me/permissions") return { permissions: ["agent.update"], root_permissions: ["agent.update"], root_scope_id: "org", personal_scope_id: "personal", is_root_admin: true };
  if (path === "/scopes/accessible") return [personal, { ...org, permissions: ["agent.update"] }];
  if (path === "/agents") return [builtin];
  if (path === "/agents/builtin") return builtin;
  return [];
 }));
 vi.mocked(api.patch).mockResolvedValue(builtin);
 mount();
 fireEvent.click(await screen.findByText("平台助手"));
 expect(screen.queryByRole("button", { name: "复制为个人智能体" })).not.toBeInTheDocument();
 fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
 fireEvent.click(screen.getByText("高级配置：指令、技能与权限"));
 expect(screen.queryByLabelText("系统提示词")).not.toBeInTheDocument();
 expect(screen.queryByDisplayValue("内置机密指令")).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "更新简介助手" } });
 fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
 await waitFor(() => expect(api.patch).toHaveBeenCalled());
 expect(vi.mocked(api.patch).mock.calls[0][1]).not.toHaveProperty("system_prompt");
});

it("keeps healthy skill and MCP bindings editable for a manager without use access", async () => {
 const managed = { ...shared, actions: { use: false, update: true, delete: true }, skill_binding_ids: ['skill'], mcp_binding_ids: ['mcp'], unavailable_capability_ids: ['skill', 'mcp'], binding_issues: [] };
 vi.mocked(api.patch).mockResolvedValue(managed);
 vi.mocked(api.get).mockImplementation(resourcePageMock(async path => {
  if (path === '/agents') return [managed];
  if (path === '/scopes/accessible') return [org];
  if (path === '/auth/me/permissions') return { permissions: [], personal_scope_id: 'personal' };
  if (path.startsWith('/agents/capability-candidates')) {
   const kind = new URL(path, 'http://test').searchParams.get('capability_type');
   return [{ id: kind, name: kind, display_name: kind === 'skill' ? '分析技能' : '数据连接', scope_id: 'org', scope_kind: 'organization', enabled: true, usable: false, manageable: true, metadata: {} }];
  }
  return [];
 }));
 mount();
 fireEvent.click(await screen.findByText('共享分析助手'));
 expect(await screen.findByText('你可以管理此智能体；使用或试运行需要相应的使用授权。')).toBeVisible();
 fireEvent.click(screen.getByRole('button', { name: '编辑' }));
 fireEvent.click(screen.getByText('高级配置：指令、技能与权限'));
 const skill = await screen.findByLabelText(/分析技能/);
 const mcp = await screen.findByLabelText(/数据连接/);
 expect(skill).toBeChecked();
 expect(skill).not.toBeDisabled();
 expect(mcp).toBeChecked();
 expect(mcp).not.toBeDisabled();
 expect(screen.queryByText(/资源已移除|资源已停用|需向智能体所属范围开放使用/)).toBeNull();
 fireEvent.click(screen.getByRole('button', { name: '保存配置' }));
 await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/agents/shared', expect.objectContaining({ skill_binding_ids: ['skill'], mcp_binding_ids: ['mcp'] })));
});

it("lets an owner disable user profile reads independently of workspace management", async () => {
 mount(); fireEvent.click(await screen.findByRole("button", { name: "创建个人智能体" }));
 fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "资料助手" } });
 fireEvent.click(screen.getByRole("checkbox", { name: "允许读取当前用户资料" }));
 fireEvent.click(screen.getByRole("button", { name: "创建智能体" }));
 await waitFor(() => expect(api.post).toHaveBeenCalledWith("/agents", expect.objectContaining({
  platform_tool_profile: "none", tool_policy: expect.objectContaining({ denied_tools: ["get_current_user"] }),
 })));
});
