import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import WorkspaceLayout from "./layout";
import { useWorkspaceDraft } from "@/components/workspace/WorkspaceDraftProvider";
import { usePermissions } from "@/lib/usePermissions";
import { api } from "@/lib/api";
const navigation = vi.hoisted(() => ({ push: vi.fn(), pathname: "/workspace" }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: navigation.push }) }));
vi.mock("@/lib/usePermissions", () => ({ usePermissions: vi.fn(() => ({ data: undefined })) }));
vi.mock("@/lib/api", () => ({ clearAuth: vi.fn(), api: {
 me: async () => ({ id: "user", display_name: "用户" }), get: vi.fn(async (path: string) => path.startsWith("/agents?") ? { items: [
 { id: "other", display_name: "其他助手", actions: { use: true } },
 { id: "default", display_name: "默认助手", is_default: true, actions: { use: true } },
 { id: "blocked", display_name: "不可用助手", actions: { use: false } },
 ], total: 3, page: 1, page_size: 100 } : path === "/conversations/old" ? { id: "old", agent_id: "other", agent: { display_name: "其他助手" } } : []), post: vi.fn(), patch: vi.fn() } }));
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); navigation.pathname = "/workspace"; vi.mocked(usePermissions).mockReturnValue({ data: undefined } as ReturnType<typeof usePermissions>); });
function DraftProbe() {
 const { draft, updateDraft } = useWorkspaceDraft();
 return <input aria-label="草稿" value={draft.input} onChange={(event) => updateDraft({ input: event.target.value })} />;
}
function mount() {
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
 render(<QueryClientProvider client={client}><WorkspaceLayout><DraftProbe /></WorkspaceLayout></QueryClientProvider>);
 return client;
}
it("selects the default in the sidebar and never creates on opening or switching drafts", async () => {
 mount();
 const picker = screen.getByRole("combobox", { name: "当前智能体" });
 await waitFor(() => expect(picker).toHaveValue("default"));
 expect(picker.closest("aside")).not.toBeNull();
 expect(screen.queryByText("今日概览")).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText("草稿"), { target: { value: "给默认助手的草稿" } });
 fireEvent.change(picker, { target: { value: "other" } });
 expect(navigation.push).toHaveBeenCalledWith("/workspace");
 expect(screen.getByLabelText("草稿")).toHaveValue("");
 fireEvent.change(picker, { target: { value: "default" } });
 expect(screen.getByLabelText("草稿")).toHaveValue("给默认助手的草稿");
 fireEvent.click(screen.getAllByRole("button", { name: "新建对话" })[0]);
 fireEvent(window, new Event("workmate:create"));
 expect(api.post).not.toHaveBeenCalled();
});
it("shows the existing conversation agent and opens a draft instead of modifying it", async () => {
 navigation.pathname = "/workspace/c/old";
 mount();
 const picker = screen.getByRole("combobox", { name: "当前智能体" });
 await waitFor(() => expect(picker).toHaveValue("other"));
 fireEvent.change(picker, { target: { value: "default" } });
 expect(navigation.push).toHaveBeenCalledWith("/workspace");
 expect(api.patch).not.toHaveBeenCalled();
 expect(api.post).not.toHaveBeenCalled();
});
it("offers an accessible mobile agent switcher", async () => {
 mount();
 await waitFor(() => expect(screen.getByRole("combobox", { name: "当前智能体" })).toHaveValue("default"));
 fireEvent.click(screen.getByRole("button", { name: "切换智能体" }));
 const mobile = screen.getByRole("combobox", { name: "当前智能体（移动端）" });
 fireEvent.change(mobile, { target: { value: "other" } });
 expect(navigation.push).toHaveBeenCalledWith("/workspace");
 expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("does not create a conversation when no agent is usable", async () => {
 const client = mount();
 await waitFor(() => expect(screen.getByRole("combobox", { name: "当前智能体" })).toHaveValue("default"));
 client.setQueryData(["agents"], []);
 await waitFor(() => expect(screen.getByRole("combobox", { name: "当前智能体" })).toBeDisabled());
 fireEvent(window, new Event("workmate:create"));
 expect(api.post).not.toHaveBeenCalled();
});

function withManagementPermissions() {
 vi.mocked(usePermissions).mockReturnValue({ data: { root_scope_id: "root", root_permissions: ["user.read", "role.read"], managed_scope_ids: ["dept"], permissions: ["audit.read"] } } as unknown as ReturnType<typeof usePermissions>);
}
it("opens management as a destination", () => {
 withManagementPermissions(); mount();
 expect(screen.getByRole("link", { name: "管理中心" })).toHaveAttribute("href", "/workspace/users");
 expect(screen.queryByRole("link", { name: "用户管理" })).not.toBeInTheDocument();
});
it("replaces conversations with management navigation", () => {
 withManagementPermissions(); navigation.pathname = "/workspace/users"; mount();
 expect(screen.getByRole("link", { name: "用户管理" })).toHaveAttribute("aria-current", "page");
 expect(screen.queryByRole("navigation", { name: "最近会话" })).not.toBeInTheDocument();
 expect(screen.getByRole("link", { name: "返回工作台" })).toHaveAttribute("href", "/workspace");
});
it("hides management entry without permissions", () => {
 mount(); expect(screen.queryByRole("button", { name: "管理中心" })).not.toBeInTheDocument();
 expect(screen.queryByRole("link", { name: "管理中心" })).not.toBeInTheDocument();
});

it("offers the same management menu on mobile", () => {
 withManagementPermissions(); mount();
 fireEvent.click(screen.getByRole("button", { name: "管理中心" }));
 expect(screen.getByRole("dialog", { name: "管理中心" })).toBeVisible();
 const auditLink = screen.getByRole("link", { name: "管理审计" });
 auditLink.addEventListener("click", (event) => event.preventDefault(), { once: true });
 fireEvent.click(auditLink);
 expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("clears private cached content on logout", async () => {
 const client = mount();
 await waitFor(() => expect(screen.getByRole("combobox", { name: "当前智能体" })).toHaveValue("default"));
 client.setQueryData(["agent", "private"], { system_prompt: "secret" });
 client.setQueryData(["skill-description", "private"], "secret markdown");
 fireEvent.click(screen.getByRole("button", { name: "退出" }));
 expect(client.getQueryData(["agent", "private"])).toBeUndefined();
 expect(client.getQueryData(["skill-description", "private"])).toBeUndefined();
 expect(navigation.push).toHaveBeenCalledWith("/login");
});

it("combines resource links into one sidebar destination", () => {
 mount();
 const sidebar = document.querySelector("aside")!;
 expect(within(sidebar).getByRole("link", { name: "资源中心" })).toHaveAttribute("href", "/workspace/agents");
 for (const name of ["智能体", "知识库", "技能", "连接器"]) expect(within(sidebar).queryByRole("link", { name })).not.toBeInTheDocument();
 expect(screen.queryByRole("navigation", { name: "资源中心分类" })).not.toBeInTheDocument();
});
it("keeps resource routes addressable and highlights nested destinations", async () => {
 navigation.pathname = "/workspace/skills/example"; mount();
 const tabs = screen.getByRole("navigation", { name: "资源中心分类" });
 expect(within(tabs).getByRole("link", { name: "技能" })).toHaveAttribute("aria-current", "page");
 expect(within(tabs).getByRole("link", { name: "知识库" })).toHaveAttribute("href", "/workspace/knowledge-bases");
 await waitFor(() => expect(within(document.querySelector("aside")!).getByRole("link", { name: "资源中心" })).toHaveAttribute("href", "/workspace/skills"));
});
it("restores the last resource destination", async () => {
 localStorage.setItem("workmate:resource-center:last-tab", "/workspace/mcps"); mount();
 await waitFor(() => expect(within(document.querySelector("aside")!).getByRole("link", { name: "资源中心" })).toHaveAttribute("href", "/workspace/mcps"));
});

it("ignores unknown stored destinations", () => {
 localStorage.setItem("workmate:resource-center:last-tab", "https://example.com"); mount();
 expect(within(document.querySelector("aside")!).getByRole("link", { name: "资源中心" })).toHaveAttribute("href", "/workspace/agents");
});
it("does not match similarly prefixed non-resource routes", () => {
 navigation.pathname = "/workspace/agents-other"; mount();
 expect(screen.queryByRole("navigation", { name: "资源中心分类" })).not.toBeInTheDocument();
});
it("uses the first authorized management destination", () => {
 vi.mocked(usePermissions).mockReturnValue({ data: { managed_scope_ids: ["dept"], permissions: [], root_permissions: [] } } as unknown as ReturnType<typeof usePermissions>);
 mount();
 expect(screen.getByRole("link", { name: "管理中心" })).toHaveAttribute("href", "/workspace/scopes");
});
it("keeps resource navigation available when preference writes fail", () => {
 const original = Storage.prototype.setItem;
 const stub = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
  if (key === "workmate:resource-center:last-tab") throw new Error("Storage unavailable");
  original.call(this, key, value);
 });
 try {
  navigation.pathname = "/workspace/mcps"; mount();
  expect(within(screen.getByRole("navigation", { name: "资源中心分类" })).getByRole("link", { name: "连接器" })).toHaveAttribute("aria-current", "page");
 } finally { stub.mockRestore(); }
});
